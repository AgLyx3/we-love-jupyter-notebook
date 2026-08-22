import type { TuningBounds, TuningKnob, TuningKnobKind, TuningTiming, TuningValue } from "../api/client";

export type KnobValues = Record<string, TuningValue>;

/** Value equality that compares tuple/list knobs elementwise. `===` on arrays
 *  would report every sequence knob as changed on any re-render, which would
 *  leave the "not in your notebook yet" band permanently on. */
export function sameValue(left: TuningValue | undefined, right: TuningValue | undefined): boolean {
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
    return left.every((item, index) => Object.is(item, right[index]));
  }
  return Object.is(left, right);
}

/** The one place "has anything moved off its committed value?" is decided.
 *  Everything user-visible about dirtiness — the band, the border, the Apply
 *  count, whether a preview runs at all — derives from this, so the panel
 *  cannot end up showing the band in one state and not in another. */
export function changedKnobs(knobs: TuningKnob[], values: KnobValues): TuningKnob[] {
  return knobs.filter((knob) => !sameValue(values[knob.name], knob.value));
}

/** Only the knobs that moved. Both preview and apply take a subset: the
 *  backend fills the rest in from the committed values. */
export function changedValues(knobs: TuningKnob[], values: KnobValues): KnobValues {
  const changed: KnobValues = {};
  for (const knob of changedKnobs(knobs, values)) changed[knob.name] = values[knob.name];
  return changed;
}

export function committedValues(knobs: TuningKnob[]): KnobValues {
  const values: KnobValues = {};
  for (const knob of knobs) values[knob.name] = knob.value;
  return values;
}

/** Bounds the rail starts from. The backend always sends them for int/float;
 *  the fallback only exists so a missing one degrades to a usable slider
 *  instead of `NaN`. */
export function boundsFor(knob: TuningKnob): TuningBounds | null {
  if (knob.kind !== "int" && knob.kind !== "float") return null;
  if (knob.bounds) return knob.bounds;
  const value = typeof knob.value === "number" ? knob.value : 0;
  return { minimum: Math.min(value, 0), maximum: Math.max(value, 1), step: knob.kind === "int" ? 1 : 0.01 };
}

/** A value outside the range widens the range — it is never clamped to it.
 *  `ALPHA = 0.75` gets suggested 0.15–3.75, which is wrong for an alpha
 *  channel, and nothing type-based can ever know that. The heuristic is a
 *  starting point, so one keystroke has to be enough to leave it behind. */
export function widen(bounds: TuningBounds, value: number): TuningBounds {
  if (!Number.isFinite(value) || (value >= bounds.minimum && value <= bounds.maximum)) return bounds;
  return { ...bounds, minimum: Math.min(bounds.minimum, value), maximum: Math.max(bounds.maximum, value) };
}

/** Slider granularity, recomputed from the range in front of the user rather
 *  than taken from the knob: the backend's step is computed for the backend's
 *  bounds and goes stale the moment those bounds are widened. */
export function stepFor(kind: TuningKnobKind, bounds: TuningBounds): number {
  if (kind === "int") return 1;
  const span = bounds.maximum - bounds.minimum;
  return span > 0 ? span / 100 : 0.01;
}

/** Parse a typed number for a knob, or null while the field is mid-edit ("",
 *  "-", "1e"). Int knobs round rather than truncate so the value that gets
 *  applied is the one that was on screen. */
export function parseKnobNumber(text: string, kind: TuningKnobKind): number | null {
  const parsed = Number(text);
  if (text.trim() === "" || !Number.isFinite(parsed)) return null;
  return kind === "int" ? Math.round(parsed) : parsed;
}

/** Apply spends a live chain re-run, so the button says how much it is about
 *  to change and that it re-runs. Neither half should be a surprise. The
 *  countless wording is only ever seen on the disabled button. */
export function applyLabel(count: number): string {
  if (count === 0) return "Apply and re-run";
  return `Apply ${count} change${count === 1 ? "" : "s"} and re-run`;
}

/** "Replaying cell 2 of 7 — 47s". A two-minute replay behind an
 *  undifferentiated spinner reads as a hang; the ordinal and the clock are
 *  what say it is still moving. Timings only exist for cells that have
 *  *finished*, so the one in flight is the next ordinal along. */
export function warmProgressLabel(timings: TuningTiming[], elapsedSeconds: number): string {
  const seconds = `${Math.max(0, Math.round(elapsedSeconds))}s`;
  const last = timings[timings.length - 1];
  if (!last) return `Replaying the cells above — ${seconds}`;
  const current = Math.min(last.ordinal + 1, last.total);
  return `Replaying cell ${current} of ${last.total} — ${seconds}`;
}
