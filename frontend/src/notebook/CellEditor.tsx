import { useMemo, useRef, useState } from "react";
import { MessageSquarePlus, Send, Wand2, X } from "lucide-react";
import CodeMirror from "@uiw/react-codemirror";
import { python } from "@codemirror/lang-python";
import { Prec, StateField, type EditorState, type Extension, type Range } from "@codemirror/state";
import { Decoration, EditorView, keymap, WidgetType, type DecorationSet, type ViewUpdate } from "@codemirror/view";
import { cellDiffRanges, type HunkOverlay } from "./cellDiff";
import type { CellSelection } from "./selectionEdit";

export type HunkControls = {
  overlays: HunkOverlay[];
  disabled: boolean;
  onKeep: (operationId: string) => void;
  onUndo: (operationId: string) => void;
};

class RemovedWidget extends WidgetType {
  constructor(readonly text: string) { super(); }
  eq(other: RemovedWidget) { return other.text === this.text; }
  toDOM() { const el = document.createElement("div"); el.className = "cm-diff-removed-line"; el.textContent = this.text || " "; return el; }
}

// One reviewable hunk: its removed lines (if any) plus Keep/Undo, rendered as a
// block widget above the hunk so the control sits on the change it acts on —
// never in a corner cluster (§4.6). WidgetType.ignoreEvent defaults to true, so
// button clicks stay in the widget instead of moving the editor selection.
class HunkWidget extends WidgetType {
  constructor(
    readonly overlay: HunkOverlay,
    readonly disabled: boolean,
    readonly onKeep: (operationId: string) => void,
    readonly onUndo: (operationId: string) => void,
  ) { super(); }
  eq(other: HunkWidget) {
    return other.overlay.operationId === this.overlay.operationId
      && other.overlay.line === this.overlay.line
      && other.disabled === this.disabled
      && other.overlay.removed.join("\n") === this.overlay.removed.join("\n");
  }
  toDOM() {
    const wrap = document.createElement("div");
    wrap.className = "cm-hunk-widget";
    for (const text of this.overlay.removed) {
      const line = document.createElement("div");
      line.className = "cm-diff-removed-line";
      line.textContent = text || " ";
      wrap.appendChild(line);
    }
    const bar = document.createElement("div");
    bar.className = "cm-hunk-actions";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Review this change");
    // Same markup and classes as the cell-header pair (icon + label, shared
    // .review-action styling), so a Keep here and a Keep there are visibly the
    // same control acting at different scopes.
    const make = (label: string, variant: string, icon: string, onClick: () => void) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `review-action ${variant}`;
      button.disabled = this.disabled;
      button.setAttribute("aria-label", `${label} this change`);
      const glyph = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      glyph.setAttribute("viewBox", "0 0 24 24");
      glyph.setAttribute("fill", "none");
      glyph.setAttribute("stroke", "currentColor");
      glyph.setAttribute("stroke-width", "2");
      glyph.setAttribute("stroke-linecap", "round");
      glyph.setAttribute("stroke-linejoin", "round");
      glyph.setAttribute("aria-hidden", "true");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", icon);
      glyph.appendChild(path);
      button.appendChild(glyph);
      button.appendChild(document.createTextNode(label));
      button.addEventListener("click", (event) => { event.preventDefault(); onClick(); });
      bar.appendChild(button);
    };
    // lucide "check" and "rotate-ccw", matching the header icons.
    make("Keep", "keep", "M20 6 9 17l-5-5", () => this.onKeep(this.overlay.operationId));
    make("Undo", "undo", "M3 7v6h6M21 17a9 9 0 0 0-15-6.7L3 13", () => this.onUndo(this.overlay.operationId));
    wrap.appendChild(bar);
    return wrap;
  }
}

function decorateHunks(state: EditorState, controls: HunkControls): DecorationSet {
  const ranges: Range<Decoration>[] = [];
  const lines = state.doc.lines;
  for (const overlay of controls.overlays) {
    const trailing = overlay.line >= lines;
    const pos = trailing ? state.doc.line(lines).to : state.doc.line(overlay.line + 1).from;
    ranges.push(Decoration.widget({
      widget: new HunkWidget(overlay, controls.disabled, controls.onKeep, controls.onUndo),
      block: true, side: trailing ? 1 : -1,
    }).range(pos));
    for (let index = overlay.line; index < overlay.line + overlay.added; index += 1) {
      if (index < lines) ranges.push(Decoration.line({ class: "cm-diff-added-line" }).range(state.doc.line(index + 1).from));
    }
  }
  return Decoration.set(ranges, true);
}

