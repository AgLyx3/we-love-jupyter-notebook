import Icon from "../ui/Icon";
import type { NotebookSnapshot } from "../api/client";

export default function FileToolbar({ notebook, saveDisabled = false, saveAsDisabled = false, closeDisabled = false, onBrowse, onSave, onSaveAs, onClose }: { notebook: NotebookSnapshot | null; saveDisabled?: boolean; saveAsDisabled?: boolean; closeDisabled?: boolean; onBrowse: () => void; onSave: () => void; onSaveAs: () => void; onClose: () => void }) {
  return <div className="file-toolbar">
    <button title="Open a notebook or folder" aria-label="Open a notebook or folder" onClick={onBrowse}><Icon name="folder_open" /></button>
    {notebook && <button disabled={saveDisabled} title="Save" aria-label="Save notebook" onClick={onSave}><Icon name="save" /></button>}
    {notebook && <button disabled={saveAsDisabled} title="Save as…" aria-label="Save notebook as" onClick={onSaveAs}><Icon name="save_as" /></button>}
    {notebook && <button disabled={closeDisabled} title="Close notebook" aria-label="Close notebook" onClick={onClose}><Icon name="close" /></button>}
  </div>;
}
