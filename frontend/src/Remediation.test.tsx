import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import AgentChatPanel, { type TurnRecord } from "./agentChat/AgentChatPanel";
import NotebookView from "./notebook/NotebookView";
import { Outputs } from "./notebook/NotebookCell";
import RiskyExecutionDialog from "./execution/RiskyExecutionDialog";
import type { AgentTurn, ExecutionOperation, NotebookSnapshot, TurnScope } from "./api/client";
import { EventSourceMock } from "./test/setup";

vi.mock("@uiw/react-codemirror", () => ({
  default: ({ value, onChange, "aria-label": label, readOnly }: { value: string; onChange: (value: string) => void; "aria-label": string; readOnly: boolean }) =>
    <textarea aria-label={label} value={value} disabled={readOnly} onChange={(event) => onChange(event.target.value)} />,
}));

const notebook: NotebookSnapshot = {
  sessionId: "session-1", filename: "sample.ipynb", revision: 3, dirty: false, metadata: {}, nbformat: 4, nbformatMinor: 5,
  cells: [{ cellId: "code-1", index: 0, cellType: "code", source: "a = 1\nprint(a)", metadata: {}, outputs: [], executionCount: null }],
};
const scope: TurnScope = { editableCellIds: ["code-1"], contextCellIds: [], sessionId: "session-1", notebookRevision: 3 };
const operation: ExecutionOperation = {
  operationId: "op-1", sessionId: "session-1", baseRevision: 3, currentDocumentRevision: 7, kind: "manual", parentTurnId: null, state: "running", currentExecutionAttemptId: "attempt-1",
  attempts: [{ executionAttemptId: "attempt-1", cellId: "code-1", cellIndex: 0, sourcePreview: "print('preview')", state: "running", risk: { level: "safe", reasons: [], matchedPatterns: [] }, decision: null, outputs: [], outputsTruncated: false, executionCount: null, error: null }], error: null, createdAt: "", completedAt: null,
};
const turn = (id: string, state = "completed"): AgentTurn => ({ turnId: id, sessionId: "session-1", baseRevision: 3, prompt: `Prompt ${id}`, editableCellIds: ["code-1"], contextCellIds: [], undoEligible: state === "completed", state, attempts: 1, finalOutput: "Done", appliedRevision: state === "completed" ? 4 : null, executionOperationId: null, changes: [], error: null, createdAt: "", completedAt: state === "completed" ? "" : null });

function json(value: unknown, status = 200) { return Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } })); }
function baseFetch(input: RequestInfo | URL, init?: RequestInit) {
  const path = String(input);
  if (path.endsWith("/notebooks/current")) return json(notebook);
  if (path.endsWith("/turn-scope")) return json(scope);
  if (path.endsWith("/kernel/status")) return json({ state: "busy", kernelSessionId: "kernel-1", executionAttemptId: "attempt-1" });
  if (path.endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: 3, activeTurn: null, activeExecution: operation });
  if (path.endsWith("/execution/attempt-1/cancel") && init?.method === "POST") return json({ ...operation, state: "cancelled" });
  return json({});
}

afterEach(() => vi.restoreAllMocks());

// The App opens its EventSource in an effect that may not have run yet when a
// test first reaches for it; wait for it instead of racing the render.
async function firstEventSource() {
  await waitFor(() => expect(EventSourceMock.instances.length).toBeGreaterThan(0));
  return EventSourceMock.instances[0];
}

