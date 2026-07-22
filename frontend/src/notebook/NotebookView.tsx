import { useEffect, useRef, useState, type MouseEvent } from "react";
import type { AgentTurn, NotebookSnapshot, TurnScope } from "../api/client";
import NotebookCell from "./NotebookCell";

export default function NotebookView({ notebook, scope, turn, disabled, sourceActionsDisabled, focusRequest, onDirtyChange, onSave, onRun, onScope, onScopeMany, onRevert }: {
  notebook: NotebookSnapshot; scope: TurnScope; turn: AgentTurn | null;
  disabled: boolean; sourceActionsDisabled: boolean; focusRequest: { cellId: string; requestId: number } | null;
  onDirtyChange: (cellId: string, dirty: boolean) => void;
  onSave: (cellId: string, source: string) => void; onRun: (cellId: string) => void; onScope: (cellId: string, editable: boolean) => void; onScopeMany: (cellIds: string[], editable: boolean) => void; onRevert: (turnId: string, cellId: string) => void;
}) {
  const [focused, setFocused] = useState(notebook.cells[0]?.cellId ?? "");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const refs = useRef(new Map<string, HTMLElement>());
  const anchorRef = useRef(notebook.cells[0]?.cellId ?? "");
  const scopeDisabled = disabled || sourceActionsDisabled;
  useEffect(() => { if (!notebook.cells.some((cell) => cell.cellId === focused)) setFocused(notebook.cells[0]?.cellId ?? ""); }, [notebook, focused]);
  useEffect(() => { setSelected(new Set()); setMenu(null); }, [notebook.sessionId]);
  useEffect(() => { if (!focusRequest) return; const node = refs.current.get(focusRequest.cellId); node?.focus(); node?.scrollIntoView({ block: "center", behavior: "smooth" }); }, [focusRequest]);
  useEffect(() => {
    if (!menu) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") setMenu(null); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [menu]);

  const selectCell = (cellId: string, event: MouseEvent) => {
    setFocused(cellId);
    if (event.shiftKey && anchorRef.current) {
      const a = notebook.cells.findIndex((cell) => cell.cellId === anchorRef.current);
      const b = notebook.cells.findIndex((cell) => cell.cellId === cellId);
      if (a >= 0 && b >= 0) {
        const [lo, hi] = a <= b ? [a, b] : [b, a];
        setSelected(new Set(notebook.cells.slice(lo, hi + 1).map((cell) => cell.cellId)));
        return;
      }
    }
    anchorRef.current = cellId;
    setSelected(new Set([cellId]));
  };

  const openMenu = (cellId: string, event: MouseEvent) => {
    if ((event.target as HTMLElement).closest(".cm-editor, textarea, input, button, a")) return;
    event.preventDefault();
    if (!selected.has(cellId)) { anchorRef.current = cellId; setSelected(new Set([cellId])); }
    setFocused(cellId);
    setMenu({ x: Math.min(event.clientX, window.innerWidth - 210), y: Math.min(event.clientY, window.innerHeight - 150) });
  };

  const applyScope = (editable: boolean) => {
    const ids = notebook.cells.filter((cell) => selected.has(cell.cellId) && (!editable || cell.cellType !== "raw")).map((cell) => cell.cellId);
    if (ids.length) onScopeMany(ids, editable);
    setMenu(null);
  };

  const editableCount = notebook.cells.filter((cell) => selected.has(cell.cellId) && cell.cellType !== "raw").length;

  return <main className="notebook-surface" aria-label="Notebook cells" onKeyDown={(event) => {
    if (!event.altKey || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) return;
    event.preventDefault();
    const index = notebook.cells.findIndex((cell) => cell.cellId === focused);
    const next = Math.max(0, Math.min(notebook.cells.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)));
    const id = notebook.cells[next]?.cellId ?? focused; setFocused(id); const node = refs.current.get(id); node?.focus(); node?.scrollIntoView({ block: "nearest" });
  }}>
    {notebook.cells.map((cell) => <NotebookCell key={`${notebook.sessionId}:${cell.cellId}`} cell={cell} focused={focused === cell.cellId} selected={selected.has(cell.cellId)}
      editable={scope.editableCellIds.includes(cell.cellId)} context={scope.contextCellIds.includes(cell.cellId)} disabled={disabled} sourceActionsDisabled={sourceActionsDisabled}
      cellRef={(node) => { if (node) refs.current.set(cell.cellId, node); else refs.current.delete(cell.cellId); }}
      change={turn?.changes.find((change) => change.cellId === cell.cellId)} onFocus={() => setFocused(cell.cellId)}
      onSelect={(event) => selectCell(cell.cellId, event)} onContextMenu={(event) => openMenu(cell.cellId, event)}
      onDirtyChange={(dirty) => onDirtyChange(cell.cellId, dirty)}
      onSave={(source) => onSave(cell.cellId, source)} onRun={() => onRun(cell.cellId)}
      onAddEditable={() => onScope(cell.cellId, true)} onAddContext={() => onScope(cell.cellId, false)}
      onRevert={() => turn && onRevert(turn.turnId, cell.cellId)} />)}
    {menu && <div className="context-menu-backdrop" onClick={() => setMenu(null)} onContextMenu={(event) => { event.preventDefault(); setMenu(null); }}>
      <div className="cell-context-menu" style={{ left: menu.x, top: menu.y }} role="menu" aria-label="Cell scope actions" onClick={(event) => event.stopPropagation()}>
        <p className="context-menu-heading">{selected.size} cell{selected.size === 1 ? "" : "s"} selected</p>
        <button role="menuitem" disabled={scopeDisabled || editableCount === 0} onClick={() => applyScope(true)}>Add {editableCount} to edit</button>
        <button role="menuitem" disabled={scopeDisabled} onClick={() => applyScope(false)}>Add {selected.size} as context</button>
        <button role="menuitem" onClick={() => { setSelected(new Set()); setMenu(null); }}>Clear selection</button>
      </div>
    </div>}
  </main>;
}
