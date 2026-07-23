import { ChevronDown, ChevronRight, File, Folder, PanelLeftClose } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type DirectoryEntry } from "../api/client";

const basename = (path: string): string => path.replace(/\/+$/, "").split("/").pop() || path;
const pad = (depth: number) => ({ paddingLeft: `${depth * 12 + 8}px` });

function TreeChildren({ path, depth, activePath, onOpenNotebook }: { path: string; depth: number; activePath: string | null; onOpenNotebook: (path: string) => void }) {
  const [entries, setEntries] = useState<DirectoryEntry[] | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => { let live = true; setLoading(true); api.listFiles(path).then((listing) => { if (live) { setEntries(listing.entries); setLoading(false); } }).catch(() => { if (live) { setEntries([]); setLoading(false); } }); return () => { live = false; }; }, [path]);
  if (loading) return <div className="tree-empty" style={pad(depth)}>Loading…</div>;
  if (!entries?.length) return <div className="tree-empty" style={pad(depth)}>empty</div>;
  return <>{entries.map((entry) => entry.kind === "directory"
    ? <TreeFolder key={entry.path} path={entry.path} name={entry.name} depth={depth} activePath={activePath} onOpenNotebook={onOpenNotebook} />
    : entry.kind === "notebook"
    ? <button key={entry.path} className={`tree-row notebook ${activePath === entry.path ? "active" : ""}`} style={pad(depth)} onClick={() => onOpenNotebook(entry.path)} title={entry.name}><File /><span>{entry.name}</span></button>
    : <span key={entry.path} className="tree-row other" style={pad(depth)} aria-disabled="true" title={`${entry.name} — not a notebook; shown for reference, can't be opened`}><File /><span>{entry.name}</span></span>)}</>;
}

function TreeFolder({ path, name, depth, activePath, onOpenNotebook }: { path: string; name: string; depth: number; activePath: string | null; onOpenNotebook: (path: string) => void }) {
  const [open, setOpen] = useState(false);
  return <>
    <button className="tree-row" style={pad(depth)} onClick={() => setOpen((value) => !value)} aria-expanded={open} title={name}>{open ? <ChevronDown /> : <ChevronRight />}<Folder /><span>{name}</span></button>
    {open && <TreeChildren path={path} depth={depth + 1} activePath={activePath} onOpenNotebook={onOpenNotebook} />}
  </>;
}

export default function WorkspaceSidebar({ root, activePath, onOpenNotebook, onCollapse }: { root: string; activePath: string | null; onOpenNotebook: (path: string) => void; onCollapse: () => void }) {
  return <aside className="workspace-sidebar" aria-label="Workspace files">
    <header className="workspace-sidebar-head"><span title={root}>{basename(root)}</span><button title="Hide files" aria-label="Hide file tree" onClick={onCollapse}><PanelLeftClose /></button></header>
    <div className="workspace-tree" aria-label="File tree"><TreeChildren path={root} depth={0} activePath={activePath} onOpenNotebook={onOpenNotebook} /></div>
  </aside>;
}
