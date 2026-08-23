import Icon from "../ui/Icon";
import ReactMarkdown from "react-markdown";
import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import type { AgentChange, AgentOperation, CellOutput, NotebookCellData, TuningPlan, TuningPreview, TuningRecord, TuningWarmResult } from "../api/client";
import TuningPanel from "../plotTuning/TuningPanel";
import type { KnobValues } from "../plotTuning/knobs";
import CellEditor, { type HunkControls } from "./CellEditor";
import { hunkOverlays } from "./cellDiff";
import { useAutoSave } from "./useAutoSave";
import type { CellSelection } from "./selectionEdit";

/** Which edit a cell's review surface is describing. The tuning record reuses
 *  the agent's per-hunk ledger deliberately (design D6), so the two arrive in
 *  the same shape — but an edit the user dialled in themselves must never be
 *  reported as the agent's, which is what this discriminates. */
export type ReviewOrigin = "agent" | "tune";

/** The review ledger from either origin. A tuning record serializes no line
 *  ranges — its hunks are literal rewrites, not a proposed diff — so the ranges
 *  are optional here and the in-editor hunk widgets simply do not appear for a
 *  tune, leaving the header Keep/Undo pair to cover the review. */
export type ReviewOperation =
  Omit<AgentOperation, "previousRange" | "nextRange">
  & Partial<Pick<AgentOperation, "previousRange" | "nextRange">>;

/** Everything the cell needs to run a tuning panel, grouped rather than spread
 *  over six props (the `hunkControls` precedent in CellEditor). App owns the
 *  calls and the snapshot; the cell only owns whether the panel is open. */
export interface CellTuningControls {
  /** Live document revision — the panel re-scans rather than serving a preview
   *  of source that has since changed. */
  revision: number;
  onScan: (cellId: string) => Promise<TuningPlan>;
  onWarm: (cellId: string) => Promise<TuningWarmResult>;
  onPreview: (shadowId: string, values: KnobValues) => Promise<TuningPreview>;
  onApply: (shadowId: string, values: KnobValues) => Promise<TuningRecord>;
  onDiscardShadow: (shadowId: string) => void;
}

const RASTER_MIMES = ["image/png", "image/jpeg", "image/gif", "image/webp"];

/** Whether a cell renders a picture. The Tune entry point is gated on this
 *  (design §3.5: a cell with no image output never offers Tune), and the
 *  renderer below decides the same question, so both read the one list. */
export function hasImageOutput(outputs: CellOutput[]): boolean {
  return outputs.some((output) => {
    const data = output.data as Record<string, unknown> | undefined;
    return Boolean(data) && (RASTER_MIMES.some((mime) => data?.[mime]) || Boolean(data?.["image/svg+xml"]));
  });
}

function text(value: unknown): string {
  if (Array.isArray(value)) return value.join("");
  if (typeof value === "string") return value;
  return value == null ? "" : JSON.stringify(value, null, 2);
}

// Strip ANSI colour escapes so the traceback reads cleanly and the text
// attached to the agent chat isn't full of control codes.
function stripAnsi(value: string): string {
  // eslint-disable-next-line no-control-regex
  return value.replace(new RegExp(String.fromCharCode(27) + "\[[0-9;]*[A-Za-z]", "g"), "");
}

// A cell error output (traceback) with a one-click "Add to chat" — no need to
// select anything; it attaches the whole error message.
function ErrorOutput({ value, disabled, onAdd }: { value: string; disabled: boolean; onAdd?: (text: string) => void }) {
  const clean = stripAnsi(value);
  return <div className="output-error-block">
    {onAdd && !disabled && <button type="button" className="output-add-chat" aria-label="Add error to agent chat" onClick={() => onAdd(clean)}><Icon name="add_comment" /> Add to chat</button>}
    <pre className="output-error">{clean}</pre>
  </div>;
}

