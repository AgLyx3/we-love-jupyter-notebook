import { useEffect, useState } from "react";
import type { AgentTurn, NotebookSnapshot, TurnScope } from "../api/client";
import NotebookCell from "./NotebookCell";

export default function NotebookView({ notebook, scope, turn, onSave, onRun, onScope, onRevert }: {
  notebook: NotebookSnapshot; scope: TurnScope; turn: AgentTurn | null;
  onSave: (cellId: string, source: string) => void; onRun: (cellId: string) => void; onScope: (cellId: string, editable: boolean) => void; onRevert: (turnId: string, cellId: string) => void;
}) {
  const [focused, setFocused] = useState(notebook.cells[0]?.cellId ?? "");
  useEffect(() => { if (!notebook.cells.some((cell) => cell.cellId === focused)) setFocused(notebook.cells[0]?.cellId ?? ""); }, [notebook, focused]);
  return <main className="notebook-surface" aria-label="Notebook cells" onKeyDown={(event) => {
    if (!event.altKey || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) return;
    event.preventDefault();
    const index = notebook.cells.findIndex((cell) => cell.cellId === focused);
    const next = Math.max(0, Math.min(notebook.cells.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)));
    setFocused(notebook.cells[next]?.cellId ?? focused);
  }}>
    {notebook.cells.map((cell) => <NotebookCell key={cell.cellId} cell={cell} focused={focused === cell.cellId}
      editable={scope.editableCellIds.includes(cell.cellId)} context={scope.contextCellIds.includes(cell.cellId)}
      change={turn?.changes.find((change) => change.cellId === cell.cellId)} onFocus={() => setFocused(cell.cellId)}
      onSave={(source) => onSave(cell.cellId, source)} onRun={() => onRun(cell.cellId)}
      onAddEditable={() => onScope(cell.cellId, true)} onAddContext={() => onScope(cell.cellId, false)}
      onRevert={() => turn && onRevert(turn.turnId, cell.cellId)} />)}
  </main>;
}
