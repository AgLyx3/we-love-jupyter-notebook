import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import type { AgentOperation, AgentTurn, NotebookSnapshot } from "../api/client";
import { EventSourceMock } from "../test/setup";

vi.mock("@uiw/react-codemirror", () => ({
  default: ({ value, onChange, "aria-label": label, readOnly }: { value: string; onChange: (value: string) => void; "aria-label": string; readOnly: boolean }) =>
    <textarea aria-label={label} value={value} disabled={readOnly} onChange={(event) => onChange(event.target.value)} />,
}));

const PREVIOUS = "a = 1\nb = 2\nc = 3";
const NEXT = "a = 99\nb = 2\nc = 99";

const notebook: NotebookSnapshot = {
  sessionId: "session-1", filename: "sample.ipynb", revision: 4, dirty: false, metadata: {}, nbformat: 4, nbformatMinor: 5,
  cells: [{ cellId: "code-1", index: 0, cellType: "code", source: NEXT, metadata: {}, outputs: [], executionCount: null }],
};

function operation(ordinal: number, state: AgentOperation["state"]): AgentOperation {
  return { operationId: `turn-1:code-1:${ordinal}`, cellId: "code-1", kind: "source_hunk", ordinal, state, previousRange: [ordinal, ordinal + 1], nextRange: [ordinal, ordinal + 1] };
}

// The cell source the backend would actually hold for a given ledger. A rejected
// operation puts its pre-turn line back, so after a partial reject the cell
// matches neither previousSource nor nextSource — which is exactly the state
// the old source-equality reconciliation got wrong. Fixtures that leave the
// source at nextSource cannot detect that regression.
function composedSource(operations: AgentOperation[]): string {
  const previous = PREVIOUS.split("\n");
  const next = NEXT.split("\n");
  return next.map((line, index) => {
    const item = operations.find((entry) => entry.ordinal === index);
    return item?.state === "rejected" ? previous[index] : line;
  }).join("\n");
}

function notebookFor(operations: AgentOperation[], outputs: Record<string, unknown>[] = []): NotebookSnapshot {
  return { ...notebook, cells: [{ ...notebook.cells[0], source: composedSource(operations), outputs }] };
}

function turnWith(operations: AgentOperation[]): AgentTurn {
  return {
    turnId: "turn-1", sessionId: "session-1", baseRevision: 3, prompt: "edit", editableCellIds: ["code-1"], contextCellIds: [],
    undoEligible: true, state: "completed", attempts: 1, finalOutput: "done", appliedRevision: 4, executionOperationId: null,
    changes: [{ cellId: "code-1", previousSource: PREVIOUS, nextSource: NEXT }],
    operations, error: null, createdAt: "", completedAt: "",
  };
}

function json(value: unknown) { return Promise.resolve(new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } })); }

function mount(turn: AgentTurn, snapshot: NotebookSnapshot = notebook) {
  return renderApp(turn, snapshot).calls;
}

// Serves a mutable snapshot so a test can advance the document the way the app
// really sees it — swap what the API returns, then push the SSE event that
// makes the app refetch.
function renderApp(turn: AgentTurn, initial: NotebookSnapshot = notebook) {
  const calls: { path: string; body: unknown }[] = [];
  let snapshot = initial;
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const path = String(input);
    if (init?.method === "POST") calls.push({ path, body: init.body ? JSON.parse(String(init.body)) : null });
    if (path.endsWith("/notebooks/current")) return json(snapshot);
    if (path.endsWith("/turn-scope")) return json({ editableCellIds: [], contextCellIds: [], sessionId: "session-1", notebookRevision: 4 });
    if (path.endsWith("/kernel/status")) return json({ state: "idle", kernelSessionId: "kernel-1", executionAttemptId: null });
    if (path.endsWith("/session/status")) return json({ sessionId: "session-1", documentRevision: 4, activeTurn: null, activeExecution: null, turnHistory: [turn] });
    if (path.includes("/accept")) return json(turnWith(turn.operations!.map((item) => ({ ...item, state: "accepted" as const }))));
    if (path.includes("/reject")) return json({ ...snapshot, revision: 5 });
    if (path.includes("/agent-turns/turn-1")) return json(turn);
    return json({});
  });
  render(<App />);
  const rerender = async (next: NotebookSnapshot) => {
    snapshot = { ...next, revision: next.revision + 1 };
    await waitFor(() => expect(EventSourceMock.instances.length).toBeGreaterThan(0));
    EventSourceMock.instances[0].emit("notebook.updated", { revision: snapshot.revision }, 1);
  };
  return { calls, rerender };
}

