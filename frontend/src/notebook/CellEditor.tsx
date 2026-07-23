import { useRef, useState } from "react";
import { MessageSquarePlus, Send, Wand2, X } from "lucide-react";
import CodeMirror from "@uiw/react-codemirror";
import { python } from "@codemirror/lang-python";
import { Prec, StateField, type EditorState, type Extension, type Range } from "@codemirror/state";
import { Decoration, EditorView, keymap, WidgetType, type DecorationSet, type ViewUpdate } from "@codemirror/view";
import { cellDiffRanges } from "./cellDiff";
import type { CellSelection } from "./selectionEdit";

class RemovedWidget extends WidgetType {
  constructor(readonly text: string) { super(); }
  eq(other: RemovedWidget) { return other.text === this.text; }
  toDOM() { const el = document.createElement("div"); el.className = "cm-diff-removed-line"; el.textContent = this.text || " "; return el; }
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

export default function CellEditor({ value, label, disabled, language, change, cellId, interactionsDisabled = false, onChange, onSave, onRun, onAddSelectionToChat, onInlineEdit }: {
  value: string; label: string; disabled: boolean; language: "code" | "markdown" | "raw"; change?: { previousSource: string; nextSource: string };
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

  const extensions = [
    ...(language === "code" ? [python()] : []),
    ...(change && change.previousSource !== change.nextSource ? [diffField(change)] : []),
    ...(language === "code" && onRun ? [Prec.highest(keymap.of([
      { key: "Shift-Enter", run: () => { onRun(); return true; } },
      { key: "Mod-Enter", run: () => { onRun(); return true; } },
    ]))] : []),
  ];

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
