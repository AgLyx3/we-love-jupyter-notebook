import { configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  CellOutput, TuningKnob, TuningPlan, TuningPreview, TuningRecord, TuningWarmResult,
} from "../api/client";
import TuningPanel, { type TuningPanelProps } from "./TuningPanel";
import type { KnobValues } from "./knobs";

// The panel drives three chained promises before the knobs exist, and the full
// suite runs these files in parallel; the default 1s budget flakes under load.
configure({ asyncUtilTimeout: 5000 });

const image = (data: string): CellOutput => ({ output_type: "display_data", data: { "image/png": data }, metadata: {} });
const errorOutput: CellOutput = { output_type: "error", ename: "ZeroDivisionError", evalue: "division by zero", traceback: ["  File <cell>, line 3"] };

const bins: TuningKnob = { name: "BINS", cellId: "c1", cellIndex: 0, kind: "int", value: 30, bounds: { minimum: 6, maximum: 150, step: 1 } };
const alpha: TuningKnob = { name: "ALPHA", cellId: "c1", cellIndex: 0, kind: "float", value: 0.75, bounds: { minimum: 0.15, maximum: 3.75, step: 0.036 } };

const plan = (over: Partial<TuningPlan> = {}): TuningPlan => ({
  cellId: "c9", revision: 4, knobs: [bins, alpha], rejections: [], blocked: null, risky: [], ...over,
});
const warmResult = (over: Partial<TuningWarmResult> = {}): TuningWarmResult => ({
  shadowId: "shadow-1", knobs: [bins, alpha], rejections: [], outputs: [image("COMMITTED")],
  timings: [], totalSeconds: 1.5, failed: false, failedCellId: null, ...over,
});
const previewResult = (over: Partial<TuningPreview> = {}): TuningPreview => ({
  outputs: [image("PREVIEW-1")], values: { BINS: 40, ALPHA: 0.75 }, executedCellIds: ["c1"],
  seconds: 0.4, cached: false, failed: false, failedCellId: null, ...over,
});
const record = (over: Partial<TuningRecord> = {}): TuningRecord => ({
  recordId: "rec-1", origin: "tune", sessionId: "s", state: "applied", knobs: ["BINS"],
  baseRevision: 4, appliedRevision: 5, executionOperationId: "op-1", executionStartIndex: 0,
  reRanFromTop: false, changes: [], operations: [], error: null, createdAt: "2026-08-14T00:00:00Z", ...over,
});

function deferred<T>() {
  let settle: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => { settle = resolve; });
  return { promise, resolve: settle };
}

function mount(over: Partial<TuningPanelProps> = {}) {
  const props: TuningPanelProps = {
    cellId: "c9",
    open: true,
    onClose: vi.fn(),
    onScan: vi.fn().mockResolvedValue(plan()),
    onWarm: vi.fn().mockResolvedValue(warmResult()),
    onPreview: vi.fn().mockResolvedValue(previewResult()),
    onApply: vi.fn().mockResolvedValue(record()),
    onDiscardShadow: vi.fn(),
    // Zero-length debounce: these tests are about what the panel shows, not
    // about how long it waits before asking.
    previewDebounceMs: 0,
    ...over,
  };
  return { props, ...render(<TuningPanel {...props} />) };
}

const band = () => screen.queryByText("Preview — not in your notebook yet");
const readyRail = () => screen.findByRole("spinbutton", { name: "BINS" });

// `<input type="number">` has no text selection, so userEvent.clear() cannot be
// relied on to empty it before typing — under load it leaves the old value in
// place and the new digits land on the end of it. One change event carrying the
// whole field is what a select-all-and-retype actually produces anyway.
const retype = (element: HTMLElement, text: string) => fireEvent.change(element, { target: { value: text } });

afterEach(() => vi.restoreAllMocks());

