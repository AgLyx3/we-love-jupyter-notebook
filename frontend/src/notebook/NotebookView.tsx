import { useEffect, useRef, useState } from "react";
import type { AgentTurn, NotebookSnapshot, TurnScope } from "../api/client";
import NotebookCell from "./NotebookCell";

export default function NotebookView({ notebook, scope, turn, disabled, focusRequest, onSave, onRun, onScope, onRevert }: {
  notebook: NotebookSnapshot; scope: TurnScope; turn: AgentTurn | null;
  disabled: boolean; focusRequest: { cellId: string; requestId: number } | null;
  onSave: (cellId: string, source: string) => void; onRun: (cellId: string) => void; onScope: (cellId: string, editable: boolean) => void; onRevert: (turnId: string, cellId: string) => void;
}) {
  const [focused, setFocused] = useState(notebook.cells[0]?.cellId ?? "");
  const refs = useRef(new Map<string, HTMLElement>());
  useEffect(() => { if (!notebook.cells.some((cell) => cell.cellId === focused)) setFocused(notebook.cells[0]?.cellId ?? ""); }, [notebook, focused]);
  useEffect(() => { if (!focusRequest) return; const node = refs.current.get(focusRequest.cellId); node?.focus(); node?.scrollIntoView({ block: "center", behavior: "smooth" }); }, [focusRequest]);
  return <main className="notebook-surface" aria-label="Notebook cells" onKeyDown={(event) => {
    if (!event.altKey || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) return;
    event.preventDefault();
    const index = notebook.cells.findIndex((cell) => cell.cellId === focused);
    const next = Math.max(0, Math.min(notebook.cells.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)));
    const id = notebook.cells[next]?.cellId ?? focused; setFocused(id); const node = refs.current.get(id); node?.focus(); node?.scrollIntoView({ block: "nearest" });
  }}>
    {notebook.cells.map((cell) => <NotebookCell key={cell.cellId} cell={cell} focused={focused === cell.cellId}
      editable={scope.editableCellIds.includes(cell.cellId)} context={scope.contextCellIds.includes(cell.cellId)} disabled={disabled}
      cellRef={(node) => { if (node) refs.current.set(cell.cellId, node); else refs.current.delete(cell.cellId); }}
      change={turn?.changes.find((change) => change.cellId === cell.cellId)} onFocus={() => setFocused(cell.cellId)}
      onSave={(source) => onSave(cell.cellId, source)} onRun={() => onRun(cell.cellId)}
      onAddEditable={() => onScope(cell.cellId, true)} onAddContext={() => onScope(cell.cellId, false)}
      onRevert={() => turn && onRevert(turn.turnId, cell.cellId)} />)}
  </main>;
}
