import { BookOpen, Bot, Check, MessageSquarePlus, Pencil, Play, RotateCcw, Save, Send, Wand2, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import type { AgentChange, AgentOperation, NotebookCellData } from "../api/client";
import CellEditor, { type HunkControls } from "./CellEditor";
import { hunkOverlays } from "./cellDiff";
import { useAutoSave } from "./useAutoSave";
import type { CellSelection } from "./selectionEdit";

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
    {onAdd && !disabled && <button type="button" className="output-add-chat" aria-label="Add error to agent chat" onClick={() => onAdd(clean)}><MessageSquarePlus /> Add to chat</button>}
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
      {onAddSelectionToChat && <button type="button" onClick={() => { onAddSelectionToChat(selectionOf(menu.text)); setMenu(null); window.getSelection()?.removeAllRanges(); }}><MessageSquarePlus /> Add to chat</button>}
      {onInlineEdit && <button type="button" onClick={openInline}><Wand2 /> Edit inline</button>}
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
        <button className="primary" type="submit" disabled={!instruction.trim()} aria-label="Run inline edit"><Send /></button>
        <button type="button" aria-label="Cancel inline edit" onClick={() => setInline(null)}><X /></button>
      </div>
      <small>Markdown selection · the agent edits the whole cell, focused on your selection</small>
    </form>}
  </div>;
}

