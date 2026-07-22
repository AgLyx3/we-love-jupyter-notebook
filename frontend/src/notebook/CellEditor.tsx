import { useMemo } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { python } from "@codemirror/lang-python";
import { StateField, type EditorState, type Extension, type Range } from "@codemirror/state";
import { Decoration, EditorView, WidgetType, type DecorationSet } from "@codemirror/view";
import { cellDiffRanges } from "./cellDiff";

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

export default function CellEditor({ value, label, disabled, language, change, onChange, onSave }: { value: string; label: string; disabled: boolean; language: "code" | "markdown" | "raw"; change?: { previousSource: string; nextSource: string }; onChange: (value: string) => void; onSave: () => void }) {
  const extensions = useMemo(() => [
    ...(language === "code" ? [python()] : []),
    ...(change && change.previousSource !== change.nextSource ? [diffField(change)] : []),
  ], [language, change?.previousSource, change?.nextSource]);
  return <CodeMirror
    value={value}
    aria-label={label}
    readOnly={disabled}
    extensions={extensions}
    basicSetup={{ lineNumbers: true, foldGutter: false, highlightActiveLine: true }}
    onChange={onChange}
    onKeyDown={(event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "s") { event.preventDefault(); onSave(); }
    }}
  />;
}
