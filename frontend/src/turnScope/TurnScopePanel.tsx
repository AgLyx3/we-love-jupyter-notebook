import { Eraser, LockKeyhole, X } from "lucide-react";
import type { NotebookSnapshot, TurnScope } from "../api/client";

export function ScopeCellList({ notebook, editableCellIds, contextCellIds, disabled, onFocusCell, onRemoveCell }: { notebook: NotebookSnapshot; editableCellIds: string[]; contextCellIds: string[]; disabled?: boolean; onFocusCell: (id: string) => void; onRemoveCell?: (id: string) => void }) {
  const items = (ids: string[], kind: "editable" | "context") => ids.map((id) => {
    const cell = notebook.cells.find((item) => item.cellId === id);
    if (!cell) return null;
    return <div className="scope-item-row" key={`${kind}-${id}`}>
      <button type="button" className={`scope-item ${kind}`} title={`Cell ID: ${id}`} onClick={() => onFocusCell(id)}><b>{cell.index + 1}</b><span><strong>{cell.cellType}</strong>{cell.source.trim().slice(0, 46) || "Empty cell"}</span></button>
      {onRemoveCell && <button type="button" className="scope-remove" disabled={disabled} title="Remove from turn scope" aria-label={`Remove cell ${cell.index + 1} from turn scope`} onClick={() => onRemoveCell(id)}><X /></button>}
    </div>;
  });
  return <div className="scope-items">{items(editableCellIds, "editable")}{items(contextCellIds, "context")}</div>;
}

export default function TurnScopePanel({ notebook, scope, disabled, onClear, onFocusCell, onDropCell, onRemoveCell }: { notebook: NotebookSnapshot; scope: TurnScope; disabled: boolean; onClear: () => void; onFocusCell: (id: string) => void; onDropCell: (id: string) => void; onRemoveCell?: (id: string) => void }) {
  return <section className="scope-panel" aria-labelledby="scope-heading" onDragOver={(event) => { if (!disabled) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; } }} onDrop={(event) => { event.preventDefault(); event.stopPropagation(); const id = event.dataTransfer.getData("application/x-notebook-cell"); if (id && !disabled) onDropCell(id); }}>
    <div className="section-heading"><div><LockKeyhole /><h2 id="scope-heading">Turn scope</h2></div><button title="Clear turn scope" aria-label="Clear turn scope" disabled={disabled || (!scope.editableCellIds.length && !scope.contextCellIds.length)} onClick={onClear}><Eraser /></button></div>
    <div className="scope-counts"><span>{scope.editableCellIds.length} editable</span><span>{scope.contextCellIds.length} context</span></div>
    <ScopeCellList notebook={notebook} editableCellIds={scope.editableCellIds} contextCellIds={scope.contextCellIds} disabled={disabled} onFocusCell={onFocusCell} onRemoveCell={onRemoveCell} />
  </section>;
}