export function Outputs({ outputs, disabled = false, onAddErrorToChat, onHoverChange }: { outputs: Record<string, unknown>[]; disabled?: boolean; onAddErrorToChat?: (text: string) => void; onHoverChange?: (hovered: boolean) => void }) {
  if (!outputs.length) return null;
  return <div className="cell-outputs" aria-label="Cell output" onMouseEnter={() => onHoverChange?.(true)} onMouseLeave={() => onHoverChange?.(false)}>{outputs.map((output, index) => {
    const kind = String(output.output_type ?? "output");
    if (kind === "stream") return <pre key={index}>{text(output.text)}</pre>;
    if (kind === "error") return <ErrorOutput key={index} value={`${text(output.ename)}: ${text(output.evalue)}\n${text(output.traceback)}`} disabled={disabled} onAdd={onAddErrorToChat} />;
    const data = output.data as Record<string, unknown> | undefined;
    const rasterMime = ["image/png", "image/jpeg", "image/gif", "image/webp"].find((mime) => data?.[mime]);
    if (rasterMime) return <img className="image-output" alt={`Cell output ${index + 1}`} src={`data:${rasterMime};base64,${text(data?.[rasterMime])}`} key={index} />;
    if (data?.["image/svg+xml"]) return <img className="image-output" alt={`SVG cell output ${index + 1}`} src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(text(data["image/svg+xml"]))}`} key={index} />;
    if (data?.["text/html"]) return <iframe className="html-output" title={`HTML output ${index + 1}`} sandbox="" srcDoc={text(data["text/html"])} key={index} />;
    return <pre key={index}>{text(data?.["text/plain"] ?? output)}</pre>;
  })}</div>;
}

export default function NotebookCell({ cell, focused, selected, dragIds, editable, context, trusted = false, change, operations = [], retyped, revertable = true, disabled, sourceActionsDisabled, autoSave, cellRef, onFocus, onSelect, onContextMenu, onDirtyChange, onSave, onRun, onAddEditable, onAddContext, onRevert, onKeep, onKeepOperation, onUndoOperation, onAddSelectionToChat, onInlineEdit, onAddErrorToChat }: {
  cell: NotebookCellData; focused: boolean; selected: boolean; dragIds: string[]; editable: boolean; context: boolean; trusted?: boolean; change?: AgentChange; revertable?: boolean;
  operations?: AgentOperation[];
  retyped?: { from: string; to: string };
  disabled: boolean; sourceActionsDisabled: boolean; autoSave: boolean; cellRef: (node: HTMLElement | null) => void;
  onFocus: () => void; onSelect: (event: MouseEvent) => void; onContextMenu: (event: MouseEvent) => void; onDirtyChange: (dirty: boolean) => void; onSave: (source: string) => void; onRun: () => void; onAddEditable: () => void; onAddContext: () => void; onRevert: () => void; onKeep?: () => void;
  onKeepOperation?: (operationId: string) => void; onUndoOperation?: (operationId: string) => void;
  onAddSelectionToChat?: (selection: CellSelection) => void; onInlineEdit?: (selection: CellSelection, instruction: string) => void; onAddErrorToChat?: (text: string) => void;
}) {
  const [source, setSource] = useState(cell.source);
  const previousServerSource = useRef(cell.source);
  const [editingMarkdown, setEditingMarkdown] = useState(false);
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
  const hunkOps = operations.filter((item) => item.kind === "source_hunk");
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
  return <article ref={cellRef} draggable={!dependentDisabled && !suppressDrag} onDragStart={(event) => {
    const target = event.target as HTMLElement;
    // Never start a cell drag from selectable output text.
    if (target.closest?.(".cell-outputs")) { event.preventDefault(); return; }
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
        {!trusted && <button disabled={dependentDisabled || cell.cellType === "raw"} className={editable ? "selected" : ""} title="Allow agent edit" aria-label={`Allow agent edit ${description}`} onClick={(event) => { event.stopPropagation(); onAddEditable(); }}>{editable ? <Check /> : <Bot />}</button>}
        <button disabled={dependentDisabled} className={context ? "selected context" : ""} title="Add as focus" aria-label={`Add ${description} as focus`} onClick={(event) => { event.stopPropagation(); onAddContext(); }}>{context ? <Check /> : <BookOpen />}</button>
        {cell.cellType === "code" && <button disabled={dependentDisabled} title="Run cell" aria-label={`Run ${description}`} onClick={onRun}><Play /></button>}
        {cell.cellType === "markdown" && <button disabled={disabled} title={editingMarkdown ? "Preview Markdown" : "Edit Markdown"} aria-label={`${editingMarkdown ? "Preview" : "Edit"} ${description}`} onClick={() => setEditingMarkdown(!editingMarkdown)}><Pencil /></button>}
        {dirty && <button disabled={disabled} title="Save source" aria-label={`Save ${description}`} onClick={() => onSave(source)}><Save /></button>}
      </div>
      {/* Agent changes are reviewed from a persistent, labelled bar rather than
          the hover-revealed action cluster above: the cluster is invisible until
          hover, unlabelled, and shared with scope/run actions, so the revert
          control was there but effectively undiscoverable. */}
      {reviewable && <div className="cell-review">
        <span className="cell-review-label"><Bot /> {stale ? "Agent change can no longer be undone" : added ? "Agent added this cell" : "Agent changed this cell"}</span>
        {!revertable
          // A Trusted turn rewrote the whole notebook, so this cell's change
          // cannot be separated from the adds, deletes and moves around it.
          ? <span className="cell-review-stale" role="note">Part of a whole-notebook edit — use “Undo entire turn” to reverse it.</span>
          : stale
          // Say why the controls went away. Silently removing them reads as a
          // bug; the change is still in the cell, it just can no longer be
          // separated from the edits made on top of it.
          ? <span className="cell-review-stale" role="note">This cell changed after the agent edited it — undo the whole turn or edit it by hand.</span>
          // Header controls only when the in-editor hunk widgets cannot cover
          // the review. With them visible, a header pair would act on exactly
          // the same change and just duplicate the buttons a few lines below.
          // Still needed for: a whole cell the agent added (no hunks to attach
          // to), a Markdown cell being previewed (no editor rendered), and
          // pre-ledger turns whose changes have no operations.
          : hunksVisible
          ? null
          : <>
            {onKeep && <button className="review-action keep" disabled={dependentDisabled} title={added ? "Keep this cell" : "Keep this agent change"} aria-label={`Keep agent change to ${description}`} onClick={onKeep}><Check /> Keep</button>}
            <button className="review-action undo" disabled={dependentDisabled} title={added ? "Remove this added cell" : "Undo this agent change"} aria-label={`Revert agent change to ${description}`} onClick={onRevert}><RotateCcw /> Undo</button>
          </>}
      </div>}
      {retypedTo && <p className="cell-retyped" role="note">
        Agent changed this cell to {retypedTo}{outputsCleared ? " — its outputs and execution count were cleared" : ""}. Undo the turn to restore it.
      </p>}
      {outputsStale && cell.outputs.length > 0 && <p className="cell-stale-outputs" role="note">Outputs are from code you undid — re-run this cell.</p>}
      {cell.cellType === "code" || cell.cellType === "raw" || editingMarkdown ? <CellEditor value={source} label={`Source for ${description}`} disabled={disabled} language={cell.cellType} change={change} hunkControls={hunkControls} cellId={cell.cellId} interactionsDisabled={dependentDisabled} onChange={setSource} onSave={() => dirty && onSave(source)} onRun={onRun} onAddSelectionToChat={onAddSelectionToChat} onInlineEdit={onInlineEdit} /> : <MarkdownPreview source={source} cellId={cell.cellId} disabled={dependentDisabled} onAddSelectionToChat={onAddSelectionToChat} onInlineEdit={onInlineEdit} onHoverChange={setSuppressDrag} />}
      <Outputs outputs={cell.outputs} disabled={dependentDisabled} onAddErrorToChat={onAddErrorToChat} onHoverChange={setSuppressDrag} />
    </div>
  </article>;
}
