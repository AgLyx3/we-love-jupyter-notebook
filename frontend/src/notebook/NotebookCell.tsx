import { BookOpen, Check, MessageSquarePlus, Pencil, Play, RotateCcw, Save, Send, Wand2, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { useEffect, useRef, useState, type MouseEvent } from "react";
import type { AgentChange, NotebookCellData } from "../api/client";
import CellEditor from "./CellEditor";
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

  return <div ref={ref} className="markdown-preview-wrap" onMouseEnter={() => onHoverChange(true)} onMouseLeave={() => onHoverChange(false)}>
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

export default function NotebookCell({ cell, focused, selected, editable, context, change, disabled, sourceActionsDisabled, cellRef, onFocus, onSelect, onContextMenu, onDirtyChange, onSave, onRun, onAddEditable, onAddContext, onRevert, onAddSelectionToChat, onInlineEdit, onAddErrorToChat }: {
  cell: NotebookCellData; focused: boolean; selected: boolean; editable: boolean; context: boolean; change?: AgentChange;
  disabled: boolean; sourceActionsDisabled: boolean; cellRef: (node: HTMLElement | null) => void;
  onFocus: () => void; onSelect: (event: MouseEvent) => void; onContextMenu: (event: MouseEvent) => void; onDirtyChange: (dirty: boolean) => void; onSave: (source: string) => void; onRun: () => void; onAddEditable: () => void; onAddContext: () => void; onRevert: () => void;
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
  const description = `${cell.cellType} cell ${cell.index + 1}`;
  const dependentDisabled = disabled || sourceActionsDisabled;
  return <article ref={cellRef} draggable={!dependentDisabled && !suppressDrag} onDragStart={(event) => {
    // Belt-and-suspenders: never start a cell drag from within selectable text.
    if ((event.target as HTMLElement).closest?.(".cell-outputs, .markdown-preview-wrap")) { event.preventDefault(); return; }
    event.dataTransfer.setData("application/x-notebook-cell", cell.cellId); event.dataTransfer.effectAllowed = "copy";
  }} className={`notebook-cell ${focused ? "is-focused" : ""} ${selected ? "is-selected" : ""}`} tabIndex={0} onFocus={onFocus} onContextMenu={onContextMenu} aria-label={description}>
    <div className="cell-gutter" aria-label={`Select ${description}`} title="Click to select · Shift-click for a range · right-click for scope actions" onClick={onSelect}><span className="execution-count">{cell.cellType === "code" ? `[${cell.executionCount ?? " "}]` : cell.cellType === "raw" ? "RAW" : "MD"}</span><div className="gutter-actions">
      <button disabled={dependentDisabled || cell.cellType === "raw"} className={editable ? "selected" : ""} title="Allow agent edit" aria-label={`Allow agent edit ${description}`} onClick={(event) => { event.stopPropagation(); onAddEditable(); }}>{editable ? <Check /> : <Pencil />}</button>
      <button disabled={dependentDisabled} className={context ? "selected context" : ""} title="Add as context" aria-label={`Add ${description} as context`} onClick={(event) => { event.stopPropagation(); onAddContext(); }}>{context ? <Check /> : <BookOpen />}</button>
    </div></div>
    <div className="cell-main">
      <div className="cell-actions">
        {cell.cellType === "code" && <button disabled={dependentDisabled} title="Run cell" aria-label={`Run ${description}`} onClick={onRun}><Play /></button>}
        {cell.cellType === "markdown" && <button disabled={disabled} title={editingMarkdown ? "Preview Markdown" : "Edit Markdown"} aria-label={`${editingMarkdown ? "Preview" : "Edit"} ${description}`} onClick={() => setEditingMarkdown(!editingMarkdown)}><Pencil /></button>}
        {dirty && <button disabled={disabled} title="Save source" aria-label={`Save ${description}`} onClick={() => onSave(source)}><Save /></button>}
        {change && <button disabled={dependentDisabled} title="Revert this agent change" aria-label={`Revert agent change to ${description}`} onClick={onRevert}><RotateCcw /></button>}
      </div>
      {cell.cellType === "code" || cell.cellType === "raw" || editingMarkdown ? <CellEditor value={source} label={`Source for ${description}`} disabled={disabled} language={cell.cellType} change={change} cellId={cell.cellId} interactionsDisabled={dependentDisabled} onChange={setSource} onSave={() => dirty && onSave(source)} onAddSelectionToChat={onAddSelectionToChat} onInlineEdit={onInlineEdit} /> : <MarkdownPreview source={source} cellId={cell.cellId} disabled={dependentDisabled} onAddSelectionToChat={onAddSelectionToChat} onInlineEdit={onInlineEdit} onHoverChange={setSuppressDrag} />}
      <Outputs outputs={cell.outputs} disabled={dependentDisabled} onAddErrorToChat={onAddErrorToChat} onHoverChange={setSuppressDrag} />
    </div>
  </article>;
}
