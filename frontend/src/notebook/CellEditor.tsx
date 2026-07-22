import CodeMirror from "@uiw/react-codemirror";
import { python } from "@codemirror/lang-python";

export default function CellEditor({ value, label, disabled, language, onChange, onSave }: { value: string; label: string; disabled: boolean; language: "code" | "markdown" | "raw"; onChange: (value: string) => void; onSave: () => void }) {
  return <CodeMirror
    value={value}
    aria-label={label}
    readOnly={disabled}
    extensions={language === "code" ? [python()] : []}
    basicSetup={{ lineNumbers: true, foldGutter: false, highlightActiveLine: true }}
    onChange={onChange}
    onKeyDown={(event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "s") { event.preventDefault(); onSave(); }
    }}
  />;
}