describe("remediation behaviors", () => {
  it("locks mutations during a recovered active operation and cancels it with immutable correlation", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(baseFetch);
    render(<App />);
    expect(await screen.findByLabelText("Run code cell 1")).toBeDisabled();
    expect(screen.getByLabelText("Allow agent edit code cell 1")).toBeDisabled();
    expect(screen.getByLabelText("Save notebook as")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Close notebook" })).toBeDisabled();
    expect(screen.getByLabelText("Open a notebook or folder")).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel run" }));
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/execution/attempt-1/cancel"), expect.objectContaining({ body: JSON.stringify({ sessionId: "session-1", expectedDocumentRevision: 7, turnId: null, cellId: "code-1" }) }));
  });

  it("keeps cancel available for the exact agent_running state", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: 3, activeTurn: turn("active", "agent_running"), activeExecution: null });
      if (path.endsWith("/agent-turns/active/cancel") && init?.method === "POST") return json(turn("active", "cancelled"));
      return baseFetch(input, init);
    });
    render(<App />);
    expect(await screen.findByText("agent running")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel turn" })).toBeEnabled();
    expect(screen.getByLabelText("Source for code cell 1")).toBeDisabled();
  });

  it("shows frozen scope for every turn and selects historical outcomes", async () => {
    const history: TurnRecord[] = [
      { turn: turn("two", "failed"), prompt: "Second prompt", editableCellIds: ["code-1"], contextCellIds: [] },
      { turn: turn("one"), prompt: "First prompt", editableCellIds: ["code-1"], contextCellIds: ["code-1"] },
    ];
    const select = vi.fn();
    render(<AgentChatPanel notebook={notebook} scope={scope} turn={history[0].turn} activeTurn={null} history={history} operation={null} busy={false} mutationsDisabled={false} onSubmit={vi.fn()} onCancel={vi.fn()} onUndo={vi.fn()} onClearScope={vi.fn()} onDecision={vi.fn()} onSelectTurn={select} onFocusCell={vi.fn()} onDropCell={vi.fn()} />);
    expect(screen.getByText("1 editable · failed")).toBeInTheDocument();
    await userEvent.click(screen.getByText("First prompt"));
    expect(select).toHaveBeenCalledWith("one");
  });

  it("keeps one EventSource through refresh events and uses monotonic event IDs", async () => {
    let currentCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => { if (String(input).endsWith("/notebooks/current")) currentCalls += 1; return baseFetch(input, init); });
    render(<App />);
    await screen.findByText("sample.ipynb");
    await waitFor(() => expect(EventSourceMock.instances).toHaveLength(1));
    expect(EventSourceMock.instances[0].url).toContain("after=0");
    EventSourceMock.instances[0].emit("notebook.updated", { revision: 4 }, 4);
    EventSourceMock.instances[0].emit("notebook.updated", { revision: 3 }, 3);
    EventSourceMock.instances[0].emit("notebook.updated", { revision: 7 }, 7);
    await waitFor(() => expect(currentCalls).toBeGreaterThanOrEqual(4));
    EventSourceMock.instances[0].onerror?.();
    EventSourceMock.instances[0].onopen?.();
    expect(EventSourceMock.instances).toHaveLength(1);
  });

  it("focuses and scrolls the same scoped cell for every click", async () => {
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => undefined });
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(() => undefined);
    vi.spyOn(globalThis, "fetch").mockImplementation(baseFetch);
    render(<App />);
    const item = await screen.findByTitle("Cell ID: code-1");
    await userEvent.click(item);
    await waitFor(() => expect(scroll).toHaveBeenCalledTimes(1));
    await userEvent.click(item);
    await waitFor(() => expect(scroll).toHaveBeenCalledTimes(2));
    expect((focus.mock.contexts as HTMLElement[]).filter((node) => node.classList.contains("notebook-cell"))).toHaveLength(2);
  });

  it("hydrates completed turn history from session status after reload", async () => {
    const historical = { ...turn("persisted"), prompt: "Persisted backend turn", editableCellIds: ["code-1"], contextCellIds: ["code-1"] };
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      if (String(input).endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: 3, activeTurn: null, activeExecution: null, turnHistory: [historical] });
      return baseFetch(input, init);
    });
    render(<App />);
    expect(await screen.findByText("Persisted backend turn")).toBeInTheDocument();
  });

  it("hydrates a truncated selected turn before showing its large diff", async () => {
    const prefix = Array.from({ length: 400 }, (_, index) => `shared line ${index}`).join("\n");
    const previousSource = `${prefix}\nold ending`;
    const nextSource = `${prefix}\nfull detail ending`;
    const detailed = {
      ...turn("large"),
      changes: [{ cellId: "code-1", previousSource, nextSource }],
      historyTruncated: false,
    };
    const summary = {
      ...detailed,
      changes: [{ cellId: "code-1", previousSource: previousSource.slice(0, 2048) + "...", nextSource: nextSource.slice(0, 2048) + "..." }],
      historyTruncated: true,
    };
    const current = { ...notebook, cells: [{ ...notebook.cells[0], source: nextSource }] };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) return json(current);
      if (path.endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: 3, activeTurn: null, activeExecution: null, turnHistory: [summary] });
      if (path.endsWith("/agent-turns/large")) return json(detailed);
      return baseFetch(input, init);
    });
    render(<App />);
    expect(await screen.findByLabelText("Revert agent change to code cell 1")).toBeInTheDocument();
    expect(screen.getByLabelText("Source for code cell 1")).toHaveValue(nextSource);
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/agent-turns/large"), expect.anything());
  });

  // The revert control used to live in .cell-actions, which is opacity:0 until
  // the cell is hovered — present in the DOM and clickable, but invisible. jsdom
  // loads no stylesheet (styles.css is imported only by main.tsx), so
  // toBeVisible() cannot catch that regression. Assert structurally instead:
  // the control must sit in the persistent review bar, not the hover cluster.
  it("puts the agent-change undo control in a persistent labelled bar, not the hover-only action cluster", async () => {
    const nextSource = "print('agent wrote this')";
    const changed = {
      ...turn("persistent"),
      appliedRevision: 3,
      changes: [{ cellId: "code-1", previousSource: notebook.cells[0].source, nextSource }],
    };
    const current = { ...notebook, cells: [{ ...notebook.cells[0], source: nextSource }] };
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) return json(current);
      if (path.endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: 3, activeTurn: null, activeExecution: null, turnHistory: [changed] });
      if (path.endsWith("/agent-turns/persistent")) return json(changed);
      return baseFetch(input, init);
    });
    render(<App />);

    const undo = await screen.findByLabelText("Revert agent change to code cell 1");
    expect(undo.closest(".cell-actions")).toBeNull();
    expect(undo.closest(".cell-review")).not.toBeNull();
    // Labelled, not icon-only — the other reason it was unfindable.
    expect(undo).toHaveTextContent("Undo");
    expect(screen.getByText("Agent changed this cell")).toBeInTheDocument();
  });

  it("rehydrates authoritative outcome and error after a truncated status advances", async () => {
    const prefix = Array.from({ length: 400 }, (_, index) => `shared line ${index}`).join("\n");
    const previousSource = `${prefix}\nold ending`;
    const nextSource = `${prefix}\nfull detail ending`;
    const initialDetail = {
      ...turn("advancing", "agent_running"),
      finalOutput: "Still running",
      changes: [{ cellId: "code-1", previousSource, nextSource }],
      historyTruncated: false,
    };
    const fullOutcome = `${"completed output ".repeat(600)}full outcome tail`;
    const fullError = `${"validation detail ".repeat(300)}full error tail`;
    const completedDetail = {
      ...initialDetail,
      state: "failed",
      finalOutput: fullOutcome,
      error: { code: "validation_failed", message: fullError, details: { stage: "execution" } },
      completedAt: "2026-07-22T00:00:00Z",
    };
    const completedSummary = {
      ...completedDetail,
      finalOutput: fullOutcome.slice(0, 8189) + "...",
      error: { code: "validation_failed", message: fullError.slice(0, 4093) + "...", details: {} },
      changes: [{ cellId: "code-1", previousSource: previousSource.slice(0, 2048) + "...", nextSource: nextSource.slice(0, 2048) + "..." }],
      historyTruncated: true,
    };
    const current = { ...notebook, cells: [{ ...notebook.cells[0], source: nextSource }] };
    let completed = false;
    let detailCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) return json(current);
      if (path.endsWith("/session/status")) {
        const record = completed ? completedSummary : initialDetail;
        return json({ sessionId: "session-1", documentRevision: 3, activeTurn: completed ? null : record, activeExecution: null, turnHistory: [record] });
      }
      if (path.endsWith("/agent-turns/advancing")) {
        detailCalls += 1;
        return json(completed ? completedDetail : initialDetail);
      }
      return baseFetch(input, init);
    });
    render(<App />);
    expect(await screen.findByText("agent running")).toBeInTheDocument();
    completed = true;
    (await firstEventSource()).emit("notebook.updated", { revision: 3 }, 1);
    expect(await screen.findByText("failed")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText((_, element) => element?.tagName === "P" && element.textContent?.endsWith("full outcome tail") === true)).toBeInTheDocument());
    expect(screen.getByText((_, element) => element?.classList.contains("error-text") === true && element.textContent?.endsWith("full error tail") === true)).toBeInTheDocument();
    expect(await screen.findByLabelText("Revert agent change to code cell 1")).toBeInTheDocument();
    expect(screen.getByLabelText("Source for code cell 1")).toHaveValue(nextSource);
    expect(detailCalls).toBeGreaterThanOrEqual(1);
  });

  it("refreshes turn eligibility and clears inline changes after undo", async () => {
    const applied = {
      ...turn("undoable"),
      changes: [{ cellId: "code-1", previousSource: "a = 1\nprint(a)", nextSource: "a = 2\nprint(a)" }],
      historyTruncated: false,
    };
    let current = { ...notebook, cells: [{ ...notebook.cells[0], source: applied.changes[0].nextSource }] };
    let undone = false;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) return json(current);
      if (path.endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: current.revision, activeTurn: null, activeExecution: null, turnHistory: [{ ...applied, undoEligible: !undone }] });
      if (path.endsWith("/agent-turns/undoable/undo") && init?.method === "POST") {
        undone = true;
        current = { ...notebook, revision: 4, cells: [{ ...notebook.cells[0], source: applied.changes[0].previousSource, executionCount: 7, outputs: [{ output_type: "stream", text: "restored output" }] }] };
        return json(current);
      }
      return baseFetch(input, init);
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Undo entire turn" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Undo entire turn" })).not.toBeInTheDocument());
    expect(screen.queryByLabelText("Revert agent change to code cell 1")).not.toBeInTheDocument();
    expect(screen.getByText("Revision 4")).toBeInTheDocument();
    expect(screen.getByLabelText("Source for code cell 1")).toHaveValue(applied.changes[0].previousSource);
    expect(screen.getByLabelText("Cell output")).toHaveTextContent("restored output");
    expect(screen.getByLabelText("code cell 1")).toHaveTextContent("[7]");
  });

  it("removes a reverted cell change so it cannot be submitted twice", async () => {
    const applied = {
      ...turn("revertible"),
      undoEligible: false,
      changes: [{ cellId: "code-1", previousSource: "a = 1\nprint(a)", nextSource: "a = 2\nprint(a)" }],
      historyTruncated: false,
    };
    let current = { ...notebook, cells: [{ ...notebook.cells[0], source: applied.changes[0].nextSource }] };
    let revertCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) return json(current);
      if (path.endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: current.revision, activeTurn: null, activeExecution: null, turnHistory: [applied] });
      if (path.endsWith("/agent-turns/revertible/cells/code-1/revert") && init?.method === "POST") {
        revertCalls += 1;
        current = { ...notebook, revision: 4 };
        return json(current);
      }
      return baseFetch(input, init);
    });
    render(<App />);
    await userEvent.click(await screen.findByLabelText("Revert agent change to code cell 1"));
    await waitFor(() => expect(screen.queryByLabelText("Revert agent change to code cell 1")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Source for code cell 1")).toHaveValue(applied.changes[0].previousSource);
    expect(revertCalls).toBe(1);
  });

  it("ignores late refreshes and clears absent active operations", async () => {
    let currentCall = 0; let statusCall = 0;
    let resolveOlder!: (value: Response) => void; let resolveNewer!: (value: Response) => void;
    const older = new Promise<Response>((resolve) => { resolveOlder = resolve; });
    const newer = new Promise<Response>((resolve) => { resolveNewer = resolve; });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) { currentCall += 1; if (currentCall === 2) return older; if (currentCall === 3) return newer; return json(notebook); }
      if (path.endsWith("/session/status")) { statusCall += 1; return json({ sessionId: "session-1", documentRevision: statusCall === 1 ? 3 : 5, activeTurn: null, activeExecution: statusCall === 1 ? operation : null, turnHistory: [] }); }
      return baseFetch(input, init);
    });
    render(<App />);
    await screen.findByText("Revision 3");
    expect(screen.getByLabelText("Run code cell 1")).toBeDisabled();
    const source = await firstEventSource();
    source.emit("notebook.updated", { revision: 4 }, 1);
    source.emit("notebook.updated", { revision: 5 }, 2);
    resolveNewer(new Response(JSON.stringify({ ...notebook, revision: 5 }), { headers: { "Content-Type": "application/json" } }));
    await waitFor(() => expect(screen.getByText("Revision 5")).toBeInTheDocument());
    expect(screen.getByLabelText("Run code cell 1")).toBeEnabled();
    resolveOlder(new Response(JSON.stringify({ ...notebook, revision: 4 }), { headers: { "Content-Type": "application/json" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText("Revision 5")).toBeInTheDocument();
  });

  it("does not let a refresh started before run-cell clear the created operation", async () => {
    let currentCall = 0; let resolveRefresh!: (value: Response) => void;
    const pendingRefresh = new Promise<Response>((resolve) => { resolveRefresh = resolve; });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) { currentCall += 1; return currentCall === 2 ? pendingRefresh : json(notebook); }
      if (path.endsWith("/execution/cells/code-1/run") && init?.method === "POST") return json(operation);
      if (path.endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: 3, activeTurn: null, activeExecution: null, turnHistory: [] });
      return baseFetch(input, init);
    });
    render(<App />);
    await screen.findByText("Revision 3");
    (await firstEventSource()).emit("notebook.updated", { revision: 3 }, 1);
    await userEvent.click(screen.getByLabelText("Run code cell 1"));
    expect(await screen.findByText("Execution: running")).toBeInTheDocument();
    resolveRefresh(new Response(JSON.stringify(notebook), { headers: { "Content-Type": "application/json" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText("Execution: running")).toBeInTheDocument();
    expect(screen.getByLabelText("Run code cell 1")).toBeDisabled();
  });

  it("does not let a refresh started before start-turn clear the created turn", async () => {
    let currentCall = 0; let resolveRefresh!: (value: Response) => void;
    const pendingRefresh = new Promise<Response>((resolve) => { resolveRefresh = resolve; });
    const active = turn("created-turn", "agent_running");
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) { currentCall += 1; return currentCall === 2 ? pendingRefresh : json(notebook); }
      if (path.endsWith("/agent-turns") && init?.method === "POST") return json(active);
      if (path.endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: 3, activeTurn: null, activeExecution: null, turnHistory: [] });
      return baseFetch(input, init);
    });
    render(<App />);
    await screen.findByText("Revision 3");
    (await firstEventSource()).emit("notebook.updated", { revision: 3 }, 1);
    await userEvent.type(screen.getByLabelText("Agent instruction"), "Update selected cell");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("agent running")).toBeInTheDocument();
    resolveRefresh(new Response(JSON.stringify(notebook), { headers: { "Content-Type": "application/json" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText("agent running")).toBeInTheDocument();
    expect(screen.getByLabelText("Run code cell 1")).toBeDisabled();
  });

  it("keeps a newer execution fetch when an older aggregate refresh finishes", async () => {
    let currentCall = 0; let resolveRefresh!: (value: Response) => void;
    const pendingRefresh = new Promise<Response>((resolve) => { resolveRefresh = resolve; });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) { currentCall += 1; return currentCall === 2 ? pendingRefresh : json(notebook); }
      if (path.endsWith("/execution/op-1")) return json(operation);
      if (path.endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: 3, activeTurn: null, activeExecution: null, turnHistory: [] });
      return baseFetch(input, init);
    });
    render(<App />);
    await screen.findByText("Revision 3");
    const source = await firstEventSource();
    source.emit("notebook.updated", { revision: 3 }, 1);
    source.emit("execution.updated", { operationId: "op-1" }, 2);
    expect(await screen.findByText("Execution: running")).toBeInTheDocument();
    resolveRefresh(new Response(JSON.stringify(notebook), { headers: { "Content-Type": "application/json" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText("Execution: running")).toBeInTheDocument();
  });

  it("does not let a delayed kernel status overwrite terminal execution state", async () => {
    const running = { ...operation, state: "running" };
    const completed = { ...operation, state: "completed", completedAt: "2026-07-22T00:00:00Z" };
    let executionCalls = 0;
    let kernelCalls = 0;
    let resolveOldKernel!: (value: Response) => void;
    const oldKernel = new Promise<Response>((resolve) => { resolveOldKernel = resolve; });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/execution/op-kernel-race")) {
        executionCalls += 1;
        return json(executionCalls === 1 ? running : completed);
      }
      if (path.endsWith("/kernel/status")) {
        kernelCalls += 1;
        if (kernelCalls === 2) return oldKernel;
        return json(kernelCalls >= 3 ? { state: "idle", kernelSessionId: "kernel-1", executionAttemptId: null } : { state: "busy", kernelSessionId: "kernel-1", executionAttemptId: "attempt-1" });
      }
      if (path.endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: 3, activeTurn: null, activeExecution: executionCalls >= 2 ? null : running, turnHistory: [], turnHistoryTruncated: false });
      return baseFetch(input, init);
    });
    render(<App />);
    expect(await screen.findByText("Kernel busy")).toBeInTheDocument();
    const source = await firstEventSource();
    source.emit("execution.updated", { operationId: "op-kernel-race" }, 1);
    await waitFor(() => expect(kernelCalls).toBe(2));
    source.emit("execution.updated", { operationId: "op-kernel-race" }, 2);
    expect(await screen.findByText("Kernel idle")).toBeInTheDocument();

    resolveOldKernel(new Response(JSON.stringify({ state: "busy", kernelSessionId: "kernel-1", executionAttemptId: "attempt-1" }), { headers: { "Content-Type": "application/json" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText("Kernel idle")).toBeInTheDocument();
  });

  it("rehydrates eligibility before allowing undo for cached truncated history", async () => {
    const newest = { ...turn("newest", "failed"), prompt: "Newer failed turn" };
    const older = { ...turn("older"), prompt: "Cached applied turn" };
    let statusCalls = 0;
    let detailCalls = 0;
    let resolveDetail!: (response: Response) => void;
    const detail = new Promise<Response>((resolve) => { resolveDetail = resolve; });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/session/status")) {
        statusCalls += 1;
        return json({ sessionId: "session-1", documentRevision: 3, activeTurn: null, activeExecution: null, turnHistory: statusCalls === 1 ? [newest, older] : [newest], turnHistoryTruncated: statusCalls > 1 });
      }
      if (path.endsWith("/agent-turns/older")) { detailCalls += 1; return detail; }
      return baseFetch(input, init);
    });
    render(<App />);
    expect(await screen.findByText("Cached applied turn")).toBeInTheDocument();
    (await firstEventSource()).emit("notebook.updated", { revision: 3 }, 1);
    await waitFor(() => expect(statusCalls).toBeGreaterThanOrEqual(2));
    await userEvent.click(screen.getByText("Cached applied turn"));
    expect(screen.queryByRole("button", { name: "Undo entire turn" })).not.toBeInTheDocument();
    await waitFor(() => expect(detailCalls).toBe(1));
    resolveDetail(new Response(JSON.stringify({ ...older, historyTruncated: false, undoEligible: true }), { headers: { "Content-Type": "application/json" } }));
    expect(await screen.findByRole("button", { name: "Undo entire turn" })).toBeEnabled();
  });

  it("reconciles terminal turn detail after earlier notebook and execution refreshes", async () => {
    const previousSource = "a = 1\nprint(a)";
    const nextSource = "a = 2\nprint(a)";
    const runningTurn = { ...turn("race", "agent_running"), undoEligible: false };
    const terminalTurn = {
      ...turn("race"),
      changes: [{ cellId: "code-1", previousSource, nextSource }],
      historyTruncated: false,
    };
    const authoritativeNotebook = { ...notebook, revision: 4, cells: [{ ...notebook.cells[0], source: nextSource }] };
    let currentCalls = 0;
    let statusCalls = 0;
    let kernelCalls = 0;
    let resolveOldNotebook!: (value: Response) => void;
    let resolveOldExecution!: (value: Response) => void;
    const oldNotebook = new Promise<Response>((resolve) => { resolveOldNotebook = resolve; });
    const oldExecution = new Promise<Response>((resolve) => { resolveOldExecution = resolve; });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) {
        currentCalls += 1;
        if (currentCalls === 2) return oldNotebook;
        return json(currentCalls >= 3 ? authoritativeNotebook : notebook);
      }
      if (path.endsWith("/session/status")) {
        statusCalls += 1;
        const currentTurn = statusCalls >= 2 ? terminalTurn : runningTurn;
        return json({ sessionId: "session-1", documentRevision: statusCalls >= 2 ? 4 : 3, activeTurn: statusCalls >= 2 ? null : runningTurn, activeExecution: statusCalls >= 2 ? null : operation, turnHistory: [currentTurn] });
      }
      if (path.endsWith("/kernel/status")) {
        kernelCalls += 1;
        return json(kernelCalls >= 2 ? { state: "idle", kernelSessionId: "kernel-1", executionAttemptId: null } : { state: "busy", kernelSessionId: "kernel-1", executionAttemptId: "attempt-1" });
      }
      if (path.endsWith("/agent-turns/race")) return json(terminalTurn);
      if (path.endsWith("/execution/op-race")) return oldExecution;
      return baseFetch(input, init);
    });
    render(<App />);
    expect(await screen.findByText("Revision 3")).toBeInTheDocument();
    const source = await firstEventSource();
    source.emit("notebook.updated", { revision: 4 }, 1);
    source.emit("execution.updated", { operationId: "op-race" }, 2);
    source.emit("turn.updated", { turnId: "race" }, 3);
    await waitFor(() => expect(screen.getByText("Revision 4")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByLabelText("Source for code cell 1")).toHaveValue(nextSource));
    expect(await screen.findByLabelText("Revert agent change to code cell 1")).toBeEnabled();
    expect(screen.getByText("Kernel idle")).toBeInTheDocument();
    expect(screen.queryByText("Execution: running")).not.toBeInTheDocument();

    resolveOldNotebook(new Response(JSON.stringify({ ...notebook, revision: 99 }), { headers: { "Content-Type": "application/json" } }));
    resolveOldExecution(new Response(JSON.stringify(operation), { headers: { "Content-Type": "application/json" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText("Revision 4")).toBeInTheDocument();
    expect(screen.getByLabelText("Source for code cell 1")).toHaveValue(nextSource);
    expect(screen.queryByText("Execution: running")).not.toBeInTheDocument();
  });

  it("does not let an older terminal event reclaim the turn generation", async () => {
    const previousSource = "a = 1\nprint(a)";
    const olderSource = "a = 2\nprint('older')";
    const newerSource = "a = 3\nprint('newest change')";
    const runningTurn = { ...turn("ordered", "agent_running"), undoEligible: false };
    const olderTurn = {
      ...turn("ordered"),
      finalOutput: "older terminal outcome",
      changes: [{ cellId: "code-1", previousSource, nextSource: olderSource }],
      historyTruncated: false,
    };
    const newerTurn = {
      ...turn("ordered"),
      finalOutput: "newest terminal outcome",
      changes: [{ cellId: "code-1", previousSource, nextSource: newerSource }],
      historyTruncated: false,
    };
    const authoritativeNotebook = { ...notebook, revision: 5, cells: [{ ...notebook.cells[0], source: newerSource }] };
    let currentCalls = 0;
    let turnCalls = 0;
    let statusCalls = 0;
    let resolveOlderRefresh!: (value: Response) => void;
    const olderRefresh = new Promise<Response>((resolve) => { resolveOlderRefresh = resolve; });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = String(input);
      if (path.endsWith("/notebooks/current")) {
        currentCalls += 1;
        if (currentCalls === 2) return olderRefresh;
        return json(currentCalls >= 3 ? authoritativeNotebook : notebook);
      }
      if (path.endsWith("/session/status")) {
        statusCalls += 1;
        const currentTurn = statusCalls >= 2 ? newerTurn : runningTurn;
        return json({ sessionId: "session-1", documentRevision: statusCalls >= 2 ? 5 : 3, activeTurn: statusCalls >= 2 ? null : runningTurn, activeExecution: null, turnHistory: [currentTurn] });
      }
      if (path.endsWith("/kernel/status")) return json({ state: "idle", kernelSessionId: "kernel-1", executionAttemptId: null });
      if (path.endsWith("/agent-turns/ordered")) {
        turnCalls += 1;
        return json(turnCalls === 1 || turnCalls >= 4 ? olderTurn : newerTurn);
      }
      return baseFetch(input, init);
    });
    render(<App />);
    expect(await screen.findByText("Revision 3")).toBeInTheDocument();
    const source = await firstEventSource();
    source.emit("turn.updated", { turnId: "ordered" }, 1);
    await waitFor(() => expect(currentCalls).toBe(2));
    source.emit("turn.updated", { turnId: "ordered" }, 2);

    expect(await screen.findByText("Revision 5")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Source for code cell 1")).toHaveValue(newerSource));
    expect(await screen.findByText("newest terminal outcome")).toBeInTheDocument();
    expect(await screen.findByLabelText("Revert agent change to code cell 1")).toBeEnabled();

    resolveOlderRefresh(new Response(JSON.stringify({ ...notebook, revision: 4, cells: [{ ...notebook.cells[0], source: olderSource }] }), { headers: { "Content-Type": "application/json" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText("newest terminal outcome")).toBeInTheDocument();
    expect(screen.queryByText("older terminal outcome")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Revert agent change to code cell 1")).toBeEnabled();
    expect(screen.getByLabelText("Source for code cell 1")).toHaveValue(newerSource);
    expect(turnCalls).toBe(3);
  });

  it("allows a read-only turn with no editable cells", async () => {
    const submit = vi.fn();
    render(<AgentChatPanel notebook={notebook} scope={{ ...scope, editableCellIds: [] }} turn={null} activeTurn={null} history={[]} operation={null} busy={false} mutationsDisabled={false} onSubmit={submit} onCancel={vi.fn()} onUndo={vi.fn()} onClearScope={vi.fn()} onDecision={vi.fn()} onSelectTurn={vi.fn()} onFocusCell={vi.fn()} onDropCell={vi.fn()} />);
    expect(screen.getByText("Read-only turn — the agent can answer but not write.")).toBeInTheDocument();
    const ask = screen.getByRole("button", { name: "Ask" });
    expect(ask).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Agent instruction"), "explain these cells");
    expect(ask).toBeEnabled();
    await userEvent.click(ask);
    expect(submit).toHaveBeenCalledWith("explain these cells", { model: "default", mode: "edit", writeScope: "blocking" });
  });

  it("shift-selects a range of cells and scopes them all via the context menu", async () => {
    const cells = [0, 1, 2].map((index) => ({ cellId: `c-${index}`, index, cellType: "code" as const, source: `x = ${index}`, metadata: {}, outputs: [], executionCount: null }));
    const scopeMany = vi.fn();
    render(<NotebookView notebook={{ ...notebook, cells }} scope={{ editableCellIds: [], contextCellIds: [], sessionId: "session-1", notebookRevision: 3 }} turn={null} disabled={false} sourceActionsDisabled={false} autoSave={false} focusRequest={null} onDirtyChange={vi.fn()} onSave={vi.fn()} onRun={vi.fn()} onScope={vi.fn()} onScopeMany={scopeMany} onRevert={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Select code cell 1"));
    fireEvent.click(screen.getByLabelText("Select code cell 3"), { shiftKey: true });
    fireEvent.contextMenu(screen.getByLabelText("code cell 3"));
    expect(screen.getByText("3 cells selected")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("menuitem", { name: "Add 3 to edit" }));
    expect(scopeMany).toHaveBeenCalledWith(["c-0", "c-1", "c-2"], true);
  });

  it("renders raster and SVG notebook outputs as images", () => {
    render(<Outputs outputs={[
      { output_type: "display_data", data: { "image/png": "aGVsbG8=" } },
      { output_type: "display_data", data: { "image/svg+xml": "<svg xmlns='http://www.w3.org/2000/svg'></svg>" } },
    ]} />);
    expect(screen.getByAltText("Cell output 1")).toHaveAttribute("src", "data:image/png;base64,aGVsbG8=");
    expect(screen.getByAltText("SVG cell output 2").getAttribute("src")).toMatch(/^data:image\/svg\+xml;charset=utf-8,/);
  });

  it("surfaces operation and attempt errors in execution status", () => {
    const failed = { ...operation, state: "timed_out", error: { code: "cell_timed_out", message: "Cell execution timed out", details: {} }, attempts: [{ ...operation.attempts[0], error: { code: "kernel_error", message: "Kernel stopped", details: {} } }] };
    render(<AgentChatPanel notebook={notebook} scope={scope} turn={null} activeTurn={null} history={[]} operation={failed} busy={false} mutationsDisabled={false} onSubmit={vi.fn()} onCancel={vi.fn()} onUndo={vi.fn()} onClearScope={vi.fn()} onDecision={vi.fn()} onSelectTurn={vi.fn()} onFocusCell={vi.fn()} onDropCell={vi.fn()} />);
    expect(screen.getByText("Cell execution timed out")).toBeInTheDocument();
    expect(screen.getByText("Cell 1: Kernel stopped")).toBeInTheDocument();
  });

  it("distinguishes truncated retained output from an empty output history", () => {
    const truncated = {
      ...operation,
      state: "completed",
      attempts: [{ ...operation.attempts[0], state: "completed", outputs: [], outputsTruncated: true }],
    };
    const view = render(<AgentChatPanel notebook={notebook} scope={scope} turn={null} activeTurn={null} history={[]} operation={truncated} busy={false} mutationsDisabled={false} onSubmit={vi.fn()} onCancel={vi.fn()} onUndo={vi.fn()} onClearScope={vi.fn()} onDecision={vi.fn()} onSelectTurn={vi.fn()} onFocusCell={vi.fn()} onDropCell={vi.fn()} />);
    expect(screen.getByText("Cell 1: Retained execution output was truncated.")).toBeInTheDocument();

    view.rerender(<AgentChatPanel notebook={notebook} scope={scope} turn={null} activeTurn={null} history={[]} operation={{ ...truncated, attempts: [{ ...truncated.attempts[0], outputsTruncated: false }] }} busy={false} mutationsDisabled={false} onSubmit={vi.fn()} onCancel={vi.fn()} onUndo={vi.fn()} onClearScope={vi.fn()} onDecision={vi.fn()} onSelectTurn={vi.fn()} onFocusCell={vi.fn()} onDropCell={vi.fn()} />);
    expect(screen.queryByText("Cell 1: Retained execution output was truncated.")).not.toBeInTheDocument();
  });

  it("focuses and contains the approval dialog and cancels on Escape", async () => {
    const decide = vi.fn();
    const risky = { ...operation, kind: "agent_downstream", parentTurnId: "turn-1", state: "awaiting_approval", attempts: [{ ...operation.attempts[0], state: "awaiting_approval", risk: { level: "confirm", reasons: ["Risk"], matchedPatterns: ["pattern"] } }] };
    const outside = document.createElement("button"); document.body.append(outside); outside.focus();
    const { unmount } = render(<RiskyExecutionDialog operation={risky} attempt={risky.attempts[0]} busy={false} onDecision={decide} />);
    const approve = screen.getByRole("button", { name: "Approve and run" });
    expect(approve).toHaveFocus();
    expect(screen.getByLabelText("Source preview for cell 1")).toHaveTextContent("print('preview')");
    fireEvent.keyDown(approve, { key: "Tab" });
    expect(screen.getByRole("button", { name: "Cancel run" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("alertdialog"), { key: "Escape" });
    expect(decide).toHaveBeenCalledWith("cancel");
    unmount();
    expect(outside).toHaveFocus();
    outside.remove();
  });
});
