import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import AgentChatPanel from "./agentChat/AgentChatPanel";
import FileToolbar from "./fileOperations/FileToolbar";
import type { NotebookSnapshot, TurnScope } from "./api/client";
import { EventSourceMock } from "./test/setup";

vi.mock("@uiw/react-codemirror", () => ({
  default: ({ value, onChange, "aria-label": label, onKeyDown }: { value: string; onChange: (value: string) => void; "aria-label": string; onKeyDown: (event: React.KeyboardEvent) => void }) =>
    <textarea aria-label={label} value={value} onChange={(event) => onChange(event.target.value)} onKeyDown={onKeyDown} />,
}));

const notebook: NotebookSnapshot = {
  sessionId: "session-1", filename: "sample.ipynb", revision: 3, dirty: false,
  metadata: {}, nbformat: 4, nbformatMinor: 5,
  cells: [
    { cellId: "intro", index: 0, cellType: "markdown", source: "# Example", metadata: {}, outputs: [], executionCount: null },
    { cellId: "code-1", index: 1, cellType: "code", source: "print('ok')", metadata: {}, outputs: [], executionCount: null },
  ],
};

function response(value: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }));
}

afterEach(() => vi.restoreAllMocks());

describe("Notebook editor", () => {
  it("shows a close control only for a loaded notebook", async () => {
    const onClose = vi.fn();
    const view = render(<FileToolbar notebook={notebook} onBrowse={vi.fn()} onSave={vi.fn()} onSaveAs={vi.fn()} onClose={onClose} />);
    const close = screen.getByRole("button", { name: "Close notebook" });
    expect(close).toHaveAttribute("title", "Close notebook");
    await userEvent.click(close);
    expect(onClose).toHaveBeenCalledOnce();

    view.rerender(<FileToolbar notebook={null} onBrowse={vi.fn()} onSave={vi.fn()} onSaveAs={vi.fn()} onClose={onClose} />);
    expect(screen.queryByRole("button", { name: "Close notebook" })).not.toBeInTheDocument();
  });

  it("shows an upload state when no notebook is loaded", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(() => response({ error: { code: "notebook_not_loaded", message: "No notebook is loaded", details: {} } }, 404));
    render(<App />);
    expect(screen.getByText("Loading notebook…")).toBeInTheDocument();
    expect(await screen.findByText("Open a notebook or a folder to begin")).toBeInTheDocument();
  });

  it("closes the current notebook and resets to the upload screen", async () => {
    const historical = {
      turnId: "turn-1", sessionId: notebook.sessionId, baseRevision: notebook.revision,
      prompt: "Persisted turn", editableCellIds: ["code-1"], contextCellIds: [],
      undoEligible: false, state: "failed", attempts: 1, finalOutput: null,
      appliedRevision: null, executionOperationId: null, changes: [], error: null,
      createdAt: "", completedAt: "", historyTruncated: false,
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current") && init?.method === "DELETE") return response({ closedSessionId: notebook.sessionId, cleanupErrors: [] });
      if (path.endsWith("/notebooks/current")) return response(notebook);
      if (path.endsWith("/turn-scope")) return response({ editableCellIds: ["code-1"], contextCellIds: [], sessionId: notebook.sessionId, notebookRevision: notebook.revision });
      if (path.endsWith("/kernel/status")) return response({ state: "idle", kernelSessionId: "kernel-1", executionAttemptId: null });
      if (path.endsWith("/session/status")) return response({ sessionId: notebook.sessionId, documentRevision: notebook.revision, activeTurn: null, activeExecution: null, turnHistory: [historical], turnHistoryTruncated: false });
      return response({});
    });
    render(<App />);
    expect(await screen.findByText("Persisted turn")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Close notebook" }));

    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/notebooks/current"), expect.objectContaining({
      method: "DELETE",
      body: JSON.stringify({ sessionId: notebook.sessionId, expectedDocumentRevision: notebook.revision }),
    }));
    expect(await screen.findByText("Open a notebook or a folder to begin")).toBeInTheDocument();
    expect(screen.queryByText("Persisted turn")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close notebook" })).not.toBeInTheDocument();
  });

  it("requires confirmation before discarding a dirty notebook", async () => {
    const dirtyNotebook = { ...notebook, dirty: true };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current") && init?.method === "DELETE") return response({ closedSessionId: notebook.sessionId, cleanupErrors: [] });
      if (path.endsWith("/notebooks/current")) return response(dirtyNotebook);
      if (path.endsWith("/turn-scope")) return response({ editableCellIds: [], contextCellIds: [], sessionId: notebook.sessionId, notebookRevision: notebook.revision });
      if (path.endsWith("/kernel/status")) return response({ state: "not_started", kernelSessionId: null, executionAttemptId: null });
      if (path.endsWith("/session/status")) return response({ sessionId: notebook.sessionId, documentRevision: notebook.revision, activeTurn: null, activeExecution: null, turnHistory: [], turnHistoryTruncated: false });
      return response({});
    });
    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Close notebook" }));
    const dialog = screen.getByRole("alertdialog", { name: "Discard unsaved notebook?" });
    expect(dialog).toHaveTextContent("Changes that have not been downloaded will be lost.");
    expect(screen.getByRole("button", { name: "Keep notebook" })).toHaveFocus();
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining("/notebooks/current"), expect.objectContaining({ method: "DELETE" }));

    await userEvent.click(screen.getByRole("button", { name: "Keep notebook" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByText("sample.ipynb")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Close notebook" }));
    await userEvent.click(screen.getByRole("button", { name: "Discard notebook" }));
    expect(await screen.findByText("Open a notebook or a folder to begin")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/notebooks/current"), expect.objectContaining({ method: "DELETE" }));
  });

  it("dismisses dirty close confirmation when the notebook is replaced", async () => {
    let current = { ...notebook, dirty: true };
    const replacement = { ...notebook, sessionId: "session-2", filename: "replacement.ipynb", revision: 0, dirty: false };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current") && init?.method === "DELETE") return response({ closedSessionId: current.sessionId, cleanupErrors: [] });
      if (path.endsWith("/notebooks/current")) return response(current);
      if (path.endsWith("/turn-scope")) return response({ editableCellIds: [], contextCellIds: [], sessionId: current.sessionId, notebookRevision: current.revision });
      if (path.endsWith("/kernel/status")) return response({ state: "not_started", kernelSessionId: null, executionAttemptId: null });
      if (path.endsWith("/session/status")) return response({ sessionId: current.sessionId, documentRevision: current.revision, activeTurn: null, activeExecution: null, turnHistory: [], turnHistoryTruncated: false });
      return response({});
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Close notebook" }));
    expect(screen.getByRole("alertdialog", { name: "Discard unsaved notebook?" })).toBeInTheDocument();

    current = replacement;
    EventSourceMock.instances[0].emit("notebook.updated", { revision: replacement.revision }, 1);

    expect(await screen.findByText("replacement.ipynb")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining("/notebooks/current"), expect.objectContaining({ method: "DELETE" }));
  });

  it("warns after closing when backend cleanup is incomplete", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current") && init?.method === "DELETE") return response({ closedSessionId: notebook.sessionId, cleanupErrors: ["kernel shutdown failed"] });
      if (path.endsWith("/notebooks/current")) return response(notebook);
      if (path.endsWith("/turn-scope")) return response({ editableCellIds: [], contextCellIds: [], sessionId: notebook.sessionId, notebookRevision: notebook.revision });
      if (path.endsWith("/kernel/status")) return response({ state: "idle", kernelSessionId: "kernel-1", executionAttemptId: null });
      if (path.endsWith("/session/status")) return response({ sessionId: notebook.sessionId, documentRevision: notebook.revision, activeTurn: null, activeExecution: null, turnHistory: [], turnHistoryTruncated: false });
      return response({});
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Close notebook" }));

    expect(await screen.findByText("Open a notebook or a folder to begin")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Notebook closed, but cleanup was incomplete: kernel shutdown failed");
  });

  it("keeps the notebook open and refreshes after a close conflict", async () => {
    let currentCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current") && init?.method === "DELETE") {
        return response({ error: { code: "revision_conflict", message: "Notebook revision does not match", details: { currentDocumentRevision: 4 } } }, 409);
      }
      if (path.endsWith("/notebooks/current")) { currentCalls += 1; return response({ ...notebook, revision: currentCalls === 1 ? 3 : 4 }); }
      if (path.endsWith("/turn-scope")) return response({ editableCellIds: [], contextCellIds: [], sessionId: notebook.sessionId, notebookRevision: currentCalls === 1 ? 3 : 4 });
      if (path.endsWith("/kernel/status")) return response({ state: "not_started", kernelSessionId: null, executionAttemptId: null });
      if (path.endsWith("/session/status")) return response({ sessionId: notebook.sessionId, documentRevision: currentCalls === 1 ? 3 : 4, activeTurn: null, activeExecution: null, turnHistory: [], turnHistoryTruncated: false });
      return response({});
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Close notebook" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Notebook changed elsewhere");
    expect(await screen.findByText("Revision 4")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close notebook" })).toBeEnabled();
    expect(screen.queryByText("Open a notebook or a folder to begin")).not.toBeInTheDocument();
  });

  it("adds cells to editable and context scope", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) return response(notebook);
      if (path.endsWith("/turn-scope") && (!init || init.method !== "DELETE")) return response({ editableCellIds: [], contextCellIds: [], sessionId: null, notebookRevision: null });
      if (path.endsWith("/editable-cells")) return response({ editableCellIds: ["code-1"], contextCellIds: [], sessionId: "session-1", notebookRevision: 3 });
      if (path.endsWith("/context-cells")) return response({ editableCellIds: ["code-1"], contextCellIds: ["intro"], sessionId: "session-1", notebookRevision: 3 });
      if (path.endsWith("/kernel/status")) return response({ state: "not_started", kernelSessionId: null });
      return response({});
    });
    render(<App />);
    await userEvent.click(await screen.findByLabelText("Allow agent edit code cell 2"));
    await userEvent.click(screen.getByLabelText("Add markdown cell 1 as context"));
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/turn-scope/editable-cells"), expect.objectContaining({ method: "POST" }));
    expect(await screen.findByText("1 editable")).toBeInTheDocument();
    expect(await screen.findByText("1 context")).toBeInTheDocument();
  });

  it("refreshes the notebook and reports a revision conflict", async () => {
    let currentCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) { currentCalls += 1; return response({ ...notebook, revision: currentCalls === 1 ? 3 : 4 }); }
      if (path.endsWith("/turn-scope")) return response({ editableCellIds: [], contextCellIds: [], sessionId: null, notebookRevision: null });
      if (path.endsWith("/kernel/status")) return response({ state: "not_started", kernelSessionId: null });
      if (path.includes("/source") && init?.method === "POST") return response({ error: { code: "revision_conflict", message: "Notebook revision does not match", details: { currentDocumentRevision: 4 } } }, 409);
      return response({});
    });
    render(<App />);
    const editor = await screen.findByLabelText("Source for code cell 2");
    await userEvent.click(editor);
    await userEvent.keyboard("{End} # changed");
    await userEvent.click(screen.getByLabelText("Save code cell 2"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Notebook changed elsewhere");
    await waitFor(() => expect(screen.getByText("Revision 4")).toBeInTheDocument());
    expect(editor).toHaveValue("print('ok') # changed");
    expect(screen.getByLabelText("Save code cell 2")).toBeEnabled();
  });

  it("preserves an unrelated unsaved draft when another cell save advances the revision", async () => {
    let current = {
      ...notebook,
      cells: [
        { ...notebook.cells[1], cellId: "code-a", index: 0, source: "a = 1" },
        { ...notebook.cells[1], cellId: "code-b", index: 1, source: "b = 1" },
      ],
    };
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) return response(current);
      if (path.endsWith("/turn-scope")) return response({ editableCellIds: [], contextCellIds: [], sessionId: null, notebookRevision: null });
      if (path.endsWith("/kernel/status")) return response({ state: "not_started", kernelSessionId: null });
      if (path.endsWith("/session/status")) return response({ sessionId: current.sessionId, documentRevision: current.revision, activeTurn: null, activeExecution: null, turnHistory: [] });
      if (path.includes("/cells/code-a/source") && init?.method === "POST") {
        current = { ...current, revision: 4, dirty: true, cells: current.cells.map((cell) => cell.cellId === "code-a" ? { ...cell, source: "a = 2" } : cell) };
        return response({ sessionId: current.sessionId, cellId: "code-a", source: "a = 2", revision: 4, dirty: true });
      }
      return response({});
    });
    render(<App />);
    const editorA = await screen.findByLabelText("Source for code cell 1");
    const editorB = screen.getByLabelText("Source for code cell 2");
    await userEvent.clear(editorA);
    await userEvent.type(editorA, "a = 2");
    await userEvent.clear(editorB);
    await userEvent.type(editorB, "b = unsaved");
    await userEvent.click(screen.getByLabelText("Save code cell 1"));
    await waitFor(() => expect(screen.getByText("Revision 4")).toBeInTheDocument());
    expect(editorA).toHaveValue("a = 2");
    expect(editorB).toHaveValue("b = unsaved");
  });

  it("renders a fully correlated risky execution approval", async () => {
    const riskyOperation = {
      operationId: "op-1", sessionId: "session-1", baseRevision: 3, currentDocumentRevision: 3, kind: "all", parentTurnId: "turn-1", state: "running", currentExecutionAttemptId: "attempt-1",
      attempts: [{ executionAttemptId: "attempt-1", cellId: "code-1", cellIndex: 1, sourcePreview: "os.system('echo guarded')", state: "awaiting_approval", risk: { level: "confirm", reasons: ["Runs a shell command"], matchedPatterns: ["os.system"] }, decision: null, outputs: [], executionCount: null, error: null }], error: null, createdAt: "", completedAt: null,
    };
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) return response(notebook);
      if (path.endsWith("/turn-scope")) return response({ editableCellIds: [], contextCellIds: [], sessionId: null, notebookRevision: null });
      if (path.endsWith("/kernel/status")) return response({ state: "idle", kernelSessionId: "kernel-1" });
      if (path.endsWith("/execution/run-all") && init?.method === "POST") return response(riskyOperation);
      if (path.includes("/execution/attempt-1/approve") && init?.method === "POST") return response({ ...riskyOperation, state: "running", attempts: [{ ...riskyOperation.attempts[0], state: "running", decision: "approve" }] });
      return response({});
    });
    render(<App />);
    await userEvent.click(await screen.findByLabelText("Run all cells"));
    expect(await screen.findByText("Execution needs approval")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve and run" }));
    expect(globalThis.fetch).toHaveBeenCalledWith(expect.stringContaining("/execution/attempt-1/approve"), expect.objectContaining({ body: JSON.stringify({ sessionId: "session-1", expectedDocumentRevision: 3, turnId: "turn-1", cellId: "code-1" }) }));
  });

  it("blocks source-dependent actions while a cell has an unsaved draft", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) return response(notebook);
      if (path.endsWith("/turn-scope")) return response({ editableCellIds: ["code-1"], contextCellIds: [], sessionId: "session-1", notebookRevision: 3 });
      if (path.endsWith("/kernel/status")) return response({ state: "idle", kernelSessionId: "kernel-1", executionAttemptId: null });
      if (path.endsWith("/session/status")) return response({ sessionId: "session-1", documentRevision: 3, activeTurn: null, activeExecution: null, turnHistory: [], turnHistoryTruncated: false });
      return response({});
    });
    render(<App />);
    const editor = await screen.findByLabelText("Source for code cell 2");
    await userEvent.type(editor, " # unsaved");
    await userEvent.type(screen.getByLabelText("Agent instruction"), "Use the visible source");

    expect(screen.getByLabelText("Save code cell 2")).toBeEnabled();
    expect(screen.getByLabelText("Run code cell 2")).toBeDisabled();
    expect(screen.getByLabelText("Run all cells")).toBeDisabled();
    expect(screen.getByLabelText("Allow agent edit code cell 2")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByLabelText("Save notebook as")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Close notebook" })).toBeDisabled();
    expect(screen.getByLabelText("Open a notebook or folder")).toBeEnabled();
  });

  it("renders raw cells literally with manual editing but no agent-edit permission", async () => {
    const rawNotebook = {
      ...notebook,
      cells: [{ cellId: "raw-1", index: 0, cellType: "raw", source: "# literal raw\n<not markdown>", metadata: {}, outputs: [], executionCount: null }],
    };
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) return response(rawNotebook);
      if (path.endsWith("/turn-scope")) return response({ editableCellIds: [], contextCellIds: [], sessionId: null, notebookRevision: null });
      if (path.endsWith("/kernel/status")) return response({ state: "not_started", kernelSessionId: null, executionAttemptId: null });
      if (path.endsWith("/session/status")) return response({ sessionId: "session-1", documentRevision: 3, activeTurn: null, activeExecution: null, turnHistory: [], turnHistoryTruncated: false });
      return response({});
    });
    render(<App />);

    expect(await screen.findByLabelText("raw cell 1")).toHaveTextContent("RAW");
    expect(screen.getByLabelText("Add raw cell 1 as context")).toBeEnabled();
    const editor = await screen.findByLabelText("Source for raw cell 1");
    expect(editor).toHaveValue("# literal raw\n<not markdown>");
    await userEvent.type(editor, "\nchanged");
    expect(screen.getByLabelText("Save raw cell 1")).toBeEnabled();
    expect(screen.getByLabelText("Allow agent edit raw cell 1")).toBeDisabled();
    expect(screen.getByLabelText("Add raw cell 1 as context")).toBeDisabled();
  });

  it("submits an agent instruction with Enter", async () => {
    const onSubmit = vi.fn();
    renderAgentPanel(onSubmit);
    const prompt = screen.getByLabelText("Agent instruction");

    await userEvent.type(prompt, "  Update the selected cell{Enter}");

    expect(onSubmit).toHaveBeenCalledWith("Update the selected cell");
    expect(prompt).toHaveValue("");
  });

  it("uses Shift+Enter for a newline without submitting", async () => {
    const onSubmit = vi.fn();
    renderAgentPanel(onSubmit);
    const prompt = screen.getByLabelText("Agent instruction");

    await userEvent.type(prompt, "first{Shift>}{Enter}{/Shift}second");

    expect(onSubmit).not.toHaveBeenCalled();
    expect(prompt).toHaveValue("first\nsecond");
  });
});

function renderAgentPanel(onSubmit: (prompt: string) => void) {
  const scope: TurnScope = {
    editableCellIds: ["code-1"], contextCellIds: [],
    sessionId: "session-1", notebookRevision: 3,
  };
  render(<AgentChatPanel
    notebook={notebook} scope={scope} turn={null} activeTurn={null} history={[]}
    operation={null} busy={false} mutationsDisabled={false} onSubmit={onSubmit}
    onCancel={() => {}} onUndo={() => {}} onClearScope={() => {}}
    onDecision={() => {}} onSelectTurn={() => {}} onFocusCell={() => {}}
    onDropCell={() => {}}
  />);
}
