import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import App from "./App";
import AgentChatPanel, { type TurnRecord } from "./agentChat/AgentChatPanel";
import LineDiff from "./notebook/LineDiff";
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
  attempts: [{ executionAttemptId: "attempt-1", cellId: "code-1", cellIndex: 0, sourcePreview: "print('preview')", state: "running", risk: { level: "safe", reasons: [], matchedPatterns: [] }, decision: null, outputs: [], executionCount: null, error: null }], error: null, createdAt: "", completedAt: null,
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

describe("remediation behaviors", () => {
  it("locks mutations during a recovered active operation and cancels it with immutable correlation", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(baseFetch);
    render(<App />);
    expect(await screen.findByLabelText("Run code cell 1")).toBeDisabled();
    expect(screen.getByLabelText("Allow agent edit code cell 1")).toBeDisabled();
    expect(screen.getByLabelText("Upload notebook").querySelector("input")).toBeDisabled();
    expect(screen.getByLabelText("Download notebook")).toBeEnabled();
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

  it("downloads through the API and always revokes the object URL", async () => {
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:notebook");
    const revoke = vi.spyOn(URL, "revokeObjectURL");
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => String(input).endsWith("/notebooks/download") ? Promise.resolve(new Response(new Blob(["{}"]))) : baseFetch(input, init));
    render(<App />);
    await userEvent.click(await screen.findByLabelText("Download notebook"));
    expect(create).toHaveBeenCalled();
    expect(revoke).toHaveBeenCalledWith("blob:notebook");
  });

  it("surfaces download failures without creating an object URL", async () => {
    const create = vi.spyOn(URL, "createObjectURL");
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => String(input).endsWith("/notebooks/download") ? json({ error: { code: "download_failed", message: "Download unavailable", details: {} } }, 500) : baseFetch(input, init));
    render(<App />);
    await userEvent.click(await screen.findByLabelText("Download notebook"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Download unavailable");
    expect(create).not.toHaveBeenCalled();
  });

  it("renders unchanged, removed, and added diff lines separately", () => {
    render(<LineDiff before={"same\nold"} after={"same\nnew"} />);
    const diff = screen.getByLabelText("Agent source diff");
    expect(diff.querySelector(".diff-same")).toHaveTextContent("same");
    expect(diff.querySelector(".diff-removed")).toHaveTextContent("old");
    expect(diff.querySelector(".diff-added")).toHaveTextContent("new");
  });

  it("uses a bounded regional diff for large cells", () => {
    const prefix = Array.from({ length: 1_000 }, (_, index) => `prefix ${index}`);
    const suffix = Array.from({ length: 1_000 }, (_, index) => `suffix ${index}`);
    render(<LineDiff before={[...prefix, "old region", ...suffix].join("\n")} after={[...prefix, "new region", ...suffix].join("\n")} />);
    const diff = screen.getByLabelText("Agent source diff");
    expect(diff.querySelectorAll(".diff-removed")).toHaveLength(1);
    expect(diff.querySelector(".diff-removed")).toHaveTextContent("old region");
    expect(diff.querySelectorAll(".diff-added")).toHaveLength(1);
    expect(diff.querySelector(".diff-added")).toHaveTextContent("new region");
    expect(diff.querySelectorAll(".diff-same").length).toBeLessThanOrEqual(402);
    expect(diff).toHaveTextContent("same lines omitted");
  });

  it("shows frozen scope for every turn and selects historical outcomes", async () => {
    const history: TurnRecord[] = [
      { turn: turn("two", "failed"), prompt: "Second prompt", editableCellIds: ["code-1"], contextCellIds: [] },
      { turn: turn("one"), prompt: "First prompt", editableCellIds: ["code-1"], contextCellIds: ["code-1"] },
    ];
    const select = vi.fn();
    render(<AgentChatPanel notebook={notebook} scope={scope} turn={history[0].turn} activeTurn={null} history={history} operation={null} busy={false} mutationsDisabled={false} onSubmit={vi.fn()} onCancel={vi.fn()} onUndo={vi.fn()} onClearScope={vi.fn()} onDecision={vi.fn()} onSelectTurn={select} onFocusCell={vi.fn()} onDropCell={vi.fn()} />);
    expect(screen.getByText("1 editable · 0 context · failed")).toBeInTheDocument();
    await userEvent.click(screen.getByText("First prompt"));
    expect(select).toHaveBeenCalledWith("one");
  });

  it("keeps one EventSource through refresh events and uses monotonic event IDs", async () => {
    let currentCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => { if (String(input).endsWith("/notebooks/current")) currentCalls += 1; return baseFetch(input, init); });
    render(<App />);
    await screen.findByText("sample.ipynb");
    expect(EventSourceMock.instances).toHaveLength(1);
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

  it("renders historical frozen members with details and focuses them", async () => {
    const history: TurnRecord[] = [
      { turn: turn("new"), prompt: "Current prompt", editableCellIds: [], contextCellIds: [] },
      { turn: turn("old"), prompt: "Historical prompt", editableCellIds: ["code-1"], contextCellIds: ["code-1"] },
    ];
    const onFocus = vi.fn();
    function Harness() {
      const [selected, setSelected] = useState("new");
      return <AgentChatPanel notebook={notebook} scope={{ ...scope, editableCellIds: [], contextCellIds: [] }} turn={history.find((item) => item.turn.turnId === selected)!.turn} activeTurn={null} history={history} operation={null} busy={false} mutationsDisabled={false} onSubmit={vi.fn()} onCancel={vi.fn()} onUndo={vi.fn()} onClearScope={vi.fn()} onDecision={vi.fn()} onSelectTurn={setSelected} onFocusCell={onFocus} onDropCell={vi.fn()} />;
    }
    render(<Harness />);
    await userEvent.click(screen.getByText("Historical prompt"));
    const frozen = screen.getByLabelText("Frozen turn scope");
    const rows = within(frozen).getAllByTitle("Cell ID: code-1");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveClass("editable");
    expect(rows[1]).toHaveClass("context");
    expect(frozen).toHaveTextContent("code");
    expect(frozen).toHaveTextContent("a = 1");
    await userEvent.click(rows[0]);
    expect(onFocus).toHaveBeenCalledWith("code-1");
  });

  it("hydrates completed turn history from session status after reload", async () => {
    const historical = { ...turn("persisted"), prompt: "Persisted backend turn", editableCellIds: ["code-1"], contextCellIds: ["code-1"] };
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      if (String(input).endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: 3, activeTurn: null, activeExecution: null, turnHistory: [historical] });
      return baseFetch(input, init);
    });
    render(<App />);
    expect(await screen.findByText("Persisted backend turn")).toBeInTheDocument();
    expect(within(screen.getByLabelText("Frozen turn scope")).getAllByTitle("Cell ID: code-1")).toHaveLength(2);
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
    const source = EventSourceMock.instances[0];
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
    EventSourceMock.instances[0].emit("notebook.updated", { revision: 3 }, 1);
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
    EventSourceMock.instances[0].emit("notebook.updated", { revision: 3 }, 1);
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
    const source = EventSourceMock.instances[0];
    source.emit("notebook.updated", { revision: 3 }, 1);
    source.emit("execution.updated", { operationId: "op-1" }, 2);
    expect(await screen.findByText("Execution: running")).toBeInTheDocument();
    resolveRefresh(new Response(JSON.stringify(notebook), { headers: { "Content-Type": "application/json" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText("Execution: running")).toBeInTheDocument();
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