describe("the preview band", () => {
  it("appears as soon as a knob differs and goes when it is put back", async () => {
    mount();
    const field = await readyRail();
    expect(band()).not.toBeInTheDocument();

    retype(field, "40");
    expect(band()).toBeInTheDocument();

    retype(field, "30");
    await waitFor(() => expect(band()).not.toBeInTheDocument());
  });

  it("keeps a half-typed field instead of snapping it back", async () => {
    mount();
    const field = await readyRail();
    retype(field, "40");

    // Emptying the field is a step on the way to another number, not a request
    // to render nothing; the knob keeps the last value that parsed.
    retype(field, "");
    expect(field).toHaveValue(null);
    expect(band()).toBeInTheDocument();

    retype(field, "45");
    expect(screen.getByRole("button", { name: "Apply 1 change and re-run" })).toBeInTheDocument();
  });

  it("stays up while a failed preview is on screen", async () => {
    mount({ onPreview: vi.fn().mockResolvedValue(previewResult({ outputs: [errorOutput], failed: true })) });
    retype(await readyRail(), "0");

    expect(await screen.findByText(/ZeroDivisionError: division by zero/)).toBeInTheDocument();
    expect(band()).toBeInTheDocument();
    // The knobs survive the crash, and so does the shadow behind them.
    expect(screen.getByRole("spinbutton", { name: "BINS" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply 1 change and re-run" })).toBeEnabled();
  });
});

// The min/max fields were dropped by stitch-diff B8 (accepted in §H), so a
// bound can no longer be set on its own — but widening was never those fields'
// job. Typing a value past an end is what moves the end, and that is what §3.3
// actually rules on, so it is still covered here.
describe("bounds", () => {
  it("widens the range around a value typed outside it, never clamping", async () => {
    mount();
    const field = await readyRail();

    retype(field, "900");

    const slider = screen.getByRole("slider", { name: "BINS slider" });
    expect(slider).toHaveAttribute("max", "900");
    expect(slider).toHaveValue("900");
    expect(field).toHaveValue(900);
    // No standalone bound editor to check any more; the slider is the bound.
    expect(screen.queryByRole("spinbutton", { name: "BINS maximum" })).not.toBeInTheDocument();
  });
});

describe("the apply button", () => {
  it("counts the knobs that changed", async () => {
    mount();
    const field = await readyRail();

    expect(screen.getByRole("button", { name: "Apply and re-run" })).toBeDisabled();

    retype(field, "40");
    expect(screen.getByRole("button", { name: "Apply 1 change and re-run" })).toBeInTheDocument();

    retype(screen.getByRole("spinbutton", { name: "ALPHA" }), "0.2");
    expect(screen.getByRole("button", { name: "Apply 2 changes and re-run" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Reset ALPHA" }));
    expect(await screen.findByRole("button", { name: "Apply 1 change and re-run" })).toBeInTheDocument();
  });

  it("sends only the changed knobs and reports a re-run of the whole notebook", async () => {
    const onApply = vi.fn().mockResolvedValue(record({ reRanFromTop: true, knobs: ["BINS"] }));
    const onApplied = vi.fn();
    mount({ onApply, onApplied });
    retype(await readyRail(), "40");

    await userEvent.click(await screen.findByRole("button", { name: "Apply 1 change and re-run" }));

    await waitFor(() => expect(onApply).toHaveBeenCalledWith("shadow-1", { BINS: 40 } as KnobValues));
    expect(await screen.findByText(/the whole notebook was re-run/)).toBeInTheDocument();
    expect(onApplied).toHaveBeenCalledTimes(1);
  });
});

describe("the empty state", () => {
  it("gives the scan's own reasons rather than 'nothing found'", async () => {
    const onWarm = vi.fn();
    mount({
      onWarm,
      onScan: vi.fn().mockResolvedValue(plan({
        knobs: [],
        rejections: [
          { name: "threshold", reason: "rebound", detail: "`threshold` is assigned more than once before this cell, so one literal does not decide it." },
          { name: "rows", reason: "not_literal", detail: "`rows` is computed rather than set to a fixed value." },
        ],
      })),
    });

    expect(await screen.findByText(/`threshold` is assigned more than once before this cell/)).toBeInTheDocument();
    expect(screen.getByText(/`rows` is computed rather than set to a fixed value/)).toBeInTheDocument();
    // Nothing to preview means nothing to spend: no kernel is started.
    expect(onWarm).not.toHaveBeenCalled();
  });

  it("shows why a whole chain was refused", async () => {
    mount({ onScan: vi.fn().mockResolvedValue(plan({ knobs: [], blocked: "The plot cell runs a non-Python cell magic." })) });
    expect(await screen.findByText("The plot cell runs a non-Python cell magic.")).toBeInTheDocument();
  });
});

describe("before anything executes", () => {
  it("waits for approval when the replay would re-fire flagged cells", async () => {
    const onWarm = vi.fn().mockResolvedValue(warmResult());
    mount({
      onWarm,
      onScan: vi.fn().mockResolvedValue(plan({
        risky: [{ cellId: "c3", cellIndex: 2, categories: ["network"], reasons: ["Calls requests.get"] }],
      })),
    });

    expect(await screen.findByText(/Calls requests.get/)).toBeInTheDocument();
    expect(onWarm).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Approve and warm up" }));
    await waitFor(() => expect(onWarm).toHaveBeenCalledTimes(1));
  });
});

describe("warming", () => {
  it("names the cell being replayed instead of showing a bare spinner", async () => {
    const warm = deferred<TuningWarmResult>();
    mount({
      onWarm: vi.fn().mockReturnValue(warm.promise),
      warmTimings: [{ cellId: "c1", ordinal: 1, total: 7, seconds: 20, failed: false }],
    });

    expect(await screen.findByText(/Replaying cell 2 of 7/)).toBeInTheDocument();
    warm.resolve(warmResult());
    await readyRail();
  });
});

describe("dirty-previewing", () => {
  it("puts the spinner over the last preview instead of blanking it", async () => {
    const second = deferred<TuningPreview>();
    // Every keystroke supersedes the previous request, so the second render is
    // whichever of them is still outstanding when the deferred settles.
    const onPreview = vi.fn()
      .mockResolvedValueOnce(previewResult({ outputs: [image("PREVIEW-1")] }))
      .mockReturnValue(second.promise);
    mount({ onPreview });
    const field = await readyRail();

    retype(field, "40");
    await waitFor(() => expect(screen.getByRole("img")).toHaveAttribute("src", expect.stringContaining("PREVIEW-1")));

    retype(field, "41");
    expect(await screen.findByText("Rendering preview…")).toBeInTheDocument();
    // The picture the new value is being compared against is still there.
    expect(screen.getByRole("img")).toHaveAttribute("src", expect.stringContaining("PREVIEW-1"));

    second.resolve(previewResult({ outputs: [image("PREVIEW-2")] }));
    await waitFor(() => expect(screen.getByRole("img")).toHaveAttribute("src", expect.stringContaining("PREVIEW-2")));
  });

  it("shows the committed output again once every knob is back", async () => {
    mount();
    const field = await readyRail();
    retype(field, "40");
    await waitFor(() => expect(screen.getByRole("img")).toHaveAttribute("src", expect.stringContaining("PREVIEW-1")));

    retype(field, "30");
    await waitFor(() => expect(screen.getByRole("img")).toHaveAttribute("src", expect.stringContaining("COMMITTED")));
  });
});

describe("the shadow", () => {
  it("is torn down when the panel closes", async () => {
    const onDiscardShadow = vi.fn();
    const { rerender, props } = mount({ onDiscardShadow });
    await readyRail();

    rerender(<TuningPanel {...props} open={false} />);

    expect(onDiscardShadow).toHaveBeenCalledWith("shadow-1");
    expect(screen.queryByRole("region", { name: "Plot tuning" })).not.toBeInTheDocument();
  });

  it("is re-scanned when the document moves under it", async () => {
    const onScan = vi.fn().mockResolvedValue(plan());
    const onDiscardShadow = vi.fn();
    const { rerender, props } = mount({ onScan, onDiscardShadow, revision: 4 });
    await readyRail();
    expect(onScan).toHaveBeenCalledTimes(1);

    rerender(<TuningPanel {...props} onScan={onScan} onDiscardShadow={onDiscardShadow} revision={5} />);

    await waitFor(() => expect(onScan).toHaveBeenCalledTimes(2));
    expect(onDiscardShadow).toHaveBeenCalledWith("shadow-1");
  });
});

// Reported from using it: applying replaced the plot with a text receipt, at
// exactly the moment the plot is worth looking at. The count is already on the
// review bar; what the panel owes here is the picture.
describe("after applying", () => {
  const applied = async () => {
    mount({ onApply: vi.fn().mockResolvedValue(record({ knobs: ["BINS"] })) });
    retype(await readyRail(), "40");
    await screen.findByAltText(/Cell output/);
    await userEvent.click(await screen.findByRole("button", { name: "Apply 1 change and re-run" }));
    await screen.findByText(/Applied 1 change/);
  };

  it("keeps the plot on screen instead of replacing it with a receipt", async () => {
    await applied();
    // The rendered preview is still there — the applied values produced it.
    expect(screen.getByAltText(/Cell output/)).toBeInTheDocument();
  });

  it("drops the not-in-your-notebook band, because now it is", async () => {
    await applied();
    expect(screen.queryByText("Preview — not in your notebook yet")).not.toBeInTheDocument();
  });

  it("keeps the knobs visible at their applied values", async () => {
    await applied();
    expect(screen.getByRole("spinbutton", { name: "BINS" })).toHaveValue(40);
  });

  it("offers a re-scan rather than pretending the knobs are still live", async () => {
    // The shadow's source ranges no longer match the file it just changed, so
    // tuning on cannot reuse it.
    await applied();
    expect(screen.getByRole("button", { name: "Tune again" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Apply/ })).not.toBeInTheDocument();
  });
});