afterEach(() => vi.restoreAllMocks());

describe("per-operation review", () => {
  it("shows a persistent review bar counting unreviewed changes", async () => {
    mount(turnWith([operation(0, "pending"), operation(1, "pending")]));
    expect(await screen.findByText("0 of 2 changes reviewed")).toBeInTheDocument();
  });

  it("counts accepted operations as reviewed", async () => {
    mount(turnWith([operation(0, "accepted"), operation(1, "pending")]));
    expect(await screen.findByText("1 of 2 changes reviewed")).toBeInTheDocument();
  });

  it("hides the review bar when a turn applied no changes", async () => {
    mount(turnWith([]));
    await screen.findByLabelText("Source for code cell 1");
    expect(screen.queryByRole("region", { name: "Review agent changes" })).not.toBeInTheDocument();
  });

  it("keeps the cell diff visible while any operation is unreviewed", async () => {
    // The old reconciliation compared the cell source to nextSource, so undoing
    // one hunk made the whole cell's diff — including this pending one — vanish.
    const operations = [operation(0, "rejected"), operation(1, "pending")];
    mount(turnWith(operations), notebookFor(operations));
    expect(await screen.findByText("Agent changed this cell")).toBeInTheDocument();
  });

  it("clears the cell diff once every operation is settled", async () => {
    const operations = [operation(0, "accepted"), operation(1, "rejected")];
    mount(turnWith(operations), notebookFor(operations));
    await screen.findByLabelText("Source for code cell 1");
    expect(screen.queryByText("Agent changed this cell")).not.toBeInTheDocument();
  });

  it("does not repeat Keep/Undo in the cell header when hunk controls are shown", async () => {
    // Per-hunk widgets act on the same change, so a header pair beside them is
    // duplication. The header keeps only the label.
    const operations = [operation(0, "pending"), operation(1, "pending")];
    mount(turnWith(operations), notebookFor(operations));
    expect(await screen.findByText("Agent changed this cell")).toBeInTheDocument();
    expect(screen.queryByLabelText("Keep agent change to code cell 1")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Revert agent change to code cell 1")).not.toBeInTheDocument();
  });

  it("keeps header Keep/Undo for an added cell, which has no hunks to attach to", async () => {
    const add: AgentOperation = {
      operationId: "turn-1:code-1:0", cellId: "code-1", kind: "structural_add",
      ordinal: 0, state: "pending", previousRange: null, nextRange: null,
    };
    mount(turnWith([add]), notebook);
    expect(await screen.findByText("Agent added this cell")).toBeInTheDocument();
    expect(screen.getByLabelText("Keep agent change to code cell 1")).toBeInTheDocument();
    expect(screen.getByLabelText("Revert agent change to code cell 1")).toBeInTheDocument();
  });

  it("sends accept-all without an expected revision", async () => {
    const calls = mount(turnWith([operation(0, "pending"), operation(1, "pending")]));
    await userEvent.click(await screen.findByRole("button", { name: /Keep all/ }));
    const accept = await waitFor(() => {
      const found = calls.find((item) => item.path.includes("/operations/accept-all"));
      expect(found).toBeDefined();
      return found!;
    });
    expect(accept.body).toEqual({ sessionId: "session-1" });
  });

  it("sends reject-all with the expected revision after confirming", async () => {
    const calls = mount(turnWith([operation(0, "pending"), operation(1, "pending")]));
    await userEvent.click(await screen.findByRole("button", { name: /Undo all/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Undo them" }));
    const reject = await waitFor(() => {
      const found = calls.find((item) => item.path.includes("/operations/reject-all"));
      expect(found).toBeDefined();
      return found!;
    });
    expect(reject.body).toEqual({ sessionId: "session-1", expectedDocumentRevision: 4 });
  });

  it("confirms before undoing, and says kept changes are preserved", async () => {
    const calls = mount(turnWith([operation(0, "accepted"), operation(1, "pending")]));
    await userEvent.click(await screen.findByRole("button", { name: /Undo all/ }));

    // Reject-all only undoes what is still unreviewed. Claiming it also
    // reverses kept work would be false — that is whole-turn undo's job.
    expect(await screen.findByText(/Undo 1 unreviewed change\? The 1 you kept stay\./)).toBeInTheDocument();
    expect(calls.some((item) => item.path.includes("/reject"))).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "Undo them" }));
    await waitFor(() => expect(calls.some((item) => item.path.includes("/operations/reject-all"))).toBe(true));
  });

  it("can be cancelled from the confirmation without undoing anything", async () => {
    const calls = mount(turnWith([operation(0, "pending"), operation(1, "pending")]));
    await userEvent.click(await screen.findByRole("button", { name: /Undo all/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("button", { name: "Undo them" })).not.toBeInTheDocument();
    expect(calls.some((item) => item.path.includes("/reject"))).toBe(false);
  });

  it("hides the review bar once every change is settled", async () => {
    // The counter used to include settled operations, so the bar stayed on
    // screen at "2 of 2 reviewed" with a live no-op Undo all.
    const operations = [operation(0, "accepted"), operation(1, "accepted")];
    mount(turnWith(operations), notebookFor(operations));
    await screen.findByLabelText("Source for code cell 1");
    expect(screen.queryByRole("region", { name: "Review agent changes" })).not.toBeInTheDocument();
  });

  it("counts a stale change as unreviewed so the bar and the cell agree", async () => {
    const operations = [operation(0, "stale"), operation(1, "accepted")];
    mount(turnWith(operations), notebookFor(operations));
    expect(await screen.findByText("1 of 2 changes reviewed")).toBeInTheDocument();
    // Stale cannot be undone, so Undo all has nothing to do — but Keep all can
    // still settle it, which is why the bar is still shown.
    expect(screen.getByRole("button", { name: /Undo all/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Keep all/ })).toBeEnabled();
  });

  it("replaces per-cell controls with an explanation when the cell went stale", async () => {
    mount(turnWith([operation(0, "stale"), operation(1, "stale")]));
    expect(await screen.findByText(/This cell changed after the agent edited it/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Revert agent change to code cell 1")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Keep agent change to code cell 1")).not.toBeInTheDocument();
  });

  it("warns that outputs came from code the user undid", async () => {
    const operations = [operation(0, "rejected"), operation(1, "pending")];
    mount(turnWith(operations), notebookFor(operations, [{ output_type: "stream", text: "stale result" }]));
    expect(await screen.findByText(/Outputs are from code you undid/)).toBeInTheDocument();
  });

  it("stops warning about outputs once the cell is re-run", async () => {
    // Derived from the ledger alone the warning would never clear: the
    // operation stays rejected however many times the user re-runs.
    const operations = [operation(0, "rejected"), operation(1, "pending")];
    const executed = { ...notebookFor(operations, [{ output_type: "stream", text: "fresh" }]) };
    executed.cells = [{ ...executed.cells[0], executionCount: 7 }];
    const { rerender } = renderApp(turnWith(operations), notebookFor(operations, [{ output_type: "stream", text: "old" }]));
    expect(await screen.findByText(/Outputs are from code you undid/)).toBeInTheDocument();
    await rerender(executed);
    await waitFor(() => expect(screen.queryByText(/Outputs are from code you undid/)).not.toBeInTheDocument());
  });

  it("does not warn about outputs when nothing was undone", async () => {
    const operations = [operation(0, "pending")];
    mount(turnWith(operations), notebookFor(operations, [{ output_type: "stream", text: "fresh" }]));
    await screen.findByLabelText("Source for code cell 1");
    expect(screen.queryByText(/Outputs are from code you undid/)).not.toBeInTheDocument();
  });

  it("keeps a whole cell's changes in one batched request", async () => {
    // Previously one request per hunk, which was N round trips and left the
    // cell half-kept if one failed mid-loop. Uses a previewed Markdown cell:
    // no editor is rendered, so the header pair is the review surface there.
    const operations = [operation(0, "pending"), operation(1, "pending")];
    const markdown = { ...notebookFor(operations) };
    markdown.cells = [{ ...markdown.cells[0], cellType: "markdown" as const }];
    const calls = mount(turnWith(operations), markdown);
    await userEvent.click(await screen.findByLabelText("Keep agent change to markdown cell 1"));
    const accepts = await waitFor(() => {
      const found = calls.filter((item) => item.path.includes("/operations/"));
      expect(found).toHaveLength(1);
      return found;
    });
    expect(accepts[0].path).toContain("/operations/accept-all");
    expect(accepts[0].body).toEqual({
      sessionId: "session-1",
      operationIds: ["turn-1:code-1:0", "turn-1:code-1:1"],
    });
  });

  // The per-hunk widget buttons themselves are not asserted here: this file
  // mocks @uiw/react-codemirror with a plain textarea, so CodeMirror
  // decorations never render. Their placement is covered by the hunkOverlays
  // unit tests, and the click-through was verified against the running app.

  it("offers Keep/Undo on a trusted turn's added cell", async () => {
    // T1: an added cell has no `change` (nothing to diff against) but carries a
    // structural_add operation, which is what the review bar keys off.
    const addOp: AgentOperation = { operationId: "turn-1:added-1:0", cellId: "added-1", kind: "structural_add", ordinal: 0, state: "pending", previousRange: null, nextRange: null };
    const trusted = { ...turnWith([addOp]), writeScope: "trusted" as const, changes: [] };
    const withAdded = { ...notebook, cells: [notebook.cells[0], { cellId: "added-1", index: 1, cellType: "markdown" as const, source: "## summary", metadata: { agent_authored: true }, outputs: [], executionCount: null }] };
    mount(trusted, withAdded);
    expect(await screen.findByText("Agent added this cell")).toBeInTheDocument();
    expect(screen.getByLabelText("Keep agent change to markdown cell 2")).toBeEnabled();
    expect(screen.getByLabelText("Revert agent change to markdown cell 2")).toBeEnabled();
  });

  it("explains instead of offering Undo on a trusted cell without operations", async () => {
    // A moved/retyped/deleted-adjacent cell has no ledger operations; only
    // whole-turn undo applies, and the cell must say so, not just go quiet.
    const trusted = { ...turnWith([]), writeScope: "trusted" as const, changes: [{ cellId: "code-1", previousSource: PREVIOUS, nextSource: NEXT }] };
    mount(trusted);
    expect(await screen.findByText(/Part of a whole-notebook edit/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Revert agent change to code cell 1")).not.toBeInTheDocument();
  });

  it("clears the cell header immediately after keeping every change", async () => {
    // The accept response still carries every change the turn made. Committing
    // it verbatim left a fully-reviewed cell showing its header and — with no
    // pending hunks left for the ledger overlay — the legacy whole-change diff.
    const operations = [operation(0, "pending"), operation(1, "pending")];
    mount(turnWith(operations), notebookFor(operations));
    await userEvent.click(await screen.findByRole("button", { name: /Keep all/ }));
    await waitFor(() => expect(screen.queryByText("Agent changed this cell")).not.toBeInTheDocument());
  });

  it("keeps the diff for a cell the ledger does not govern", async () => {
    // A Trusted turn's retyped cells carry a change but no operations. Reading
    // "no operations" as "fully reviewed" hid their diff and the note saying
    // only whole-turn undo applies.
    const governed = operation(0, "pending");
    const turn: AgentTurn = {
      ...turnWith([governed]),
      writeScope: "trusted",
      changes: [
        { cellId: "code-1", previousSource: PREVIOUS, nextSource: NEXT },
        { cellId: "code-2", previousSource: "old\n", nextSource: "new\n" },
      ],
    };
    const twoCells = { ...notebookFor([governed]) };
    twoCells.cells = [
      twoCells.cells[0],
      { ...twoCells.cells[0], cellId: "code-2", index: 1, source: "new\n" },
    ];
    mount(turn, twoCells);
    // Both cells still under review: the governed one and the ungoverned one.
    await waitFor(() => expect(screen.getAllByText(/Agent changed this cell/)).toHaveLength(2));
    expect(screen.getByText(/Part of a whole-notebook edit/)).toBeInTheDocument();
  });

  it("falls back to source comparison for turns served without a ledger", async () => {
    const legacy = turnWith([]);
    delete (legacy as { operations?: unknown }).operations;
    mount(legacy);
    // Pre-ledger turns must still resolve their diffs against the notebook.
    expect(await screen.findByLabelText("Revert agent change to code cell 1")).toBeInTheDocument();
  });
});
