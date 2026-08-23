import { configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import NotebookView from "./NotebookView";
import type { CellTuningControls } from "./NotebookCell";
import type { AgentTurn, CellOutput, NotebookSnapshot, TuningPlan, TuningRecord, TuningWarmResult, TurnScope } from "../api/client";

// Opening the panel chains scan → warm before the knobs exist, and the whole
// suite runs these files in parallel; the default 1s budget flakes under load.
configure({ asyncUtilTimeout: 5000 });

// CodeMirror measures layout on interaction and jsdom has no layout; the rest
// of the suite replaces it with a plain textarea for the same reason.
vi.mock("@uiw/react-codemirror", () => ({
  default: ({ value, onChange, "aria-label": label, readOnly }: { value: string; onChange: (value: string) => void; "aria-label": string; readOnly: boolean }) =>
    <textarea aria-label={label} value={value} disabled={readOnly} onChange={(event) => onChange(event.target.value)} />,
}));

const plot: CellOutput = { output_type: "display_data", data: { "image/png": "COMMITTED" }, metadata: {} };
const printed: CellOutput = { output_type: "stream", name: "stdout", text: "42\n" };

const notebook = (outputs: CellOutput[] = [plot]): NotebookSnapshot => ({
  sessionId: "s1", filename: "n.ipynb", revision: 7, dirty: false, metadata: {},
  nbformat: 4, nbformatMinor: 5,
  cells: [{ cellId: "cell-a", index: 0, cellType: "code", source: "plt.hist(data, bins=BINS)", metadata: {}, outputs, executionCount: 1 }],
});
const scope: TurnScope = { editableCellIds: [], contextCellIds: [], sessionId: "s1", notebookRevision: 7 };

const plan: TuningPlan = {
  cellId: "cell-a", revision: 7, blocked: null, rejections: [], risky: [],
  knobs: [{ name: "BINS", cellId: "cell-a", cellIndex: 0, kind: "int", value: 30, bounds: { minimum: 6, maximum: 150, step: 1 } }],
};
const warmed: TuningWarmResult = {
  shadowId: "shadow-1", knobs: plan.knobs, rejections: [], outputs: [plot],
  timings: [], totalSeconds: 0.4, failed: false, failedCellId: null,
};

const tuningRecord = (over: Partial<TuningRecord> = {}): TuningRecord => ({
  recordId: "rec-1", origin: "tune", sessionId: "s1", state: "applied", knobs: ["BINS"],
  baseRevision: 6, appliedRevision: 7, executionOperationId: "exec-1", executionStartIndex: 0,
  reRanFromTop: false, error: null, createdAt: "2026-08-14T00:00:00Z",
  changes: [{ cellId: "cell-a", previousSource: "plt.hist(data, bins=20)", nextSource: "plt.hist(data, bins=30)" }],
  // A tuning record's hunks carry no line ranges — the backend serializes none
  // — which is exactly the shape the cell has to cope with.
  operations: [{ operationId: "op-1", cellId: "cell-a", ordinal: 0, kind: "source_hunk", state: "pending" }],
  ...over,
});

const agentTurn: AgentTurn = {
  turnId: "turn-1", sessionId: "s1", baseRevision: 6, prompt: "tidy up", editableCellIds: ["cell-a"], contextCellIds: [],
  undoEligible: true, state: "completed", attempts: 1, finalOutput: null, appliedRevision: 7,
  executionOperationId: null, error: null, createdAt: "2026-08-14T00:00:00Z", completedAt: "2026-08-14T00:00:01Z",
  changes: [{ cellId: "cell-a", previousSource: "a = 1", nextSource: "a = 2" }],
  operations: [{ operationId: "op-a", cellId: "cell-a", ordinal: 0, kind: "structural_add", state: "pending", previousRange: null, nextRange: null }],
};

function controls(over: Partial<CellTuningControls> = {}): CellTuningControls {
  return {
    revision: 7,
    onScan: vi.fn().mockResolvedValue(plan),
    onWarm: vi.fn().mockResolvedValue(warmed),
    onPreview: vi.fn(),
    onApply: vi.fn(),
    onDiscardShadow: vi.fn(),
    ...over,
  };
}

function view(over: {
  outputs?: CellOutput[]; tunable?: string[]; tuning?: CellTuningControls;
  record?: TuningRecord | null; turn?: AgentTurn | null;
  onKeepTuned?: (recordId: string, operationIds: string[]) => void;
  onUndoTuned?: (recordId: string, operationIds: string[]) => void;
} = {}) {
  return <NotebookView notebook={notebook(over.outputs)} scope={scope} turn={over.turn ?? null}
    tuningRecord={over.record ?? null}
    tuningControls={over.tuning ?? controls()}
    tunableCellIds={new Set(over.tunable ?? ["cell-a"])}
    disabled={false} sourceActionsDisabled={false} autoSave={false} focusRequest={null}
    onDirtyChange={vi.fn()} onSave={vi.fn()} onRun={vi.fn()} onScope={vi.fn()} onScopeMany={vi.fn()} onRevert={vi.fn()}
    onKeepOperation={vi.fn()} onUndoOperation={vi.fn()} onKeepCell={vi.fn()}
    onKeepTuned={over.onKeepTuned ?? vi.fn()} onUndoTuned={over.onUndoTuned ?? vi.fn()} />;
}

const tuneButton = () => screen.queryByRole("button", { name: "Tune code cell 1" });

describe("the Tune entry point", () => {
  it("offers Tune on a plot cell the scan found knobs in", () => {
    render(view());
    expect(tuneButton()).toBeInTheDocument();
  });

  it("stays away when the scan found no knobs", () => {
    render(view({ tunable: [] }));
    expect(tuneButton()).not.toBeInTheDocument();
  });

  it("stays away when the cell has no picture to compare against", () => {
    render(view({ outputs: [printed] }));
    expect(tuneButton()).not.toBeInTheDocument();
  });

  it("opens the panel inside the output region, replacing the committed output", async () => {
    const tuning = controls();
    render(view({ tuning }));

    await userEvent.click(tuneButton() as HTMLElement);

    const panel = await screen.findByRole("region", { name: "Plot tuning" });
    expect(tuning.onScan).toHaveBeenCalledWith("cell-a");
    // Inside the output region rather than floating somewhere else in the cell.
    expect(panel.closest(".cell-output-region")).not.toBeNull();
    // One picture, not two: the panel renders the committed output as its own
    // "before", so the cell's copy must go.
    await screen.findByRole("spinbutton", { name: "BINS" });
    expect(screen.getAllByRole("img")).toHaveLength(1);
    expect(tuneButton()).not.toBeInTheDocument();
    // The button that had focus was just unmounted; focus follows the swap
    // instead of falling back to <body>.
    expect(document.activeElement).toBe(panel.closest(".cell-output-region"));
  });

  it("gives focus back to the Tune button when the panel closes", async () => {
    render(view());
    await userEvent.click(tuneButton() as HTMLElement);
    await screen.findByRole("spinbutton", { name: "BINS" });

    await userEvent.click(screen.getByRole("button", { name: "Close tuning panel" }));

    const reopened = await screen.findByRole("button", { name: "Tune code cell 1" });
    expect(document.activeElement).toBe(reopened);
  });
});

describe("who a change is attributed to", () => {
  it("says the user tuned the cell, never that the agent changed it", () => {
    render(view({ record: tuningRecord() }));
    expect(screen.getByText("You tuned this cell")).toBeInTheDocument();
    expect(screen.queryByText("Agent Suggestion")).not.toBeInTheDocument();
    expect(screen.queryByText("Agent added this cell")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Keep tuned change to code cell 1")).toBeInTheDocument();
    expect(screen.getByLabelText("Discard tuned change to code cell 1")).toBeInTheDocument();
    expect(screen.queryByLabelText("Discard agent change to code cell 1")).not.toBeInTheDocument();
  });

  it("keeps the agent's wording for an agent turn", () => {
    render(view({ turn: agentTurn }));
    expect(screen.getByText("Agent added this cell")).toBeInTheDocument();
    expect(screen.queryByText("You tuned this cell")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Discard agent change to code cell 1")).toBeInTheDocument();
  });

  it("explains a stale tuned change without blaming the agent", () => {
    render(view({ record: tuningRecord({ operations: [{ operationId: "op-1", cellId: "cell-a", ordinal: 0, kind: "source_hunk", state: "stale" }] }) }));
    expect(screen.getByText("Your tuned change can no longer be undone")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("This cell changed after you tuned it");
    expect(screen.queryByText(/agent/i)).not.toBeInTheDocument();
  });

  it("prefers the tune when both records touch the same cell", () => {
    render(view({ record: tuningRecord(), turn: agentTurn }));
    expect(screen.getByText("You tuned this cell")).toBeInTheDocument();
    expect(screen.queryByText("Agent added this cell")).not.toBeInTheDocument();
  });
});

describe("reviewing a tuned cell", () => {
  it("keeps every hunk the cell still shows", async () => {
    const onKeepTuned = vi.fn();
    render(view({ record: tuningRecord(), onKeepTuned }));
    await userEvent.click(screen.getByLabelText("Keep tuned change to code cell 1"));
    expect(onKeepTuned).toHaveBeenCalledWith("rec-1", ["op-1"]);
  });

  it("undoes only the hunks that can still be undone", async () => {
    const onUndoTuned = vi.fn();
    render(view({
      record: tuningRecord({
        operations: [
          { operationId: "op-1", cellId: "cell-a", ordinal: 0, kind: "source_hunk", state: "pending" },
          { operationId: "op-2", cellId: "cell-a", ordinal: 1, kind: "source_hunk", state: "accepted" },
        ],
      }),
      onUndoTuned,
    }));
    await userEvent.click(screen.getByLabelText("Discard tuned change to code cell 1"));
    expect(onUndoTuned).toHaveBeenCalledWith("rec-1", ["op-1"]);
  });
});

// Regression: a tune the user has finished with must hand the cell back.
//
// `with_state` leaves accepted/rejected operations in the record, and the record
// itself only clears on a session change, so "this cell appears in the tuning
// record" stays true for the rest of the session. Keying the routing off that
// meant a settled tune silently swallowed every later agent edit to the cell.
describe("a settled tuning record releases the cell", () => {
  const settled = (state: "accepted" | "rejected") => tuningRecord({
    // Mirrors reconcileTuningChanges: settling a cell strips its change but
    // deliberately keeps the operation.
    changes: [],
    operations: [{ operationId: "op-1", cellId: "cell-a", ordinal: 0, kind: "source_hunk", state }],
  });

  it("keeps reviewing while a hunk is still pending", () => {
    render(view({ record: tuningRecord() }));
    expect(screen.getByText("You tuned this cell")).toBeInTheDocument();
  });

  it.each(["accepted", "rejected"] as const)(
    "shows the agent's later edit once the tune is %s", (state) => {
      render(view({ record: settled(state), turn: agentTurn }));
      expect(screen.getByText("Agent added this cell")).toBeInTheDocument();
      expect(screen.queryByText("You tuned this cell")).not.toBeInTheDocument();
    },
  );

  it("offers the agent's per-cell undo once the tune is settled", async () => {
    const onRevert = vi.fn();
    render(<NotebookView notebook={notebook()} scope={scope} turn={agentTurn}
      tuningRecord={settled("accepted")} tuningControls={controls()}
      tunableCellIds={new Set(["cell-a"])}
      disabled={false} sourceActionsDisabled={false} autoSave={false} focusRequest={null}
      onDirtyChange={vi.fn()} onSave={vi.fn()} onRun={vi.fn()} onScope={vi.fn()} onScopeMany={vi.fn()}
      onRevert={onRevert} onKeepOperation={vi.fn()} onUndoOperation={vi.fn()} onKeepCell={vi.fn()}
      onKeepTuned={vi.fn()} onUndoTuned={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Discard agent change to code cell 1" }));
    // The agent's turn, not the finished tune.
    expect(onRevert).toHaveBeenCalledWith("turn-1", "cell-a");
  });
});

// Regression: a shadow kernel must never outlive the panel that asked for it.
//
// The panel is swapped out wholesale rather than re-rendered with open=false —
// the cell key carries the session id, so switching notebooks unmounts it — and
// at that moment the shadow id is still unknown because warm has not returned.
// Left alone, the in-flight warm resolves, adopts a shadow nobody is watching,
// and a full duplicate kernel survives until the backend's idle timeout.
describe("an in-flight warm-up does not leak its kernel", () => {
  it("discards the shadow when the panel unmounts mid-replay", async () => {
    let release: (result: TuningWarmResult) => void = () => {};
    const onWarm = vi.fn().mockReturnValue(
      new Promise<TuningWarmResult>((resolve) => { release = resolve; }),
    );
    const onDiscardShadow = vi.fn();
    const tuning = controls({ onWarm, onDiscardShadow });
    const { unmount } = render(view({ tuning }));

    await userEvent.click(tuneButton()!);
    await screen.findByText(/replaying/i);
    expect(onDiscardShadow).not.toHaveBeenCalled();

    unmount();
    release(warmed);
    await vi.waitFor(() => expect(onDiscardShadow).toHaveBeenCalledWith("shadow-1"));
  });
});
