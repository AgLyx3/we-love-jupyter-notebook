import { Download, FileUp } from "lucide-react";
import type { NotebookSnapshot } from "../api/client";

export default function FileToolbar({ notebook, onUpload }: { notebook: NotebookSnapshot | null; onUpload: (file: File) => void }) {
  return <div className="file-toolbar">
    <label className="icon-button" title="Upload notebook" aria-label="Upload notebook"><FileUp /><input type="file" accept=".ipynb,application/x-ipynb+json" onChange={(event) => { const file = event.target.files?.[0]; if (file) onUpload(file); event.target.value = ""; }} /></label>
    <a className={`icon-button ${notebook ? "" : "disabled"}`} title="Download notebook" aria-label="Download notebook" href={notebook ? "/api/notebooks/download" : undefined} download={notebook?.filename}><Download /></a>
  </div>;
}