// Rendered markdown that stays selectable: pick any text and add it to the
// agent chat or start an inline edit, without switching to raw edit mode.
function MarkdownPreview({ source, cellId, disabled, onAddSelectionToChat, onInlineEdit, onHoverChange }: {
  source: string; cellId: string; disabled: boolean;
  onAddSelectionToChat?: (selection: CellSelection) => void;
  onInlineEdit?: (selection: CellSelection, instruction: string) => void;
  onHoverChange: (hovered: boolean) => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [menu, setMenu] = useState<{ left: number; top: number; text: string } | null>(null);
  const [inline, setInline] = useState<{ text: string; top: number } | null>(null);
  const [instruction, setInstruction] = useState("");
  const actionsEnabled = !disabled && (Boolean(onAddSelectionToChat) || Boolean(onInlineEdit));
  useEffect(() => {
    if (!actionsEnabled) { setMenu(null); return; }
    const check = () => {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed || selection.rangeCount === 0) { setMenu(null); return; }
      const range = selection.getRangeAt(0);
      if (!ref.current || !ref.current.contains(range.commonAncestorContainer)) { setMenu(null); return; }
      const picked = selection.toString();
      if (!picked.trim()) { setMenu(null); return; }
      const rect = range.getBoundingClientRect();
      const wrap = ref.current.getBoundingClientRect();
      setMenu({ left: Math.max(4, rect.left - wrap.left), top: rect.top - wrap.top - 6, text: picked });
    };
    document.addEventListener("selectionchange", check);
    return () => document.removeEventListener("selectionchange", check);
  }, [actionsEnabled]);

  const selectionOf = (value: string): CellSelection => ({ cellId, text: value, startLine: 0, endLine: 0, kind: "markdown" });
  const openInline = () => {
    if (!menu || !ref.current) return;
    const active = window.getSelection();
    const rect = active && active.rangeCount ? active.getRangeAt(0).getBoundingClientRect() : null;
    const wrap = ref.current.getBoundingClientRect();
    setInstruction("");
    setInline({ text: menu.text, top: rect ? Math.max(0, rect.bottom - wrap.top + 4) : 0 });
    setMenu(null);
  };

  return <div ref={ref} className="markdown-preview-wrap" onMouseMove={(event) => { const target = event.target as HTMLElement; onHoverChange(!(target === ref.current || target.classList.contains("markdown-preview"))); }} onMouseLeave={() => onHoverChange(false)}>
    <div className="markdown-preview"><ReactMarkdown>{source}</ReactMarkdown></div>
    {menu && !inline && <div className="selection-toolbar above" role="toolbar" aria-label="Selection actions" style={{ left: menu.left, top: menu.top }} onMouseDown={(event) => event.preventDefault()}>
      {onAddSelectionToChat && <button type="button" onClick={() => { onAddSelectionToChat(selectionOf(menu.text)); setMenu(null); window.getSelection()?.removeAllRanges(); }}><Icon name="add_comment" /> Add to chat</button>}
      {onInlineEdit && <button type="button" onClick={openInline}><Icon name="auto_fix_high" /> Edit inline</button>}
    </div>}
    {inline && <form className="inline-edit-widget" style={{ top: inline.top }} aria-label="Inline edit instruction" onSubmit={(event) => {
      event.preventDefault();
      const value = instruction.trim();
      if (!value) return;
      onInlineEdit?.(selectionOf(inline.text), value);
      setInline(null);
      window.getSelection()?.removeAllRanges();
    }}>
      <div className="inline-edit-row">
        <input autoFocus value={instruction} placeholder="Describe the edit for the selection…" aria-label="Inline edit instruction"
          onChange={(event) => setInstruction(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); setInline(null); } }} />
        <button className="primary" type="submit" disabled={!instruction.trim()} aria-label="Run inline edit"><Icon name="arrow_upward" /></button>
        <button type="button" aria-label="Cancel inline edit" onClick={() => setInline(null)}><Icon name="close" /></button>
      </div>
      <small>Markdown selection · the agent edits the whole cell, focused on your selection</small>
    </form>}
  </div>;
}

