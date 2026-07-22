import { BookOpen, Check, Pencil, Play, RotateCcw, Save } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { useEffect, useRef, useState, type MouseEvent } from "react";
import type { AgentChange, NotebookCellData } from "../api/client";
import CellEditor from "./CellEditor";

function text(value: unknown): string {
  if (Array.isArray(value)) return value.join("");
  if (typeof value === "string") return value;
  return value == null ? "" : JSON.stringify(value, null, 2);
}

export function Outputs({ outputs }: { outputs: Record<string, unknown>[] }) {
  if (!outputs.length) return null;
  return <div className="cell-outputs" aria-label="Cell output">{outputs.map((output, index) => {
    const kind = String(output.output_type ?? "output");
    if (kind === "stream") return <pre key={index}>{text(output.text)}</pre>;
    if (kind === "error") return <pre className="output-error" key={index}>{text(output.ename)}: {text(output.evalue)}{"\n"}{text(output.traceback)}</pre>;
    const data = output.data as Record<string, unknown> | undefined;
    const rasterMime = ["image/png", "image/jpeg", "image/gif", "image/webp"].find((mime) => data?.[mime]);
    if (rasterMime) return <img className="image-output" alt={`Cell output ${index + 1}`} src={`data:${rasterMime};base64,${text(data?.[rasterMime])}`} key={index} />;
    if (data?.["image/svg+xml"]) return <img className="image-output" alt={`SVG cell output ${index + 1}`} src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(text(data["image/svg+xml"]))}`} key={index} />;
    if (data?.["text/html"]) return <iframe className="html-output" title={`HTML output ${index + 1}`} sandbox="" srcDoc={text(data["text/html"])} key={index} />;
    return <pre key={index}>{text(data?.["text/plain"] ?? output)}</pre>;
  })}</div>;
}

export default function NotebookCell({ cell, focused, selected, editable, context, change, disabled, sourceActionsDisabled, cellRef, onFocus, onSelect, onContextMenu, onDirtyChange, onSave, onRun, onAddEditable, onAddContext, onRevert }: {
  cell: NotebookCellData; focused: boolean; selected: boolean; editable: boolean; context: boolean; change?: AgentChange;
  disabled: boolean; sourceActionsDisabled: boolean; cellRef: (node: HTMLElement | null) => void;
  onFocus: () => void; onSelect: (event: MouseEvent) => void; onContextMenu: (event: MouseEvent) => void; onDirtyChange: (dirty: boolean) => void; onSave: (source: string) => void; onRun: () => void; onAddEditable: () => void; onAddContext: () => void; onRevert: () => void;
}) {
  const [source, setSource] = useState(cell.source);
  const previousServerSource = useRef(cell.source);
  const [editingMarkdown, setEditingMarkdown] = useState(false);
  const dirty = source !== cell.source;
  useEffect(() => {
    setSource((current) => current === previousServerSource.current ? cell.source : current);
    previousServerSource.current = cell.source;
  }, [cell.source]);
  useEffect(() => onDirtyChange(dirty), [cell.cellId, dirty]);
  const description = `${cell.cellType} cell ${cell.index + 1}`;
  const dependentDisabled = disabled || sourceActionsDisabled;
  return <article ref={cellRef} draggable={!dependentDisabled} onDragStart={(event) => { event.dataTransfer.setData("application/x-notebook-cell", cell.cellId); event.dataTransfer.effectAllowed = "copy"; }} className={`notebook-cell ${focused ? "is-focused" : ""} ${selected ? "is-selected" : ""}`} tabIndex={0} onFocus={onFocus} onContextMenu={onContextMenu} aria-label={description}>
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
      {cell.cellType === "code" || cell.cellType === "raw" || editingMarkdown ? <CellEditor value={source} label={`Source for ${description}`} disabled={disabled} language={cell.cellType} change={change} onChange={setSource} onSave={() => dirty && onSave(source)} /> : <div className="markdown-preview"><ReactMarkdown>{source}</ReactMarkdown></div>}
      <Outputs outputs={cell.outputs} />
    </div>
  </article>;
}
