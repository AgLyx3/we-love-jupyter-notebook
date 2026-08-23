import { describe, expect, it } from "vitest";
import type { TuningKnob, TuningTiming } from "../api/client";
import { applyLabel, changedKnobs, changedValues, parseKnobNumber, sameValue, stepFor, warmProgressLabel, widen } from "./knobs";

const knob = (over: Partial<TuningKnob> = {}): TuningKnob => ({
  name: "BINS", cellId: "c1", cellIndex: 0, kind: "int", value: 30,
  bounds: { minimum: 6, maximum: 150, step: 1 }, ...over,
});

describe("knob value comparison", () => {
  it("compares tuple knobs elementwise, so a re-render is not a change", () => {
    expect(sameValue([8, 4], [8, 4])).toBe(true);
    expect(sameValue([8, 4], [8, 5])).toBe(false);
    expect(sameValue([8, 4], [8])).toBe(false);
  });

  it("reports only the knobs that moved off their committed value", () => {
    const knobs = [knob(), knob({ name: "ALPHA", kind: "float", value: 0.75 })];
    expect(changedKnobs(knobs, { BINS: 30, ALPHA: 0.75 })).toEqual([]);
    expect(changedKnobs(knobs, { BINS: 40, ALPHA: 0.75 }).map((item) => item.name)).toEqual(["BINS"]);
    expect(changedValues(knobs, { BINS: 40, ALPHA: 0.1 })).toEqual({ BINS: 40, ALPHA: 0.1 });
  });
});

describe("bounds", () => {
  it("widens rather than clamps in both directions", () => {
    const bounds = { minimum: 6, maximum: 150, step: 1 };
    expect(widen(bounds, 900)).toEqual({ minimum: 6, maximum: 900, step: 1 });
    expect(widen(bounds, -5)).toEqual({ minimum: -5, maximum: 150, step: 1 });
    expect(widen(bounds, 40)).toBe(bounds);
  });

  it("leaves the range alone for a value that is not a number yet", () => {
    const bounds = { minimum: 6, maximum: 150, step: 1 };
    expect(widen(bounds, Number.NaN)).toBe(bounds);
  });

  it("recomputes the float step from the current range, not the backend's", () => {
    // The knob arrived with step 0.0135 for 0.15–1.5; once the user widens the
    // range to 0–100 a step from the old range would be uselessly fine.
    expect(stepFor("float", { minimum: 0, maximum: 100, step: 0.0135 })).toBe(1);
    expect(stepFor("int", { minimum: 6, maximum: 900, step: 1 })).toBe(1);
    expect(stepFor("float", { minimum: 5, maximum: 5, step: 0 })).toBe(0.01);
  });
});

describe("typed numbers", () => {
  it("holds off while the field is mid-edit", () => {
    expect(parseKnobNumber("", "int")).toBeNull();
    expect(parseKnobNumber("-", "float")).toBeNull();
  });

  it("rounds int knobs so the applied value is the one on screen", () => {
    expect(parseKnobNumber("3.7", "int")).toBe(4);
    expect(parseKnobNumber("3.7", "float")).toBe(3.7);
  });
});

describe("labels", () => {
  it("says how much Apply is about to change", () => {
    expect(applyLabel(1)).toBe("Apply 1 change and re-run");
    expect(applyLabel(3)).toBe("Apply 3 changes and re-run");
    expect(applyLabel(0)).toBe("Apply and re-run");
  });

  it("names the chain cell being replayed, not just 'working'", () => {
    const timings: TuningTiming[] = [
      { cellId: "a", ordinal: 1, total: 7, seconds: 20, failed: false },
    ];
    expect(warmProgressLabel(timings, 47)).toBe("Replaying cell 2 of 7 — 47s");
    expect(warmProgressLabel([], 3)).toBe("Replaying the cells above — 3s");
  });

  it("does not run off the end of the chain on the last cell", () => {
    const timings: TuningTiming[] = [
      { cellId: "g", ordinal: 7, total: 7, seconds: 2, failed: false },
    ];
    expect(warmProgressLabel(timings, 61)).toBe("Replaying cell 7 of 7 — 61s");
  });
});
