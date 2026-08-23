"""The eval's client adapters: command construction and transcript parsing.

The Codex adapter was written without an OpenAI credential to run it against,
so everything about it that can be checked without one is checked here. That is
more than it sounds: which flags are passed, which are deliberately absent, and
— the part that matters — that a transcript it cannot understand is reported as
a harness failure rather than as a model that used no tools.

That distinction is not hypothetical. This suite has already had a check pass
because a run made zero tool calls, and an empty trace from a mismatched parser
looks exactly the same from the outside.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))

from mcp_tool_eval import (  # noqa: E402
    ClaudeClient,
    CodexClient,
    parse_model_spec,
)


def jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


# --- choosing a client and model ---------------------------------------------


def test_a_bare_client_name_means_its_default_model():
    spec = parse_model_spec("claude")
    assert spec.client.name == "claude"
    assert spec.model is None
    assert spec.label == "claude"


def test_a_model_can_be_named_after_the_client():
    spec = parse_model_spec("codex:gpt-5-codex")
    assert spec.client.name == "codex"
    assert spec.model == "gpt-5-codex"
    assert spec.label == "codex:gpt-5-codex"


def test_an_unknown_client_is_refused_by_name():
    with pytest.raises(ValueError, match="unknown client"):
        parse_model_spec("gpt4all")


# --- claude, the verified path -----------------------------------------------


def test_claude_runs_with_no_builtin_tools():
    """Without this the client reads the notebook with its own file reader and
    never touches the surface under test."""
    command = ClaudeClient().command("do it", Path("/c"), Path("/c/project"), None)
    assert "--tools" in command
    assert command[command.index("--tools") + 1] == ""


def test_claude_passes_a_model_only_when_one_was_asked_for():
    assert "--model" not in ClaudeClient().command("x", Path("/c"), Path("/c/p"), None)
    with_model = ClaudeClient().command("x", Path("/c"), Path("/c/p"), "haiku")
    assert with_model[with_model.index("--model") + 1] == "haiku"


def test_claude_reads_tool_calls_and_the_final_answer():
    parsed = ClaudeClient().parse(jsonl(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__agent_notebook__open", "input": {"path": "a.ipynb"}}
        ]}},
        {"type": "result", "result": "done"},
    ))
    assert parsed.tool_calls == [("mcp__agent_notebook__open", {"path": "a.ipynb"})]
    assert parsed.text == "done"
    assert not parsed.unrecognised


# --- codex, the unverified path ----------------------------------------------


def test_codex_does_not_override_the_home_that_holds_its_credentials():
    """--ignore-user-config skips the config file but auth still comes from
    CODEX_HOME, so redirecting it would leave every run unauthenticated and
    hanging on an interactive login. Caught by reading the CLI's own help."""
    client = CodexClient()
    assert not hasattr(client, "environment"), (
        "no environment override, so CODEX_HOME is inherited"
    )
    assert "--ignore-user-config" in client.command("x", Path("/c"), Path("/c/p"), None)


def test_codex_runs_read_only_so_the_tools_are_the_only_way_to_write():
    """The nearest equivalent to Claude's --tools "". It is weaker: Codex can
    still read the notebook with its own shell, which is why every task also
    asserts the surface was used."""
    command = CodexClient().command("x", Path("/c"), Path("/c/p"), None)
    assert command[command.index("--sandbox") + 1] == "read-only"


def test_codex_is_told_about_the_server_on_the_command_line():
    command = CodexClient().command("x", Path("/c"), Path("/c/project"), None)
    joined = " ".join(command)
    assert "mcp_servers.agent_notebook.command=" in joined
    assert "--workspace-root" in joined
    assert command[-1] == "x", "the prompt goes last"


def test_codex_reads_an_mcp_tool_call():
    parsed = CodexClient().parse(jsonl(
        {"type": "thread.started"},
        {"type": "item.completed", "item": {
            "type": "mcp_tool_call", "server": "agent_notebook",
            "tool": "open", "arguments": {"path": "a.ipynb"}}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
    ))
    assert parsed.tool_calls == [("mcp__agent_notebook__open", {"path": "a.ipynb"})]
    assert parsed.text == "done"
    assert not parsed.unrecognised


def test_codex_tolerates_the_other_plausible_field_names():
    """The exact spelling could not be confirmed against a real transcript, so
    the parser accepts the alternatives rather than silently recording none."""
    parsed = CodexClient().parse(jsonl(
        {"type": "item.completed", "item": {
            "type": "mcp_tool_call", "tool_name": "run_cell",
            "arguments": '{"cell_id": "c1"}'}},
    ))
    assert parsed.tool_calls[0][0].endswith("run_cell")
    assert parsed.tool_calls[0][1] == {"cell_id": "c1"}, "a JSON string is decoded"


def test_a_codex_transcript_it_cannot_read_is_a_harness_failure():
    """The property this file exists for. Returning an empty trace here would
    score as "the model used no tools" and fail every check for the wrong
    reason; naming the event types turns the first real run into a schema
    discovery instead of a mystery."""
    parsed = CodexClient().parse(jsonl(
        {"type": "something.new", "payload": {"whatever": 1}},
        {"type": "another.thing"},
    ))
    assert parsed.unrecognised
    assert "something.new" in parsed.unrecognised
    assert parsed.tool_calls == []


def test_an_empty_transcript_is_not_reported_as_a_schema_problem():
    """A client that died before emitting anything is a different failure, and
    is already reported from its exit code."""
    assert not CodexClient().parse("").unrecognised
