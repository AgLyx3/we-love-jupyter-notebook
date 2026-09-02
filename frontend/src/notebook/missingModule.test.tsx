/** Saying what to do about `ModuleNotFoundError` in the cell that failed (#52).
 *
 *  The traceback is correct and useless on its own: the same error means the
 *  environment running the cells lacks a package, or that it is not the
 *  environment that was meant, and those have opposite fixes. What tells them
 *  apart is the interpreter — so these tests are mostly about the block naming
 *  it, and about the cases where naming it would be a guess.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Outputs } from "./NotebookCell";
import NotebookView from "./NotebookView";
import { installCommand, missingModule } from "./missingModule";
import type { KernelStatus, NotebookSnapshot, TurnScope } from "../api/client";

vi.mock("@uiw/react-codemirror", () => ({
  default: ({ value, "aria-label": label }: { value: string; "aria-label": string }) =>
    <textarea aria-label={label} value={value} readOnly />,
}));

const kernel: KernelStatus = {
  kernelSessionId: "kernel-1", state: "idle", executionAttemptId: null,
  interpreter: "/proj/.venv/bin/python", interpreterSource: "kernelspec",
};

const error = (ename: string, evalue: string) =>
  ({ output_type: "error", ename, evalue, traceback: [`${ename}: ${evalue}`] });

const missingPandas = error("ModuleNotFoundError", "No module named 'pandas'");

describe("reading a ModuleNotFoundError", () => {
  it("names the module and the distribution that provides it", () => {
    expect(missingModule("ModuleNotFoundError", "No module named 'pandas'"))
      .toEqual({ module: "pandas", packageName: "pandas", known: false });
    expect(missingModule("ModuleNotFoundError", "No module named 'sklearn'"))
      .toEqual({ module: "sklearn", packageName: "scikit-learn", known: true });
  });

  it("does not read a package name off Object.prototype", () => {
    // `constructor` is a real installable module, and a plain `table[name]`
    // would hand back a function to print as the install command.
    expect(missingModule("ModuleNotFoundError", "No module named 'constructor'"))
      .toEqual({ module: "constructor", packageName: "constructor", known: false });
  });

  it("quotes an interpreter path a shell would otherwise split", () => {
    // A venv under "~/My Projects" would run `/Users/me/My` and fail — the
    // outcome the whole block is meant to avoid.
    const missing = missingModule("ModuleNotFoundError", "No module named 'pandas'")!;
    expect(installCommand("/Users/me/My Projects/.venv/bin/python", missing))
      .toBe("'/Users/me/My Projects/.venv/bin/python' -m pip install pandas");
  });

  it("leaves ImportError alone", () => {
    // `from x import y` where x imports fine is not fixed by installing x.
    expect(missingModule("ImportError", "cannot import name 'y' from 'x'")).toBeNull();
  });

  it("leaves a submodule alone", () => {
    // CPython names the first component it could not find, so a dotted name
    // means the distribution is already installed.
    expect(missingModule("ModuleNotFoundError", "No module named 'pandas.nonesuch'")).toBeNull();
  });

  it("runs pip through the interpreter rather than a guessed sibling pip", () => {
    const missing = missingModule("ModuleNotFoundError", "No module named 'pandas'")!;
    expect(installCommand("/proj/.venv/bin/python", missing))
      .toBe("/proj/.venv/bin/python -m pip install pandas");
  });
});

describe("the remediation under a failed cell", () => {
  it("names the environment that looked and the command that installs into it", () => {
    render(<Outputs outputs={[missingPandas]} kernel={kernel} />);
    const note = screen.getByRole("note", { name: "Missing module" });
    expect(note).toHaveTextContent("pandas is not installed in the environment your cells run in, /proj/.venv/bin/python");
    expect(note).toHaveTextContent("/proj/.venv/bin/python -m pip install pandas");
  });

  it("offers the other fix when nobody chose the interpreter", () => {
    // The case that goes wrong: the editor's own environment won the
    // kernelspec lookup, so installing may be the wrong move entirely.
    render(<Outputs outputs={[missingPandas]} kernel={kernel} />);
    expect(screen.getByRole("note", { name: "Missing module" }))
      .toHaveTextContent("restart the editor with --kernel-python");
  });

  it("does not offer that fix when --kernel-python was already used", () => {
    render(<Outputs outputs={[missingPandas]} kernel={{ ...kernel, interpreterSource: "kernel-python" }} />);
    const note = screen.getByRole("note", { name: "Missing module" });
    expect(note).toHaveTextContent("chosen with --kernel-python");
    expect(note).not.toHaveTextContent("restart the editor");
  });

  it("says which package a mismatched module comes from", () => {
    render(<Outputs outputs={[error("ModuleNotFoundError", "No module named 'sklearn'")]} kernel={kernel} />);
    const note = screen.getByRole("note", { name: "Missing module" });
    expect(note).toHaveTextContent("It comes from the scikit-learn package");
    expect(note).toHaveTextContent("-m pip install scikit-learn");
  });

  it("does not claim a package exists for a module it does not know", () => {
    // `import helpers` usually fails because somebody's own file is not on the
    // path. `helpers` is on PyPI, so asserting the install is the fix would
    // pull in a stranger's package and bury the real cause.
    render(<Outputs outputs={[error("ModuleNotFoundError", "No module named 'helpers'")]} kernel={kernel} />);
    const note = screen.getByRole("note", { name: "Missing module" });
    expect(note).toHaveTextContent("If it comes from a package rather than a file of your own");
    expect(note).not.toHaveTextContent("It comes from the");
  });

  it("says nothing about a traceback no kernel here produced", () => {
    // A .ipynb stores its outputs, so a notebook can arrive carrying somebody
    // else's ModuleNotFoundError. `NotebookCell` withholds the kernel for a
    // cell this tab has not run, and without it there is nothing to assert —
    // the module name would come from the file, and the `pip install` built
    // from it is something a person may paste.
    render(<Outputs outputs={[missingPandas]} />);
    expect(screen.queryByRole("note", { name: "Missing module" })).not.toBeInTheDocument();
  });

  it("stays out of the way of every other error", () => {
    render(<Outputs outputs={[error("NameError", "name 'x' is not defined")]} kernel={kernel} />);
    expect(screen.queryByRole("note", { name: "Missing module" })).not.toBeInTheDocument();
  });

  it("says nothing when the interpreter is unknown", () => {
    // With no path there is no environment to name and no command to give:
    // the block would only restate the traceback above it.
    render(<Outputs outputs={[missingPandas]} kernel={{ ...kernel, interpreter: null }} />);
    expect(screen.queryByRole("note", { name: "Missing module" })).not.toBeInTheDocument();
  });

  it("stays visible while mutations are disabled", () => {
    // It is something to read, not something to press. A turn in flight hides
    // the Add-to-chat button and does not make the environment less true.
    render(<Outputs outputs={[missingPandas]} kernel={kernel} disabled onAddErrorToChat={vi.fn()} />);
    expect(screen.getByRole("note", { name: "Missing module" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add error to agent chat" })).not.toBeInTheDocument();
  });

  it("hands the agent the same environment fact when the error is added to chat", async () => {
    // Otherwise the agent gets the bare traceback and has to guess an
    // interpreter — the failure this whole thing is about.
    const onAddErrorToChat = vi.fn();
    render(<Outputs outputs={[missingPandas]} kernel={kernel} onAddErrorToChat={onAddErrorToChat} />);
    await userEvent.click(screen.getByRole("button", { name: "Add error to agent chat" }));
    const text = onAddErrorToChat.mock.calls[0][0] as string;
    expect(text).toContain("No module named 'pandas'");
    expect(text).toContain("/proj/.venv/bin/python -m pip install pandas");
  });
});

describe("which outputs the remediation is willing to speak about", () => {
  // The load-bearing check. A .ipynb carries its outputs, so a notebook from
  // anywhere can arrive with a fabricated `ModuleNotFoundError` naming a
  // package of its author's choosing. The remediation ends in a `pip install`
  // a person may paste, so it must only ever describe a failure this kernel
  // actually produced — not one the file was written with.
  const snapshot = (): NotebookSnapshot => ({
    sessionId: "session-1", filename: "sample.ipynb", revision: 1, dirty: false,
    metadata: {}, nbformat: 4, nbformatMinor: 5,
    cells: [{
      cellId: "code-1", index: 0, cellType: "code", source: "import pandas", metadata: {},
      outputs: [missingPandas], executionCount: 1,
    }],
  });
  const scope: TurnScope = { editableCellIds: [], contextCellIds: [], sessionId: "session-1", notebookRevision: 1 };
  const view = (ranCellIds: ReadonlySet<string>) =>
    <NotebookView notebook={snapshot()} scope={scope} turn={null} kernel={kernel} ranCellIds={ranCellIds}
      disabled={false} sourceActionsDisabled={false} autoSave={false} focusRequest={null}
      onDirtyChange={vi.fn()} onSave={vi.fn()} onRun={vi.fn()} onScope={vi.fn()} onScopeMany={vi.fn()} onRevert={vi.fn()} />;

  it("stays silent about an output the notebook arrived with", () => {
    render(view(new Set()));
    expect(screen.getByText(/No module named 'pandas'/)).toBeInTheDocument();
    expect(screen.queryByRole("note", { name: "Missing module" })).not.toBeInTheDocument();
  });

  it("speaks about an output this tab's kernel produced", () => {
    render(view(new Set(["code-1"])));
    expect(screen.getByRole("note", { name: "Missing module" }))
      .toHaveTextContent("/proj/.venv/bin/python -m pip install pandas");
  });

  // The set itself is filled in App, and a set that never filled would leave
  // every test here passing while the feature appeared for nobody. That is
  // pinned in App.test.tsx, where the App harness already lives.
});