function hunkField(controls: HunkControls): Extension {
  return StateField.define<DecorationSet>({
    create: (state) => decorateHunks(state, controls),
    update: (deco, tr) => tr.docChanged ? decorateHunks(tr.state, controls) : deco,
    provide: (field) => EditorView.decorations.from(field),
  });
}

function decorateDiff(state: EditorState, added: number[], removed: { line: number; text: string }[]): DecorationSet {
  const ranges: Range<Decoration>[] = [];
  const lines = state.doc.lines;
  for (const index of added) if (index < lines) ranges.push(Decoration.line({ class: "cm-diff-added-line" }).range(state.doc.line(index + 1).from));
  for (const { line, text } of removed) {
    const trailing = line >= lines;
    const pos = trailing ? state.doc.line(lines).to : state.doc.line(line + 1).from;
    ranges.push(Decoration.widget({ widget: new RemovedWidget(text), block: true, side: trailing ? 1 : -1 }).range(pos));
  }
  return Decoration.set(ranges, true);
}

function diffField(change: { previousSource: string; nextSource: string }): Extension {
  const { added, removed } = cellDiffRanges(change.previousSource, change.nextSource);
  return StateField.define<DecorationSet>({
    create: (state) => decorateDiff(state, added, removed),
    update: (deco, tr) => tr.docChanged ? decorateDiff(tr.state, added, removed) : deco,
    provide: (field) => EditorView.decorations.from(field),
  });
}

