import { ArrowUp, File, FolderOpen, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError, type DirectoryListing } from "../api/client";

export default function FilePicker({ mode = "open", defaultName = "", initialPath, onOpenNotebook, onOpenFolder, onSaveAs, onClose }: {
  mode?: "open" | "save"; defaultName?: string; initialPath?: string;
  onOpenNotebook?: (path: string, workspaceRoot: string) => void; onOpenFolder?: (path: string) => void; onSaveAs?: (path: string) => void; onClose: () => void;
}) {
  const [listing, setListing] = useState<DirectoryListing | null>(null);
  const [root, setRoot] = useState<string | null>(null);
  const [name, setName] = useState(defaultName);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = (path?: string) => {
    setLoading(true); setError(null);
    api.listFiles(path).then((next) => { setListing(next); setLoading(false); }).catch((caught) => { setError(caught instanceof ApiError ? caught.message : "Could not read that folder"); setLoading(false); });
  };
  useEffect(() => { navigate(initialPath); }, []);
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const join = (dir: string, leaf: string) => `${dir.replace(/\/+$/, "")}/${leaf}`;
  const saveName = name.trim().endsWith(".ipynb") ? name.trim() : `${name.trim()}.ipynb`;
  return <div className="dialog-backdrop" onClick={onClose}>
    <section className="file-picker" role="dialog" aria-modal="true" aria-label={mode === "save" ? "Save notebook as" : "Open a notebook or folder"} onClick={(event) => event.stopPropagation()}>
      <header className="file-picker-head">
        <button disabled={!listing?.parent} title="Parent folder" aria-label="Parent folder" onClick={() => navigate(listing?.parent ?? undefined)}><ArrowUp /></button>
        <span className="file-picker-path" title={listing?.path}>{listing?.path ?? "…"}</span>
        <button title="Close" aria-label="Close file picker" onClick={onClose}><X /></button>
      </header>
      {mode === "open" && <div className="file-picker-root">
        <span title={root ?? listing?.path ?? ""}>Workspace folder: {root ?? listing?.path ?? "…"}</span>
        <button disabled={!listing || root === listing.path} onClick={() => listing && setRoot(listing.path)}>Use this folder</button>
      </div>}
      {error && <p className="file-picker-error" role="alert">{error}</p>}
      <ul className="file-picker-list" aria-label="Folder contents">
        {loading && <li className="file-picker-empty">Loading…</li>}
        {!loading && listing && !listing.entries.length && <li className="file-picker-empty">This folder is empty.</li>}
        {!loading && listing?.entries.map((entry) => <li key={entry.path}>{entry.kind === "directory"
          ? <button className="file-picker-row" onClick={() => navigate(entry.path)}><FolderOpen /> {entry.name}</button>
          : entry.kind === "notebook"
          ? <button className="file-picker-row notebook" onClick={() => mode === "save" ? setName(entry.name) : onOpenNotebook?.(entry.path, root ?? listing.path)}><File /> {entry.name}</button>
          : <span className="file-picker-row other" aria-disabled="true" title={`${entry.name} — not a notebook; shown for reference, can't be opened here`}><File /> {entry.name}</span>}</li>)}
      </ul>
      {mode === "save"
        ? <footer className="file-picker-save">
            <input aria-label="File name" value={name} placeholder="notebook.ipynb" onChange={(event) => setName(event.target.value)} />
            <button className="primary" disabled={!listing || !name.trim()} onClick={() => listing && onSaveAs?.(join(listing.path, saveName))}>Save here</button>
          </footer>
        : <footer className="file-picker-foot">
            <span>Click a notebook to open it, or</span>
            <button disabled={!listing} onClick={() => listing && onOpenFolder?.(root ?? listing.path)}>Open this folder</button>
          </footer>}
    </section>
  </div>;
}
