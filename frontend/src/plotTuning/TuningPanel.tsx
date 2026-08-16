import { ShieldAlert, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type {
  CellOutput, TuningBounds, TuningKnob, TuningPlan, TuningPreview, TuningRecord,
  TuningTiming, TuningWarmResult,
} from "../api/client";
import { Outputs } from "../notebook/NotebookCell";
import KnobControl from "./KnobControl";
import {
  applyLabel, boundsFor, changedKnobs, changedValues, committedValues, warmProgressLabel,
  type KnobValues,
} from "./knobs";

/** The states of §3.5, as the panel actually renders them.
 *
 *  `dirty` is *derived*, never stored, so the four "ready" states cannot
 *  disagree with the preview band about whether anything has moved. `stage`
 *  below only records what the panel is waiting on. */
export type TuningState =
  | "closed" | "scanning" | "empty" | "risky-confirm" | "warming"
  | "ready-clean" | "dirty-previewing" | "dirty-ready" | "preview-failed"
  | "applying" | "applied" | "error";

/** What the panel is waiting on. Deliberately coarser than `TuningState`:
 *  "ready" covers all four states the knobs are live in, and which of them is
 *  showing follows from the values themselves. */
type Stage = "closed" | "scanning" | "empty" | "risky-confirm" | "warming" | "ready" | "applying" | "applied" | "error";

interface Failure { title: string; detail: string; outputs?: CellOutput[]; retry?: boolean }

export interface TuningPanelProps {
  cellId: string;
  /** Parent-controlled, so the entry point can live on the cell's output. */
  open: boolean;
  /** The host is expected to honour this by setting `open` to false: the panel
   *  reports that it is done rather than deciding its own visibility. */
  onClose: () => void;
  /** POST /tuning/open — analysis only, starts no kernel. */
  onScan: (cellId: string) => Promise<TuningPlan>;
  /** POST /tuning/warm — the first thing that costs anything. */
  onWarm: (cellId: string) => Promise<TuningWarmResult>;
  onPreview: (shadowId: string, values: KnobValues) => Promise<TuningPreview>;
  onApply: (shadowId: string, values: KnobValues) => Promise<TuningRecord>;
  /** Cancel + DELETE the shadow. Called on close, on unmount, and when a
   *  cancelled warm-up's shadow finally turns up. */
  onDiscardShadow?: (shadowId: string) => void;
  onApplied?: (record: TuningRecord) => void;
  /** Live document revision. When it moves the shadow replayed source that no
   *  longer exists, so the panel re-scans rather than serving a stale preview. */
  revision?: number;
  /** Chain cells finished so far, if the host has a progress channel. Warm is
   *  one blocking request today, so this is usually empty until it returns and
   *  the elapsed clock carries the "still moving" signal on its own. */
  warmTimings?: TuningTiming[];
  /** Concurrent previews supersede each other; this is the debounce in front. */
  previewDebounceMs?: number;
}

const boundsIndex = (knobs: TuningKnob[]): Record<string, TuningBounds> => {
  const index: Record<string, TuningBounds> = {};
  for (const knob of knobs) {
    const bounds = boundsFor(knob);
    if (bounds) index[knob.name] = bounds;
  }
  return index;
};

const messageOf = (error: unknown) => (error instanceof Error ? error.message : String(error));

export default function TuningPanel(props: TuningPanelProps) {
  const { cellId, open, previewDebounceMs = 250 } = props;

  // Callbacks live in a ref because hosts pass them inline: putting them in an
  // effect's dependencies would re-scan (and re-warm) on every parent render.
  const callbacks = useRef(props);
  useEffect(() => { callbacks.current = props; });

  const [stage, setStage] = useState<Stage>("closed");
  const [plan, setPlan] = useState<TuningPlan | null>(null);
  const [knobs, setKnobs] = useState<TuningKnob[]>([]);
  const [values, setValues] = useState<KnobValues>({});
  const [bounds, setBounds] = useState<Record<string, TuningBounds>>({});
  const [shadowId, setShadowId] = useState<string | null>(null);
  const [committedOutputs, setCommittedOutputs] = useState<CellOutput[]>([]);
  const [previewOutputs, setPreviewOutputs] = useState<CellOutput[] | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewFailed, setPreviewFailed] = useState(false);
  const [timings, setTimings] = useState<TuningTiming[]>([]);
  const [elapsed, setElapsed] = useState(0);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [record, setRecord] = useState<TuningRecord | null>(null);
  const [nonce, setNonce] = useState(0);

  const shadowRef = useRef<string | null>(null);
  const warmToken = useRef(0);
  const previewToken = useRef(0);
  const revisionRef = useRef<number | null>(props.revision ?? null);

  const changed = useMemo(() => changedKnobs(knobs, values), [knobs, values]);
  // The single source of truth for "this is not your notebook". The band, the
  // border, the Apply count and whether a preview runs at all all read this.
  const dirty = changed.length > 0;

  const state: TuningState = useMemo(() => {
    if (!open || stage === "closed") return "closed";
    if (stage !== "ready") return stage;
    if (previewFailed) return "preview-failed";
    if (!dirty) return "ready-clean";
    return previewing ? "dirty-previewing" : "dirty-ready";
  }, [open, stage, dirty, previewing, previewFailed]);

  const setShadow = (id: string | null) => { shadowRef.current = id; setShadowId(id); };
  const discardShadow = () => {
    const id = shadowRef.current;
    shadowRef.current = null;
    setShadowId(null);
    if (id) callbacks.current.onDiscardShadow?.(id);
  };

  const warmUp = () => {
    setStage("warming");
    setTimings([]);
    setElapsed(0);
    setFailure(null);
    const token = ++warmToken.current;
    callbacks.current.onWarm(cellId).then((result) => {
      if (token !== warmToken.current) {
        // Cancelled while it was in flight. Warm is a single blocking request
        // that only reveals the shadow id when it finishes, so "cancel" means
        // stop waiting and tear the shadow down the moment it exists.
        callbacks.current.onDiscardShadow?.(result.shadowId);
        return;
      }
      setShadow(result.shadowId);
      setTimings(result.timings);
      if (result.failed) {
        // The notebook itself is broken at its committed values. Previews stay
        // refused until a re-warm, so this is an error state, not a preview.
        setFailure({ title: "The cells above could not be replayed", detail: "Your notebook is unchanged. Fix the cell below and try again.", outputs: result.outputs, retry: true });
        setStage("error");
        return;
      }
      setKnobs(result.knobs);
      setValues(committedValues(result.knobs));
      setBounds(boundsIndex(result.knobs));
      setCommittedOutputs(result.outputs);
      setPreviewOutputs(null);
      setPreviewFailed(false);
      setStage("ready");
    }).catch((error) => {
      if (token !== warmToken.current) return;
      setFailure({ title: "Could not warm up a preview kernel", detail: messageOf(error), retry: true });
      setStage("error");
    });
  };

  // Retire both tokens before re-scanning. A warm still in flight would
  // otherwise resolve against the *new* scan, be adopted as current, and then be
  // overwritten by the second warm's shadow — leaking the first kernel, and in
  // the revision-moved case leaving the panel "ready" against source the
  // notebook no longer has. A superseded preview would likewise land after the
  // reset and pin `previewFailed` on with every knob back at its committed value.
  const rescan = () => {
    warmToken.current += 1;
    previewToken.current += 1;
    discardShadow();
    setNonce((value) => value + 1);
  };

  // Scan on open. Nothing here starts a kernel — that is what lets the risky
  // confirmation sit between the analysis and the first execution.
  useEffect(() => {
    if (!open) {
      warmToken.current += 1;
      previewToken.current += 1;
      discardShadow();
      setStage("closed");
      return;
    }
    let live = true;
    setStage("scanning");
    setFailure(null);
    setRecord(null);
    setPreviewOutputs(null);
    setPreviewFailed(false);
    setCommittedOutputs([]);
    callbacks.current.onScan(cellId).then((next) => {
      if (!live) return;
      setPlan(next);
      setKnobs(next.knobs);
      setValues(committedValues(next.knobs));
      setBounds(boundsIndex(next.knobs));
      if (next.blocked || next.knobs.length === 0) { setStage("empty"); return; }
      if (next.risky.length > 0) { setStage("risky-confirm"); return; }
      warmUp();
    }).catch((error) => {
      if (!live) return;
      setFailure({ title: "Could not read this cell", detail: messageOf(error), retry: true });
      setStage("error");
    });
    return () => { live = false; };
    // Deliberately keyed on identity, not on the callbacks: `nonce` is the
    // explicit "scan again" signal (retry, revision moved, tune again).
  }, [open, cellId, nonce]);

  // Discard the shadow when the panel goes away entirely.
  //
  // Retiring the warm token here is what covers unmount *during* a replay. The
  // panel is swapped out wholesale rather than re-rendered with open=false (the
  // cell key carries the session id, so switching notebooks unmounts it), and at
  // that moment `shadowRef` is still null because warm has not returned. Without
  // the bump the in-flight warm resolves, matches the token, adopts a shadow
  // nobody is watching, and a full duplicate kernel survives until the backend's
  // idle timeout. With it, warm takes its superseded branch and sends the DELETE.
  useEffect(() => () => {
    warmToken.current += 1;
    previewToken.current += 1;
    if (shadowRef.current) callbacks.current.onDiscardShadow?.(shadowRef.current);
  }, []);

  // §3.6: a document revision change invalidates the shadow. Re-scan rather
  // than serve a preview of source that no longer exists. Our own Apply moves
  // the revision too, which is why the applying/applied stages are excluded.
  useEffect(() => {
    const next = props.revision;
    if (next === undefined) return;
    const previous = revisionRef.current;
    revisionRef.current = next;
    if (previous === null || previous === next) return;
    if (!open || stage === "applying" || stage === "applied") return;
    rescan();
    // Only the revision belongs in here: re-running this on a stage change
    // would re-scan the panel out from under its own Apply.
  }, [props.revision]);

  // A warm-up is the one wait long enough to read as a hang, so it gets a clock.
  useEffect(() => {
    if (stage !== "warming") return;
    const started = Date.now();
    const timer = setInterval(() => setElapsed((Date.now() - started) / 1000), 1000);
    return () => clearInterval(timer);
  }, [stage]);

  const changedKey = useMemo(() => JSON.stringify(changedValues(knobs, values)), [knobs, values]);

  useEffect(() => {
    if (stage !== "ready") return;
    if (!dirty) {
      // Back at the committed values: the real output is the truth again.
      previewToken.current += 1;
      setPreviewing(false);
      setPreviewFailed(false);
      setPreviewOutputs(null);
      return;
    }
    const id = shadowRef.current;
    if (!id) return;
    const timer = setTimeout(() => {
      const token = ++previewToken.current;
      setPreviewing(true);
      callbacks.current.onPreview(id, JSON.parse(changedKey) as KnobValues).then((result) => {
        if (token !== previewToken.current) return; // superseded; latest wins
        setPreviewing(false);
        setPreviewOutputs(result.outputs);
        setPreviewFailed(result.failed);
      }).catch((error) => {
        if (token !== previewToken.current) return;
        setPreviewing(false);
        setFailure({ title: "The preview could not be rendered", detail: messageOf(error), retry: true });
        setStage("error");
      });
    }, previewDebounceMs);
    return () => clearTimeout(timer);
    // `changedKey` rather than `values`: a re-render that produces an equal
    // value set must not spend another preview.
  }, [stage, dirty, changedKey, shadowId, previewDebounceMs]);

  const apply = () => {
    const id = shadowRef.current;
    if (!id || !dirty) return;
    const payload = changedValues(knobs, values);
    setStage("applying");
    callbacks.current.onApply(id, payload).then((applied) => {
      setRecord(applied);
      revisionRef.current = applied.appliedRevision ?? revisionRef.current;
      // The document moved, so the shadow is now replaying source that is gone.
      discardShadow();
      setStage("applied");
      callbacks.current.onApplied?.(applied);
    }).catch((error) => {
      setFailure({ title: "Could not apply these values", detail: messageOf(error), retry: true });
      setStage("error");
    });
  };

  const close = () => { warmToken.current += 1; discardShadow(); setStage("closed"); callbacks.current.onClose(); };
  // Cancelling a warm-up leaves a request in flight; the token is what makes
  // its shadow get torn down on arrival instead of becoming this panel's.
  const cancelWarm = close;
  const resetAll = () => setValues(committedValues(knobs));

  if (state === "closed") return null;

  const shown = dirty ? (previewOutputs ?? committedOutputs) : committedOutputs;
  const knobsDisabled = stage !== "ready";

  const preview = <div className={`tuning-preview${dirty ? " dirty" : ""}`}>
    {/* Requirement one: while anything differs, the picture is labelled as not
        being the notebook's. Derived from `dirty` alone so it cannot go missing
        in one state and appear in another. */}
    {dirty && <p className="tuning-preview-band">Preview — not in your notebook yet</p>}
    {state === "preview-failed" && <p className="tuning-preview-failed">These values crash the cell. Your notebook is untouched — the traceback is below.</p>}
    <div className="tuning-preview-frame">
      {shown.length > 0 ? <Outputs outputs={shown} disabled /> : <p className="tuning-preview-blank">This cell has no output yet.</p>}
      {/* Requirement two: the spinner sits *over* the last preview. Blanking it
          would remove the picture the new one is being compared against. */}
      {previewing && <p className="tuning-preview-spinner" role="status">Rendering preview…</p>}
    </div>
  </div>;

  const rail = <div className="tuning-rail">
    <div className="tuning-knobs" role="group" aria-label="Knobs">
      {knobs.map((knob) => <KnobControl
        key={knob.name}
        knob={knob}
        value={values[knob.name]}
        bounds={bounds[knob.name] ?? null}
        disabled={knobsDisabled}
        onChange={(value) => setValues((current) => ({ ...current, [knob.name]: value }))}
        onBoundsChange={(next) => setBounds((current) => ({ ...current, [knob.name]: next }))}
        onReset={() => setValues((current) => ({ ...current, [knob.name]: knob.value }))}
      />)}
    </div>
    <div className="tuning-apply">
      <button type="button" className="primary" disabled={!dirty || stage !== "ready"} onClick={apply}>{applyLabel(changed.length)}</button>
      <button type="button" disabled={!dirty || stage !== "ready"} onClick={resetAll}>Reset all</button>
    </div>
  </div>;

  return <section className="tuning-panel" data-state={state} aria-label="Plot tuning">
    <header className="tuning-panel-head">
      <strong>Tune</strong>
      <button type="button" className="tuning-close" aria-label="Close tuning panel" onClick={close}><X /></button>
    </header>

    {state === "scanning" && <p className="tuning-message" role="status">Looking for values you can tune…</p>}

    {/* The empty state explains itself. "No tunable variables found" would read
        as broken; the scan's own sentences read as honest. */}
    {state === "empty" && <div className="tuning-message">
      <p>Nothing in the cells above can be tuned safely.</p>
      {plan?.blocked && <p className="tuning-blocked">{plan.blocked}</p>}
      {plan && plan.rejections.length > 0 && <ul className="tuning-rejections">
        {plan.rejections.map((rejection) => <li key={rejection.name}>{rejection.detail}</li>)}
      </ul>}
    </div>}

    {state === "risky-confirm" && plan && <div className="tuning-message tuning-risky">
      <p className="risk-title"><ShieldAlert /> Warming up re-runs {plan.risky.length} flagged {plan.risky.length === 1 ? "cell" : "cells"} above this one.</p>
      <p>Nothing runs until you approve. Your notebook and its kernel are not touched either way.</p>
      <ul>{plan.risky.map((cell) => <li key={cell.cellId}>Cell {cell.cellIndex + 1} — {cell.reasons.join(", ") || cell.categories.join(", ")}</li>)}</ul>
      <div className="tuning-actions">
        <button type="button" onClick={close}>Cancel</button>
        <button type="button" className="primary" onClick={warmUp}>Approve and warm up</button>
      </div>
    </div>}

    {state === "error" && failure && <div className="tuning-message tuning-error">
      <p><strong>{failure.title}</strong></p>
      <p>{failure.detail}</p>
      {failure.outputs && failure.outputs.length > 0 && <Outputs outputs={failure.outputs} disabled />}
      <div className="tuning-actions">
        <button type="button" onClick={close}>Close</button>
        {failure.retry && <button type="button" className="primary" onClick={rescan}>Try again</button>}
      </div>
    </div>}

    {state === "applied" && record && <div className="tuning-message tuning-applied">
      <p>Applied {record.knobs.length} {record.knobs.length === 1 ? "change" : "changes"} to your notebook.</p>
      {/* A different bill from the one the Apply button quoted, so it is said
          out loud rather than left to be noticed in the execution counts. */}
      {record.reRanFromTop && <p className="tuning-rerun-note">The kernel had not run the cells above this one, so the whole notebook was re-run.</p>}
      <div className="tuning-actions">
        <button type="button" onClick={close}>Close</button>
        <button type="button" onClick={rescan}>Tune again</button>
      </div>
    </div>}

    {(state === "warming" || stage === "ready" || stage === "applying") && <div className="tuning-panel-body">
      <div className="tuning-preview-column">
        {state === "warming"
          ? <p className="tuning-message" role="status">{warmProgressLabel(props.warmTimings?.length ? props.warmTimings : timings, elapsed)}</p>
          : preview}
        {state === "applying" && <p className="tuning-message" role="status">Applying and re-running…</p>}
      </div>
      {state === "warming"
        ? <div className="tuning-rail"><div className="tuning-actions"><button type="button" onClick={cancelWarm}>Cancel</button></div></div>
        : rail}
    </div>}
  </section>;
}
