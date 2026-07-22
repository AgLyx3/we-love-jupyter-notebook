import { Eraser, LockKeyhole } from "lucide-react";
import type { NotebookSnapshot, TurnScope } from "../api/client";

export default function TurnScopePanel({ notebook, scope, onClear }: { notebook: NotebookSnapshot; scope: TurnScope; onClear: () => void }) {
  const names = (ids: string[]) => ids.map((id) => `Cell ${(notebook.cells.find((cell) => cell.cellId === id)?.index ?? -1) + 1}`).join(", ");
  return <section className="scope-panel" aria-labelledby="scope-heading">
    <div className="section-heading"><div><LockKeyhole /><h2 id="scope-heading">Turn scope</h2></div><button title="Clear turn scope" aria-label="Clear turn scope" disabled={!scope.editableCellIds.length && !scope.contextCellIds.length} onClick={onClear}><Eraser /></button></div>
    <div className="scope-counts"><span>{scope.editableCellIds.length} editable</span><span>{scope.contextCellIds.length} context</span></div>
    {(scope.editableCellIds.length > 0 || scope.contextCellIds.length > 0) && <p className="scope-detail">{names(scope.editableCellIds)}{scope.contextCellIds.length > 0 ? ` · Context: ${names(scope.contextCellIds)}` : ""}</p>}
  </section>;
}
