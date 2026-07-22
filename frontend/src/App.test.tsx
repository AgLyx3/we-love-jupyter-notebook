import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";

vi.mock("@uiw/react-codemirror", () => ({
  default: ({ value, onChange, "aria-label": label, onKeyDown }: { value: string; onChange: (value: string) => void; "aria-label": string; onKeyDown: (event: React.KeyboardEvent) => void }) =>
    <textarea aria-label={label} value={value} onChange={(event) => onChange(event.target.value)} onKeyDown={onKeyDown} />,
}));

const notebook = {
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
  it("shows an upload state when no notebook is loaded", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(() => response({ error: { code: "notebook_not_loaded", message: "No notebook is loaded", details: {} } }, 404));
    render(<App />);
    expect(screen.getByText("Loading notebook…")).toBeInTheDocument();
    expect(await screen.findByText("Open a notebook to begin")).toBeInTheDocument();
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
    expect(editor).toHaveValue("print('ok')");
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
});