export default function CellEditor({ value, label, disabled, language, change, hunkControls, cellId, interactionsDisabled = false, onChange, onSave, onRun, onAddSelectionToChat, onInlineEdit }: {
  value: string; label: string; disabled: boolean; language: "code" | "markdown" | "raw"; change?: { previousSource: string; nextSource: string };
  hunkControls?: HunkControls;
  cellId?: string; interactionsDisabled?: boolean;
  onChange: (value: string) => void; onSave: () => void; onRun?: () => void;
  onAddSelectionToChat?: (selection: CellSelection) => void; onInlineEdit?: (selection: CellSelection, instruction: string) => void;
}) {
  const viewRef = useRef<EditorView | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [toolbar, setToolbar] = useState<{ top: number; left: number; above: boolean; selection: CellSelection } | null>(null);
  const [inlineEdit, setInlineEdit] = useState<{ selection: CellSelection; top: number } | null>(null);
  const [instruction, setInstruction] = useState("");
  const selectionActionsEnabled = Boolean(cellId) && (Boolean(onAddSelectionToChat) || Boolean(onInlineEdit));

  // Rebuilding this array redefines the diff StateField and reconfigures the
  // editor, so it is memoized: without it every keystroke anywhere in the
  // notebook reconfigures every cell under review. onRun goes through a ref so
  // its per-render identity does not invalidate the memo.
  const runRef = useRef(onRun);
  runRef.current = onRun;
  const extensions = useMemo(() => [
    ...(language === "code" ? [python()] : []),
    // Ledger-driven overlays win when available: they map hunks onto the
    // *composed* document, so highlights stay aligned after a partial undo —
    // the legacy previous→next diff cannot represent that state. Legacy stays
    // for pre-ledger turns, stale cells, and trusted cells without operations.
    ...(hunkControls && hunkControls.overlays.length ? [hunkField(hunkControls)]
      : change && change.previousSource !== change.nextSource ? [diffField(change)] : []),
    ...(language === "code" && onRun ? [Prec.highest(keymap.of([
      { key: "Shift-Enter", run: () => { runRef.current?.(); return true; } },
      { key: "Mod-Enter", run: () => { runRef.current?.(); return true; } },
    ]))] : []),
  ], [language, hunkControls, change?.previousSource, change?.nextSource, Boolean(onRun)]);

  const readSelection = (view: EditorView): CellSelection | null => {
    if (!cellId) return null;
    const { from, to } = view.state.selection.main;
    if (from === to) return null;
    return {
      cellId,
      text: view.state.sliceDoc(from, to),
      startLine: view.state.doc.lineAt(from).number,
      endLine: view.state.doc.lineAt(to).number,
    };
  };

  const refreshToolbar = (view: EditorView) => {
    if (!selectionActionsEnabled || interactionsDisabled) { setToolbar(null); return; }
    const selection = readSelection(view);
    if (!selection || !view.hasFocus) { setToolbar(null); return; }
    const wrapper = wrapperRef.current;
    const coords = view.coordsAtPos(view.state.selection.main.from);
    if (!wrapper || !coords) { setToolbar((current) => current ?? { top: 0, left: 8, above: false, selection }); return; }
    const rect = wrapper.getBoundingClientRect();
    const relativeTop = coords.top - rect.top;
    const above = relativeTop > 30;
    setToolbar({
      top: above ? relativeTop - 6 : (coords.bottom - rect.top) + 6,
      left: Math.max(4, Math.min(coords.left - rect.left, rect.width - 200)),
      above,
      selection,
    });
  };

  const openInlineEdit = (selection: CellSelection) => {
    const view = viewRef.current;
    const wrapper = wrapperRef.current;
    let top = 0;
    if (view && wrapper) {
      const coords = view.coordsAtPos(view.state.selection.main.to);
      if (coords) top = Math.max(0, coords.bottom - wrapper.getBoundingClientRect().top + 4);
    }
    setInstruction("");
    setToolbar(null);
    setInlineEdit({ selection, top });
  };

  return <div ref={wrapperRef} className="cell-editor">
    <CodeMirror
      value={value}
      aria-label={label}
      readOnly={disabled}
      extensions={extensions}
      basicSetup={{ lineNumbers: true, foldGutter: false, highlightActiveLine: true }}
      onCreateEditor={(view) => { viewRef.current = view; }}
      onChange={onChange}
      onUpdate={(update: ViewUpdate) => { if (update.selectionSet || update.docChanged || update.focusChanged) refreshToolbar(update.view); }}
      onKeyDown={(event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === "s") { event.preventDefault(); onSave(); }
      }}
    />
    {toolbar && !inlineEdit && <div className={`selection-toolbar ${toolbar.above ? "above" : "below"}`} role="toolbar" aria-label="Selection actions"
      style={{ top: toolbar.top, left: toolbar.left }}
      onMouseDown={(event) => event.preventDefault()}>
      {onAddSelectionToChat && <button type="button" onClick={() => { onAddSelectionToChat(toolbar.selection); setToolbar(null); }}><MessageSquarePlus /> Add to chat</button>}
      {onInlineEdit && <button type="button" onClick={() => openInlineEdit(toolbar.selection)}><Wand2 /> Edit inline</button>}
    </div>}
    {inlineEdit && <form className="inline-edit-widget" style={{ top: inlineEdit.top }} aria-label="Inline edit instruction" onSubmit={(event) => {
      event.preventDefault();
      const value = instruction.trim();
      if (!value || interactionsDisabled) return;
      onInlineEdit?.(inlineEdit.selection, value);
      setInlineEdit(null);
    }}>
      <div className="inline-edit-row">
        <input autoFocus value={instruction} disabled={interactionsDisabled} placeholder="Describe the edit for the selection…"
          aria-label="Inline edit instruction"
          onChange={(event) => setInstruction(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); setInlineEdit(null); } }} />
        <button className="primary" type="submit" disabled={interactionsDisabled || !instruction.trim()} aria-label="Run inline edit"><Send /></button>
        <button type="button" aria-label="Cancel inline edit" onClick={() => setInlineEdit(null)}><X /></button>
      </div>
      <small>{inlineEdit.selection.startLine === inlineEdit.selection.endLine ? `Line ${inlineEdit.selection.startLine}` : `Lines ${inlineEdit.selection.startLine}–${inlineEdit.selection.endLine}`} · the agent edits the whole cell, focused on your selection</small>
    </form>}
  </div>;
}
