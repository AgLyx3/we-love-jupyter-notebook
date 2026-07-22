import { BookOpen, Check, Pencil, Play, RotateCcw, Save } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { useEffect, useState } from "react";
import type { AgentChange, NotebookCellData } from "../api/client";
import CellEditor from "./CellEditor";

function text(value: unknown): string {
  if (Array.isArray(value)) return value.join("");
  if (typeof value === "string") return value;
  return value == null ? "" : JSON.stringify(value, null, 2);
}

function Outputs({ outputs }: { outputs: Record<string, unknown>[] }) {
  if (!outputs.length) return null;
  return <div className="cell-outputs" aria-label="Cell output">{outputs.map((output, index) => {
    const kind = String(output.output_type ?? "output");
    if (kind === "stream") return <pre key={index}>{text(output.text)}</pre>;
    if (kind === "error") return <pre className="output-error" key={index}>{text(output.ename)}: {text(output.evalue)}{"\n"}{text(output.traceback)}</pre>;
    const data = output.data as Record<string, unknown> | undefined;
    if (data?.["text/html"]) return <iframe className="html-output" title={`HTML output ${index + 1}`} sandbox="" srcDoc={text(data["text/html"])} key={index} />;
    return <pre key={index}>{text(data?.["text/plain"] ?? output)}</pre>;
  })}</div>;
}

export default function NotebookCell({ cell, focused, editable, context, change, onFocus, onSave, onRun, onAddEditable, onAddContext, onRevert }: {
  cell: NotebookCellData; focused: boolean; editable: boolean; context: boolean; change?: AgentChange;
  onFocus: () => void; onSave: (source: string) => void; onRun: () => void; onAddEditable: () => void; onAddContext: () => void; onRevert: () => void;
}) {
  const [source, setSource] = useState(cell.source);
  const [editingMarkdown, setEditingMarkdown] = useState(false);
  useEffect(() => setSource(cell.source), [cell.source]);
  const dirty = source !== cell.source;
  const description = `${cell.cellType} cell ${cell.index + 1}`;
  return <article className={`notebook-cell ${focused ? "is-focused" : ""}`} tabIndex={0} onFocus={onFocus} aria-label={description}>
    <div className="cell-gutter"><span className="execution-count">{cell.cellType === "code" ? `[${cell.executionCount ?? " "}]` : "MD"}</span><div className="gutter-actions">
      <button className={editable ? "selected" : ""} title="Allow agent edit" aria-label={`Allow agent edit ${description}`} onClick={onAddEditable}>{editable ? <Check /> : <Pencil />}</button>
      <button className={context ? "selected context" : ""} title="Add as context" aria-label={`Add ${description} as context`} onClick={onAddContext}>{context ? <Check /> : <BookOpen />}</button>
    </div></div>
    <div className="cell-main">
      <div className="cell-actions">
        {cell.cellType === "code" && <button title="Run cell" aria-label={`Run ${description}`} onClick={onRun}><Play /></button>}
        {cell.cellType === "markdown" && <button title={editingMarkdown ? "Preview Markdown" : "Edit Markdown"} aria-label={`${editingMarkdown ? "Preview" : "Edit"} ${description}`} onClick={() => setEditingMarkdown(!editingMarkdown)}><Pencil /></button>}
        {dirty && <button title="Save source" aria-label={`Save ${description}`} onClick={() => onSave(source)}><Save /></button>}
        {change && <button title="Revert this agent change" aria-label={`Revert agent change to ${description}`} onClick={onRevert}><RotateCcw /></button>}
      </div>
      {cell.cellType === "code" || editingMarkdown ? <CellEditor value={source} label={`Source for ${description}`} onChange={setSource} onSave={() => dirty && onSave(source)} /> : <div className="markdown-preview"><ReactMarkdown>{source}</ReactMarkdown></div>}
      <Outputs outputs={cell.outputs} />
      {change && <details className="cell-diff"><summary>Agent change</summary><div className="diff-columns"><pre className="diff-before">{change.previousSource}</pre><pre className="diff-after">{change.nextSource}</pre></div></details>}
    </div>
  </article>;
}
