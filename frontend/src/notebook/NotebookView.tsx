import { useEffect, useRef, useState, type MouseEvent, type PointerEvent } from "react";
import type { AgentTurn, NotebookSnapshot, TurnScope } from "../api/client";
import NotebookCell from "./NotebookCell";
import type { CellSelection } from "./selectionEdit";

export default function NotebookView({ notebook, scope, turn, disabled, sourceActionsDisabled, autoSave, focusRequest, onDirtyChange, onSave, onRun, onScope, onScopeMany, onRevert, onKeepCell, onAddSelectionToChat, onInlineEdit, onAddErrorToChat }: {
  notebook: NotebookSnapshot; scope: TurnScope; turn: AgentTurn | null;
  disabled: boolean; sourceActionsDisabled: boolean; autoSave: boolean; focusRequest: { cellId: string; requestId: number } | null;
  onDirtyChange: (cellId: string, dirty: boolean) => void;
  onSave: (cellId: string, source: string) => void; onRun: (cellId: string) => void; onScope: (cellId: string, editable: boolean) => void; onScopeMany: (cellIds: string[], editable: boolean) => void; onRevert: (turnId: string, cellId: string) => void;
  onKeepCell?: (turnId: string, cellId: string) => void;
  onAddSelectionToChat?: (selection: CellSelection) => void; onInlineEdit?: (selection: CellSelection, instruction: string) => void; onAddErrorToChat?: (cellId: string, text: string) => void;
}) {
  const [focused, setFocused] = useState(notebook.cells[0]?.cellId ?? "");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const refs = useRef(new Map<string, HTMLElement>());
  const anchorRef = useRef(notebook.cells[0]?.cellId ?? "");
  const [marquee, setMarquee] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const surfaceRef = useRef<HTMLElement | null>(null);
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

  // Marquee (rubber-band) select: press on empty surface background and drag a
  // rectangle; every cell it intersects becomes selected. Hold Shift to add to
  // the existing selection. Dragging near the top/bottom edge auto-scrolls, and
  // the rectangle's anchor tracks the scrolled content so cells scrolled into it
  // are picked up. Ignores presses on a cell so cell drag / text selection /
  // gutter clicks are unaffected.
  const startMarquee = (event: PointerEvent) => {
    if (event.button !== 0 || (event.target as HTMLElement).closest(".notebook-cell, .cell-context-menu")) return;
    event.preventDefault();
    const base = event.shiftKey ? Array.from(selected) : [];
    const box = { x0: event.clientX, y0: event.clientY, x1: event.clientX, y1: event.clientY };
    let pointerX = event.clientX, pointerY = event.clientY;
    setMarquee({ ...box });
    const apply = () => {
      const minX = Math.min(box.x0, box.x1), maxX = Math.max(box.x0, box.x1), minY = Math.min(box.y0, box.y1), maxY = Math.max(box.y0, box.y1);
      const hit = notebook.cells.filter((cell) => {
        const rect = refs.current.get(cell.cellId)?.getBoundingClientRect();
        return rect && rect.right >= minX && rect.left <= maxX && rect.bottom >= minY && rect.top <= maxY;
      }).map((cell) => cell.cellId);
      const next = new Set([...base, ...hit]);
      setSelected(next);
      if (hit.length) { anchorRef.current = hit[0]; setFocused(hit[0]); }
    };
    let raf = 0;
    const EDGE = 48, SPEED = 14;
    const tick = () => {
      const surface = surfaceRef.current;
      if (surface) {
        const rect = surface.getBoundingClientRect();
        const dir = pointerY < rect.top + EDGE ? -1 : pointerY > rect.bottom - EDGE ? 1 : 0;
        if (dir) {
          const before = surface.scrollTop;
          surface.scrollTop += dir * SPEED;
          const applied = surface.scrollTop - before;
          if (applied) { box.y0 -= applied; box.x1 = pointerX; box.y1 = pointerY; setMarquee({ ...box }); apply(); }
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    const onMove = (moveEvent: globalThis.PointerEvent) => { pointerX = moveEvent.clientX; pointerY = moveEvent.clientY; box.x1 = moveEvent.clientX; box.y1 = moveEvent.clientY; setMarquee({ ...box }); apply(); };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      cancelAnimationFrame(raf); apply(); setMarquee(null); document.body.classList.remove("marquee-active");
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    document.body.classList.add("marquee-active");
  };

  const orderedSelection = notebook.cells.filter((cell) => selected.has(cell.cellId)).map((cell) => cell.cellId);
  return <main ref={surfaceRef} className="notebook-surface" aria-label="Notebook cells" onPointerDown={startMarquee} onKeyDown={(event) => {
    if (!event.altKey || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) return;
    event.preventDefault();
    const index = notebook.cells.findIndex((cell) => cell.cellId === focused);
    const next = Math.max(0, Math.min(notebook.cells.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)));
    const id = notebook.cells[next]?.cellId ?? focused; setFocused(id); const node = refs.current.get(id); node?.focus(); node?.scrollIntoView({ block: "nearest" });
  }}>
    {notebook.cells.map((cell) => <NotebookCell key={`${notebook.sessionId}:${cell.cellId}`} cell={cell} focused={focused === cell.cellId} selected={selected.has(cell.cellId)} dragIds={selected.has(cell.cellId) && orderedSelection.length > 1 ? orderedSelection : [cell.cellId]}
      editable={scope.editableCellIds.includes(cell.cellId)} context={scope.contextCellIds.includes(cell.cellId)} disabled={disabled} sourceActionsDisabled={sourceActionsDisabled} autoSave={autoSave}
      cellRef={(node) => { if (node) refs.current.set(cell.cellId, node); else refs.current.delete(cell.cellId); }}
      change={turn?.changes.find((change) => change.cellId === cell.cellId)}
      operations={(turn?.operations ?? []).filter((item) => item.cellId === cell.cellId)}
      onFocus={() => setFocused(cell.cellId)}
      onSelect={(event) => selectCell(cell.cellId, event)} onContextMenu={(event) => openMenu(cell.cellId, event)}
      onDirtyChange={(dirty) => onDirtyChange(cell.cellId, dirty)}
      onSave={(source) => onSave(cell.cellId, source)} onRun={() => onRun(cell.cellId)}
      onAddEditable={() => onScope(cell.cellId, true)} onAddContext={() => onScope(cell.cellId, false)}
      onAddSelectionToChat={onAddSelectionToChat} onInlineEdit={onInlineEdit} onAddErrorToChat={(errorText) => onAddErrorToChat?.(cell.cellId, errorText)}
      onRevert={() => turn && onRevert(turn.turnId, cell.cellId)}
      onKeep={onKeepCell && turn ? () => onKeepCell(turn.turnId, cell.cellId) : undefined} />)}
    {menu && <div className="context-menu-backdrop" onClick={() => setMenu(null)} onContextMenu={(event) => { event.preventDefault(); setMenu(null); }}>
      <div className="cell-context-menu" style={{ left: menu.x, top: menu.y }} role="menu" aria-label="Cell scope actions" onClick={(event) => event.stopPropagation()}>
        <p className="context-menu-heading">{selected.size} cell{selected.size === 1 ? "" : "s"} selected</p>
        <button role="menuitem" disabled={scopeDisabled || editableCount === 0} onClick={() => applyScope(true)}>Add {editableCount} to edit</button>
        <button role="menuitem" disabled={scopeDisabled} onClick={() => applyScope(false)}>Add {selected.size} as context</button>
        <button role="menuitem" onClick={() => { setSelected(new Set()); setMenu(null); }}>Clear selection</button>
      </div>
    </div>}
    {marquee && <div className="marquee-box" style={{ left: Math.min(marquee.x0, marquee.x1), top: Math.min(marquee.y0, marquee.y1), width: Math.abs(marquee.x1 - marquee.x0), height: Math.abs(marquee.y1 - marquee.y0) }} />}
  </main>;
}
