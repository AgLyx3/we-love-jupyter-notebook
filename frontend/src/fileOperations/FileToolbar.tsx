import { Download, FileUp } from "lucide-react";
import type { NotebookSnapshot } from "../api/client";

export default function FileToolbar({ notebook, uploadDisabled = false, onUpload, onDownload }: { notebook: NotebookSnapshot | null; uploadDisabled?: boolean; onUpload: (file: File) => void; onDownload: () => void }) {
  return <div className="file-toolbar">
    <label className={`icon-button ${uploadDisabled ? "disabled" : ""}`} title="Upload notebook" aria-label="Upload notebook"><FileUp /><input disabled={uploadDisabled} type="file" accept=".ipynb,application/x-ipynb+json" onChange={(event) => { const file = event.target.files?.[0]; if (file) onUpload(file); event.target.value = ""; }} /></label>
    <button disabled={!notebook} title="Download notebook" aria-label="Download notebook" onClick={onDownload}><Download /></button>
  </div>;
}
