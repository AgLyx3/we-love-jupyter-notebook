import Icon from "../ui/Icon";
import { useEffect, useState } from "react";
import type { TuningBounds, TuningKnob, TuningKnobKind, TuningValue } from "../api/client";
import { parseKnobNumber, sameValue, stepFor, widen } from "./knobs";

/** A number field that keeps the text the user is typing.
 *
 *  Without the draft, "" and "-" and "1e" would each parse to nothing and snap
 *  the field back to the committed value mid-keystroke, which makes typing a
 *  new value impossible. The draft is dropped as soon as the value arrives
 *  from somewhere else (a reset, a re-warm), so the field never lies. */
function NumberField({ label, value, kind, disabled, className, onChange }: {
  label: string; value: number; kind: TuningKnobKind; disabled: boolean; className?: string;
  onChange: (value: number) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  useEffect(() => {
    setDraft((current) => (current !== null && parseKnobNumber(current, kind) === value ? current : null));
  }, [value, kind]);
  return <input
    type="number"
    // Deliberately no min/max: the bounds are a slider hint, and the browser
    // clamping this field is exactly the behaviour §3.3 rules out.
    className={className}
    aria-label={label}
    disabled={disabled}
    value={draft ?? String(value)}
    onChange={(event) => {
      setDraft(event.target.value);
      const parsed = parseKnobNumber(event.target.value, kind);
      if (parsed !== null) onChange(parsed);
    }}
  />;
}

/** One knob: the control §3.3 assigns to its type.
 *
 *  The min/max editors are gone (stitch-diff B8, accepted in §H) — the row is
 *  now name + value + slider, as the approved design draws it. Widening still
 *  works, because it was never the bound fields that did it: typing a value
 *  past an end moves that end out of the way (`widen`), and that is the
 *  behaviour §3.3 actually rules on. What is lost is setting a bound *without*
 *  moving the value; `onBoundsChange` stays for the widen path. */
export default function KnobControl({ knob, value, bounds, disabled, onChange, onBoundsChange, onReset }: {
  knob: TuningKnob; value: TuningValue; bounds: TuningBounds | null; disabled: boolean;
  onChange: (value: TuningValue) => void;
  onBoundsChange: (bounds: TuningBounds) => void;
  onReset: () => void;
}) {
  const changed = !sameValue(value, knob.value);
  const numeric = (knob.kind === "int" || knob.kind === "float") && bounds !== null;
  const step = numeric && bounds ? stepFor(knob.kind, bounds) : 1;

  // Typing past an end moves that end out of the way. Clamping here instead
  // would make the heuristic a cage — see `widen`.
  const setNumber = (next: number) => {
    if (bounds) onBoundsChange(widen(bounds, next));
    onChange(next);
  };

  return <div className="knob" role="group" aria-label={knob.name}>
    <div className="knob-head">
      <code className="knob-name">{knob.name}</code>
      <span className="knob-origin">cell {knob.cellIndex + 1}</span>
      {changed && <button type="button" className="knob-reset" aria-label={`Reset ${knob.name}`} disabled={disabled} onClick={onReset}><Icon name="undo" /></button>}
      {numeric && bounds && <NumberField className="knob-value" label={knob.name} kind={knob.kind} disabled={disabled}
        value={typeof value === "number" ? value : 0} onChange={setNumber} />}
    </div>

    {numeric && bounds && <input
      type="range"
      className="knob-slider"
      aria-label={`${knob.name} slider`}
      disabled={disabled}
      min={bounds.minimum}
      max={bounds.maximum}
      step={step}
      value={typeof value === "number" ? value : 0}
      onChange={(event) => setNumber(parseKnobNumber(event.target.value, knob.kind) ?? 0)}
    />}

    {knob.kind === "bool" && <label className="knob-toggle">
      <input type="checkbox" aria-label={knob.name} disabled={disabled}
        checked={value === true} onChange={(event) => onChange(event.target.checked)} />
      <span>{value === true ? "True" : "False"}</span>
    </label>}

    {/* No dropdown: a type-only scan has no vocabulary to populate one with. */}
    {knob.kind === "str" && <input type="text" className="knob-text" aria-label={knob.name} disabled={disabled}
      value={typeof value === "string" ? value : ""} onChange={(event) => onChange(event.target.value)} />}

    {/* A sequence has no single axis to slide along, so it gets one field per
        element and no slider. */}
    {(knob.kind === "tuple" || knob.kind === "list") && <div className="knob-items">
      {(Array.isArray(value) ? value : []).map((item, index) => <NumberField
        key={index}
        className="knob-item"
        label={`${knob.name} item ${index + 1}`}
        kind={knob.kind}
        disabled={disabled}
        value={item}
        onChange={(next) => {
          const items = (Array.isArray(value) ? [...value] : []);
          items[index] = next;
          onChange(items);
        }}
      />)}
    </div>}
  </div>;
}
