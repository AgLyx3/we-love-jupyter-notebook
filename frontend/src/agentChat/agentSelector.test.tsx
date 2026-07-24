import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import AgentChatPanel from "./AgentChatPanel";
import type { AgentAdaptersResponse, NotebookSnapshot, TurnScope } from "../api/client";

const notebook: NotebookSnapshot = {
  sessionId: "s1", filename: "n.ipynb", revision: 1, dirty: false, metadata: {},
  nbformat: 4, nbformatMinor: 5,
  cells: [{ cellId: "cell-a", index: 0, cellType: "code", source: "print(1)", metadata: {}, outputs: [], executionCount: null }],
};
const scope: TurnScope = { editableCellIds: ["cell-a"], contextCellIds: [], sessionId: "s1", notebookRevision: 1 };

const adapters: AgentAdaptersResponse = {
  defaultAgent: "claude",
  agents: [
    { id: "claude", label: "Claude", modes: ["edit", "plan"], models: [
      { value: "default", label: "Default" }, { value: "opus", label: "Opus" },
      { value: "sonnet", label: "Sonnet" }, { value: "haiku", label: "Haiku" }] },
    { id: "codex", label: "Codex", modes: ["edit", "plan"], models: [
      { value: "default", label: "Default" }, { value: "gpt-5.5", label: "GPT-5.5" },
      { value: "gpt-5.4", label: "GPT-5.4" }, { value: "gpt-5.4-mini", label: "GPT-5.4 Mini" }] },
  ],
};

const baseProps = {
  notebook, scope, turn: null, activeTurn: null, history: [], operation: null, busy: false, mutationsDisabled: false,
  onCancel: vi.fn(), onUndo: vi.fn(), onClearScope: vi.fn(), onDecision: vi.fn(), onSelectTurn: vi.fn(), onFocusCell: vi.fn(), onDropCell: vi.fn(),
};

describe("AgentChatPanel agent selector", () => {
  it("switches model options with the agent and submits the selection", async () => {
    const onSubmit = vi.fn();
    render(<AgentChatPanel {...baseProps} agentAdapters={adapters} onSubmit={onSubmit} />);
    const agentSelect = screen.getByLabelText("Agent backend");
    expect(agentSelect).toHaveValue("claude");
    expect(screen.getByRole("option", { name: "Opus" })).toBeInTheDocument();
    fireEvent.change(agentSelect, { target: { value: "codex" } });
    expect(screen.getByRole("option", { name: "GPT-5.5" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Opus" })).not.toBeInTheDocument();
    const modelSelect = screen.getByLabelText("Agent model");
    fireEvent.change(modelSelect, { target: { value: "gpt-5.5" } });
    fireEvent.change(screen.getByLabelText("Agent instruction"), { target: { value: "hi" } });
    fireEvent.submit(screen.getByLabelText("Agent instruction").closest("form")!);
    expect(onSubmit).toHaveBeenCalledWith("hi", { agent: "codex", model: "gpt-5.5", mode: "edit", writeScope: "blocking" });
  });

  it("resets model to default when the agent changes", () => {
    render(<AgentChatPanel {...baseProps} agentAdapters={adapters} onSubmit={vi.fn()} />);
    const modelSelect = screen.getByLabelText("Agent model");
    fireEvent.change(modelSelect, { target: { value: "opus" } });
    expect(modelSelect).toHaveValue("opus");
    fireEvent.change(screen.getByLabelText("Agent backend"), { target: { value: "codex" } });
    expect(modelSelect).toHaveValue("default");
  });

  it("renders a single Default agent option before adapters load", () => {
    render(<AgentChatPanel {...baseProps} onSubmit={vi.fn()} />);
    const agentSelect = screen.getByLabelText("Agent backend");
    expect(agentSelect).toHaveValue("default");
    expect(screen.getAllByRole("option", { name: "Default" })).toHaveLength(2); // agent + model selects
  });
});