export function Outputs({ outputs, disabled = false, onAddErrorToChat, onHoverChange, onTune, tuneLabel = "Tune" }: { outputs: Record<string, unknown>[]; disabled?: boolean; onAddErrorToChat?: (text: string) => void; onHoverChange?: (hovered: boolean) => void; onTune?: () => void; tuneLabel?: string }) {
  if (!outputs.length) return null;
  return <div className="cell-outputs" aria-label="Cell output" onMouseEnter={() => onHoverChange?.(true)} onMouseLeave={() => onHoverChange?.(false)}>
    {/* Entry point for plot tuning, following the labelled output-add-chat
        precedent below rather than the .cell-actions cluster — the comment on
        that cluster records that a control put there was "effectively
        undiscoverable". Unlike add-chat this one is never hover-revealed: it is
        the entry point for a whole feature, and hiding it until hover would
        reintroduce exactly the objection it was placed here to avoid. */}
    {onTune && <button type="button" className="output-tune" title="Tune the values this plot depends on" aria-label={tuneLabel} onClick={onTune}><Icon name="tune" /> Tune</button>}
    {outputs.map((output, index) => {
    const kind = String(output.output_type ?? "output");
    if (kind === "stream") return <pre key={index}>{text(output.text)}</pre>;
    if (kind === "error") return <ErrorOutput key={index} value={`${text(output.ename)}: ${text(output.evalue)}\n${text(output.traceback)}`} disabled={disabled} onAdd={onAddErrorToChat} />;
    const data = output.data as Record<string, unknown> | undefined;
    const rasterMime = RASTER_MIMES.find((mime) => data?.[mime]);
    if (rasterMime) return <img className="image-output" alt={`Cell output ${index + 1}`} src={`data:${rasterMime};base64,${text(data?.[rasterMime])}`} key={index} />;
    if (data?.["image/svg+xml"]) return <img className="image-output" alt={`SVG cell output ${index + 1}`} src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(text(data["image/svg+xml"]))}`} key={index} />;
    if (data?.["text/html"]) return <iframe className="html-output" title={`HTML output ${index + 1}`} sandbox="" srcDoc={text(data["text/html"])} key={index} />;
    return <pre key={index}>{text(data?.["text/plain"] ?? output)}</pre>;
  })}
  </div>;
}

export default function NotebookCell({ cell, focused, selected, dragIds, editable, context, trusted = false, change, operations = [], origin = "agent", retyped, revertable = true, tunable = false, tuning, disabled, sourceActionsDisabled, autoSave, cellRef, onFocus, onSelect, onContextMenu, onDirtyChange, onSave, onRun, onAddEditable, onAddContext, onRevert, onKeep, onKeepOperation, onUndoOperation, onAddSelectionToChat, onInlineEdit, onAddErrorToChat }: {
  cell: NotebookCellData; focused: boolean; selected: boolean; dragIds: string[]; editable: boolean; context: boolean; trusted?: boolean; change?: AgentChange; revertable?: boolean;
  operations?: ReviewOperation[];
  origin?: ReviewOrigin;
  /** The scan already found knobs for this cell; Tune is worth offering. */
  tunable?: boolean;
  tuning?: CellTuningControls;
  retyped?: { from: string; to: string };
  disabled: boolean; sourceActionsDisabled: boolean; autoSave: boolean; cellRef: (node: HTMLElement | null) => void;
  onFocus: () => void; onSelect: (event: MouseEvent) => void; onContextMenu: (event: MouseEvent) => void; onDirtyChange: (dirty: boolean) => void; onSave: (source: string) => void; onRun: () => void; onAddEditable: () => void; onAddContext: () => void; onRevert: () => void; onKeep?: () => void;
  onKeepOperation?: (operationId: string) => void; onUndoOperation?: (operationId: string) => void;
  onAddSelectionToChat?: (selection: CellSelection) => void; onInlineEdit?: (selection: CellSelection, instruction: string) => void; onAddErrorToChat?: (text: string) => void;
}) {
  const [source, setSource] = useState(cell.source);
  const previousServerSource = useRef(cell.source);
  const [editingMarkdown, setEditingMarkdown] = useState(false);
  // Panel visibility is the cell's own business and nothing above it needs to
  // know. The cell key is `sessionId:cellId`, so this survives every SSE
  // refresh and only resets when the notebook itself changes.
  const [tuningOpen, setTuningOpen] = useState(false);
  const outputRegionRef = useRef<HTMLDivElement | null>(null);
  const tuningWasOpen = useRef(false);
  useEffect(() => {
    // Opening swaps the Tune button out for the panel, and closing swaps it
    // back. Either gesture destroys the element that had focus, which would
    // drop a keyboard user on <body> in the middle of the notebook — so focus
    // follows the swap: into the panel, and back onto the button that opened it.
    if (tuningOpen === tuningWasOpen.current) return;
    tuningWasOpen.current = tuningOpen;
    const region = outputRegionRef.current;
    if (tuningOpen) region?.focus();
    else region?.querySelector<HTMLElement>(".output-tune")?.focus();
  }, [tuningOpen]);
  // Disable cell drag while the pointer is over selectable regions (outputs,
  // rendered markdown) so their text can be selected instead of starting a drag.
  const [suppressDrag, setSuppressDrag] = useState(false);
  const dirty = source !== cell.source;
  useEffect(() => {
    setSource((current) => current === previousServerSource.current ? cell.source : current);
    previousServerSource.current = cell.source;
  }, [cell.source]);
  useEffect(() => onDirtyChange(dirty), [cell.cellId, dirty]);
  // Auto-save (opt-in) a short idle after the last edit; paused while off, or
  // while the agent/kernel is busy (disabled) so saves never race a turn.
  useAutoSave(source, dirty, disabled || !autoSave, onSave);
  const description = `${cell.cellType} cell ${cell.index + 1}`;
  const dependentDisabled = disabled || sourceActionsDisabled;
  const stale = operations.some((item) => item.state === "stale");
  // T1: an added cell carries a structural_add operation and no `change`
  // (it has no previous source to diff), so the review bar keys off either.
  const added = operations.some((item) => item.kind === "structural_add");
  // A retype is worth announcing on its own. It is as likely to be a slip while
  // the agent rewrote structure.json for some other reason as it is deliberate,
  // and moving a cell off `code` silently discards its outputs and execution
  // count. A pure retype changes no source, so without this the cell would
  // render no review surface at all and the loss would only be noticed later.
  const retypedTo = retyped?.to;
  const outputsCleared = retyped?.from === "code";
  const reviewable = Boolean(change) || Boolean(retyped)
    || operations.some((item) => item.state === "pending" || item.state === "stale");
  // Per-hunk Keep/Undo inside the editor. Only when the ledger is live for this
  // cell: a stale cell keeps the read-only legacy diff (the review bar explains
  // why), and pre-ledger/trusted-no-ops cells have no hunks to control.
  //
  // Memoized on a signature rather than the arrays themselves: NotebookView
  // rebuilds each cell's operations array every render, and CellEditor turns
  // these controls into a CodeMirror StateField, so an unstable identity here
  // reconfigures the editor of every cell under review on every keystroke.
  // Handlers go through refs for the same reason — their identity changes each
  // render and would otherwise defeat the memo.
  // Ranges are normalised to null here because a tuning record ships hunks
  // without them; hunkOverlays skips a hunk it cannot place, which is what
  // leaves a tuned cell with the header Keep/Undo pair instead.
  const hunkOps = operations.filter((item) => item.kind === "source_hunk")
    .map((item) => ({ ...item, previousRange: item.previousRange ?? null, nextRange: item.nextRange ?? null }));
  const hunkSignature = hunkOps
    .map((item) => `${item.operationId}:${item.state}:${item.previousRange?.join("-")}:${item.nextRange?.join("-")}`)
    .join("|");
  const keepRef = useRef(onKeepOperation);
  const undoRef = useRef(onUndoOperation);
  keepRef.current = onKeepOperation;
  undoRef.current = onUndoOperation;
  const hunkReviewEnabled = Boolean(change) && !stale && Boolean(onKeepOperation) && Boolean(onUndoOperation);
  const previousSource = change?.previousSource;
  const hunkControls = useMemo<HunkControls | undefined>(() => {
    if (!hunkReviewEnabled || previousSource === undefined) return undefined;
    const overlays = hunkOverlays(previousSource, hunkOps);
    if (!overlays.length) return undefined;
    return {
      overlays,
      disabled: dependentDisabled,
      onKeep: (operationId: string) => keepRef.current?.(operationId),
      onUndo: (operationId: string) => undoRef.current?.(operationId),
    };
    // hunkSignature stands in for hunkOps; see the note above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hunkReviewEnabled, previousSource, hunkSignature, dependentDisabled]);
  // Outputs are suspect only until the cell is executed again. Deriving this
  // from the ledger alone would pin the warning forever, since the operation
  // stays rejected no matter how many times the user re-runs. Remember the
  // execution count seen when the undo first appeared and clear once it moves.
  // Whether the per-hunk Keep/Undo widgets are actually on screen for this
  // cell: they live inside the editor, so a previewed Markdown cell has none
  // even when the ledger has hunks.
  const editorVisible = cell.cellType === "code" || cell.cellType === "raw" || editingMarkdown;
  const hunksVisible = Boolean(hunkControls) && editorVisible;
  const undone = operations.some((item) => item.state === "rejected");
  const undoneAt = useRef<number | null | undefined>(undefined);
  if (!undone) undoneAt.current = undefined;
  else if (undoneAt.current === undefined) undoneAt.current = cell.executionCount;
  const outputsStale = undone && undoneAt.current === cell.executionCount;
  // Every string on the review surface is keyed off the origin. The user reused
  // the agent's ledger on purpose, but their own edit must never be attributed
  // to the agent — so "Agent changed this cell" becomes "You tuned this cell",
  // and so does everything around it.
  const tuned = origin === "tune";
  const reviewLabel = stale
    ? tuned ? "Your tuned change can no longer be undone" : "Agent change can no longer be undone"
    // A tune only rewrites literals in cells that already exist, so `added` is
    // unreachable for it today. The branch is here so that a structural tune,
    // if one ever lands, cannot silently fall through to the agent's wording.
    : added ? tuned ? "You added this cell" : "Agent added this cell"
    : tuned ? "You tuned this cell" : "Agent changed this cell";
  const staleNote = tuned
    ? "This cell changed after you tuned it — these values can no longer be undone individually; edit them by hand."
    : "This cell changed after the agent edited it — undo the whole turn or edit it by hand.";
  const keepLabel = tuned ? `Keep tuned change to ${description}` : `Keep agent change to ${description}`;
  const undoLabel = tuned ? `Undo tuned change to ${description}` : `Revert agent change to ${description}`;
  const keepTitle = tuned ? "Keep these tuned values" : added ? "Keep this cell" : "Keep this agent change";
  const undoTitle = tuned ? "Undo these tuned values" : added ? "Remove this added cell" : "Undo this agent change";
  // §3.5's gate: a picture to compare against, and knobs the scan actually
  // found. Without the second half the button would promise a panel that opens
  // on "nothing here can be tuned".
  const canTune = Boolean(tuning) && tunable && cell.cellType === "code" && hasImageOutput(cell.outputs) && !dependentDisabled;
  return <article ref={cellRef} draggable={!dependentDisabled && !suppressDrag} onDragStart={(event) => {
    const target = event.target as HTMLElement;
    // Never start a cell drag from selectable output text, or from the tuning
    // panel: its sliders and number fields are dragged, and without this a
    // knob drag becomes a cell drag.
    if (target.closest?.(".cell-outputs, .tuning-panel")) { event.preventDefault(); return; }
    // In a markdown block, allow dragging from blank space (the container/padding)
    // but let presses on the rendered text select instead of dragging the cell.
    const mdWrap = target.closest?.(".markdown-preview-wrap");
    if (mdWrap && !(target === mdWrap || target.classList.contains("markdown-preview"))) { event.preventDefault(); return; }
    const ids = dragIds.length ? dragIds : [cell.cellId];
    event.dataTransfer.setData("application/x-notebook-cell", cell.cellId);
    event.dataTransfer.setData("application/x-notebook-cells", JSON.stringify(ids));
    event.dataTransfer.effectAllowed = "copy";
  }} className={`notebook-cell ${focused ? "is-focused" : ""} ${selected ? "is-selected" : ""}`} tabIndex={0} onFocus={onFocus} onContextMenu={onContextMenu} aria-label={description}>
    <div className="cell-gutter" aria-label={`Select ${description}`} title="Click to select · Shift-click for a range · right-click for scope actions" onClick={onSelect}><span className="cell-number" title={`Cell ${cell.index + 1}`}>{cell.index + 1}</span><span className="execution-count">{cell.cellType === "code" ? `[${cell.executionCount ?? " "}]` : cell.cellType === "raw" ? "RAW" : "MD"}</span>{cell.metadata?.agent_authored ? <span className="agent-authored-badge" title="Added by the agent — review before running">AI</span> : null}</div>
    <div className="cell-main">
      <div className="cell-actions">
        {!trusted && <button disabled={dependentDisabled || cell.cellType === "raw"} className={editable ? "selected" : ""} title="Allow agent edit" aria-label={`Allow agent edit ${description}`} onClick={(event) => { event.stopPropagation(); onAddEditable(); }}>{editable ? <Icon name="check" /> : <Icon name="smart_toy" />}</button>}
        <button disabled={dependentDisabled} className={context ? "selected context" : ""} title="Add as focus" aria-label={`Add ${description} as focus`} onClick={(event) => { event.stopPropagation(); onAddContext(); }}>{context ? <Icon name="check" /> : <Icon name="menu_book" />}</button>
        {cell.cellType === "code" && <button disabled={dependentDisabled} title="Run cell" aria-label={`Run ${description}`} onClick={onRun}><Icon name="play_arrow" /></button>}
        {cell.cellType === "markdown" && <button disabled={disabled} title={editingMarkdown ? "Preview Markdown" : "Edit Markdown"} aria-label={`${editingMarkdown ? "Preview" : "Edit"} ${description}`} onClick={() => setEditingMarkdown(!editingMarkdown)}><Icon name="edit" /></button>}
        {dirty && <button disabled={disabled} title="Save source" aria-label={`Save ${description}`} onClick={() => onSave(source)}><Icon name="save" /></button>}
      </div>
      {/* Agent changes are reviewed from a persistent, labelled bar rather than
          the hover-revealed action cluster above: the cluster is invisible until
          hover, unlabelled, and shared with scope/run actions, so the revert
          control was there but effectively undiscoverable. */}
      {reviewable && <div className={`cell-review${tuned ? " tuned" : ""}`}>
        <span className="cell-review-label">{tuned ? <Icon name="tune" /> : <Icon name="smart_toy" />} {reviewLabel}</span>
        {!revertable
          // A Trusted turn rewrote the whole notebook, so this cell's change
          // cannot be separated from the adds, deletes and moves around it.
          ? <span className="cell-review-stale" role="note">Part of a whole-notebook edit — use “Undo entire turn” to reverse it.</span>
          : stale
          // Say why the controls went away. Silently removing them reads as a
          // bug; the change is still in the cell, it just can no longer be
          // separated from the edits made on top of it.
          ? <span className="cell-review-stale" role="note">{staleNote}</span>
          // Header controls only when the in-editor hunk widgets cannot cover
          // the review. With them visible, a header pair would act on exactly
          // the same change and just duplicate the buttons a few lines below.
          // Still needed for: a whole cell the agent added (no hunks to attach
          // to), a Markdown cell being previewed (no editor rendered), and
          // pre-ledger turns whose changes have no operations.
          : hunksVisible
          ? null
          : <>
            {onKeep && <button className="review-action keep" disabled={dependentDisabled} title={keepTitle} aria-label={keepLabel} onClick={onKeep}><Icon name="check" /> Keep</button>}
            <button className="review-action undo" disabled={dependentDisabled} title={undoTitle} aria-label={undoLabel} onClick={onRevert}><Icon name="undo" /> Undo</button>
          </>}
      </div>}
      {retypedTo && <p className="cell-retyped" role="note">
        Agent changed this cell to {retypedTo}{outputsCleared ? " — its outputs and execution count were cleared" : ""}. Undo the turn to restore it.
      </p>}
      {outputsStale && cell.outputs.length > 0 && <p className="cell-stale-outputs" role="note">Outputs are from code you undid — re-run this cell.</p>}
      {cell.cellType === "code" || cell.cellType === "raw" || editingMarkdown ? <CellEditor value={source} label={`Source for ${description}`} disabled={disabled} language={cell.cellType} change={change} hunkControls={hunkControls} cellId={cell.cellId} interactionsDisabled={dependentDisabled} onChange={setSource} onSave={() => dirty && onSave(source)} onRun={onRun} onAddSelectionToChat={onAddSelectionToChat} onInlineEdit={onInlineEdit} /> : <MarkdownPreview source={source} cellId={cell.cellId} disabled={dependentDisabled} onAddSelectionToChat={onAddSelectionToChat} onInlineEdit={onInlineEdit} onHoverChange={setSuppressDrag} />}
      {/* The output region. It stops being a single column once the panel is
          open: the panel lays out preview-left / knob-rail-right, and this
          wrapper is the CSS container that decides when that stacks — the pane
          can be far narrower than the viewport, so a viewport media query
          would miss it. */}
      <div className="cell-output-region" ref={outputRegionRef} tabIndex={tuningOpen ? -1 : undefined}>
        {/* The panel replaces the cell's own outputs rather than sitting under
            them: it renders that same committed picture as its "before", and a
            second copy of a tall plot would push the knob rail out of sight. */}
        {tuningOpen && tuning
          ? <TuningPanel cellId={cell.cellId} open revision={tuning.revision}
            onScan={tuning.onScan} onWarm={tuning.onWarm} onPreview={tuning.onPreview} onApply={tuning.onApply}
            onDiscardShadow={tuning.onDiscardShadow} onClose={() => setTuningOpen(false)} />
          : <Outputs outputs={cell.outputs} disabled={dependentDisabled} onAddErrorToChat={onAddErrorToChat} onHoverChange={setSuppressDrag}
            onTune={canTune ? () => setTuningOpen(true) : undefined} tuneLabel={`Tune ${description}`} />}
      </div>
    </div>
  </article>;
}
