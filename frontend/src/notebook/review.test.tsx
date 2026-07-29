import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import type { AgentOperation, AgentTurn, NotebookSnapshot } from "../api/client";

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
  const calls: { path: string; body: unknown }[] = [];
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
  return calls;
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
    expect(await screen.findByLabelText("Revert agent change to code cell 1")).toBeInTheDocument();
  });

  it("clears the cell diff once every operation is settled", async () => {
    const operations = [operation(0, "accepted"), operation(1, "rejected")];
    mount(turnWith(operations), notebookFor(operations));
    await screen.findByLabelText("Source for code cell 1");
    expect(screen.queryByLabelText("Revert agent change to code cell 1")).not.toBeInTheDocument();
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

  it("sends reject-all with the expected revision", async () => {
    const calls = mount(turnWith([operation(0, "pending"), operation(1, "pending")]));
    await userEvent.click(await screen.findByRole("button", { name: /Undo all/ }));
    const reject = await waitFor(() => {
      const found = calls.find((item) => item.path.includes("/operations/reject-all"));
      expect(found).toBeDefined();
      return found!;
    });
    expect(reject.body).toEqual({ sessionId: "session-1", expectedDocumentRevision: 4 });
  });

  it("confirms before undoing everything when changes have been kept", async () => {
    const calls = mount(turnWith([operation(0, "accepted"), operation(1, "pending")]));
    await userEvent.click(await screen.findByRole("button", { name: /Undo all/ }));

    // Undo-all also reverses kept work, so it must say so rather than fire.
    expect(await screen.findByText(/also reverses the 1 change you kept/)).toBeInTheDocument();
    expect(calls.some((item) => item.path.includes("/reject"))).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "Undo everything" }));
    await waitFor(() => expect(calls.some((item) => item.path.includes("/operations/reject-all"))).toBe(true));
  });

  it("does not confirm when nothing has been kept", async () => {
    const calls = mount(turnWith([operation(0, "pending"), operation(1, "pending")]));
    await userEvent.click(await screen.findByRole("button", { name: /Undo all/ }));
    await waitFor(() => expect(calls.some((item) => item.path.includes("/operations/reject-all"))).toBe(true));
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

  it("does not warn about outputs when nothing was undone", async () => {
    const operations = [operation(0, "pending")];
    mount(turnWith(operations), notebookFor(operations, [{ output_type: "stream", text: "fresh" }]));
    await screen.findByLabelText("Source for code cell 1");
    expect(screen.queryByText(/Outputs are from code you undid/)).not.toBeInTheDocument();
  });

  it("keeps a single cell's changes from the cell's own control", async () => {
    const calls = mount(turnWith([operation(0, "pending"), operation(1, "pending")]));
    await userEvent.click(await screen.findByLabelText("Keep agent change to code cell 1"));
    await waitFor(() => expect(calls.filter((item) => /operations\/.+\/accept$/.test(item.path))).toHaveLength(2));
  });

  it("falls back to source comparison for turns served without a ledger", async () => {
    const legacy = turnWith([]);
    delete (legacy as { operations?: unknown }).operations;
    mount(legacy);
    // Pre-ledger turns must still resolve their diffs against the notebook.
    expect(await screen.findByLabelText("Revert agent change to code cell 1")).toBeInTheDocument();
  });
});
