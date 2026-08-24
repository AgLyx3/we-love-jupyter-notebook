import Icon from "../ui/Icon";
import { useEffect, useState } from "react";
import { api, type DirectoryEntry, type NotebookSnapshot } from "../api/client";
import OutlinePanel from "../notebookOverview/OutlinePanel";

const basename = (path: string): string => path.replace(/\/+$/, "").split("/").pop() || path;
const pad = (depth: number) => ({ paddingLeft: `${depth * 12 + 8}px` });

export type RailTab = "files" | "outline";

// Tab choice is remembered per notebook (spec §7.1). Save As changes the path,
// which is treated as a new notebook; an uploaded notebook has no path at all,
// so it falls back to the default tab rather than inheriting someone else's.
const tabKey = (notebookPath: string | null | undefined): string | null =>
  notebookPath ? `notebook-rail-tab:${notebookPath}` : null;

export function readRailTab(notebookPath: string | null | undefined, fallback: RailTab): RailTab {
  const key = tabKey(notebookPath);
  if (!key) return fallback;
  try {
    const stored = window.localStorage.getItem(key);
    return stored === "files" || stored === "outline" ? stored : fallback;
  } catch {
    return fallback;
  }
}

export function writeRailTab(notebookPath: string | null | undefined, tab: RailTab): void {
  const key = tabKey(notebookPath);
  if (!key) return;
  try { window.localStorage.setItem(key, tab); } catch { /* private mode; the tab just won't persist */ }
}

function TreeChildren({ path, depth, activePath, onOpenNotebook }: { path: string; depth: number; activePath: string | null; onOpenNotebook: (path: string) => void }) {
  const [entries, setEntries] = useState<DirectoryEntry[] | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => { let live = true; setLoading(true); api.listFiles(path).then((listing) => { if (live) { setEntries(listing.entries); setLoading(false); } }).catch(() => { if (live) { setEntries([]); setLoading(false); } }); return () => { live = false; }; }, [path]);
  if (loading) return <div className="tree-empty" style={pad(depth)}>Loading…</div>;
  if (!entries?.length) return <div className="tree-empty" style={pad(depth)}>empty</div>;
  return <>{entries.map((entry) => entry.kind === "directory"
    ? <TreeFolder key={entry.path} path={entry.path} name={entry.name} depth={depth} activePath={activePath} onOpenNotebook={onOpenNotebook} />
    : entry.kind === "notebook"
    ? <button key={entry.path} className={`tree-row notebook ${activePath === entry.path ? "active" : ""}`} style={pad(depth)} onClick={() => onOpenNotebook(entry.path)} title={entry.name}><Icon name="description" /><span>{entry.name}</span></button>
    : <span key={entry.path} className="tree-row other" style={pad(depth)} aria-disabled="true" title={`${entry.name} — not a notebook; shown for reference, can't be opened`}><Icon name="description" /><span>{entry.name}</span></span>)}</>;
}

function TreeFolder({ path, name, depth, activePath, onOpenNotebook }: { path: string; name: string; depth: number; activePath: string | null; onOpenNotebook: (path: string) => void }) {
  const [open, setOpen] = useState(false);
  return <>
    <button className="tree-row" style={pad(depth)} onClick={() => setOpen((value) => !value)} aria-expanded={open} title={name}>{open ? <Icon name="expand_more" /> : <Icon name="chevron_right" />}<Icon name="folder" /><span>{name}</span></button>
    {open && <TreeChildren path={path} depth={depth + 1} activePath={activePath} onOpenNotebook={onOpenNotebook} />}
  </>;
}

/** The one left rail, with both tabs in it.
 *
 *  Not a second region beside the file tree: `.workspace-layout` is already
 *  sidebar + notebook + agent panel, and a third rail squeezes the notebook
 *  body (spec §7.1). The file tree is workspace-scoped and the outline is
 *  notebook-scoped, so with no folder open there is no tree and the outline
 *  takes the whole rail with no tabs rendered.
 */
export default function WorkspaceSidebar({ root, activePath, notebook, tab, onTabChange, onOpenNotebook, onCollapse, onJumpToCell, onHoverBlock }: {
  root: string | null;
  activePath: string | null;
  notebook: NotebookSnapshot | null;
  tab: RailTab;
  onTabChange: (tab: RailTab) => void;
  onOpenNotebook: (path: string) => void;
  onCollapse: () => void;
  onJumpToCell: (cellId: string) => void;
  onHoverBlock?: (cellIds: string[] | null) => void;
}) {
  const showTabs = Boolean(root) && Boolean(notebook);
  // With only one of the two available, that one is what shows regardless of
  // the remembered tab — an Outline tab with no notebook behind it, or a Files
  // tab with no folder, would just be an empty box.
  const active: RailTab = !root ? "outline" : !notebook ? "files" : tab;
  return <aside className="workspace-sidebar" aria-label={active === "outline" ? "Notebook outline" : "Workspace files"}>
    <header className="workspace-sidebar-head">
      {showTabs
        ? <div className="rail-tabs" role="tablist" aria-label="Sidebar view">
            <button role="tab" aria-selected={active === "files"} className={active === "files" ? "active" : ""} onClick={() => onTabChange("files")}>Files</button>
            <button role="tab" aria-selected={active === "outline"} className={active === "outline" ? "active" : ""} onClick={() => onTabChange("outline")}>Outline</button>
          </div>
        : root
        ? <span title={root}>{basename(root)}</span>
        // With no folder open the rail is all outline. Naming the section beats
        // repeating the filename, which the title bar already shows just above.
        : <span>Outline</span>}
      <button title="Hide sidebar" aria-label="Hide sidebar" onClick={onCollapse}><Icon name="keyboard_tab_rtl" /></button>
    </header>
    {/* #33: this was a ternary, so switching to Files unmounted OutlinePanel
        and took its `generating` flag with it. Coming back mid-build showed
        "Build map" rather than "Building…", the request was unobservable, and
        pressing the button spent a second model call.

        Both panes stay mounted and the inactive one is hidden, so a build
        survives a tab switch. `hidden` rather than `display: none` in CSS
        because it also takes the pane out of the accessibility tree — a file
        tree a screen reader can still walk while the Outline tab is selected
        is the same bug in a different sense. */}
    {root && <div className="workspace-tree" aria-label="File tree" hidden={active !== "files"}>
      <TreeChildren path={root} depth={0} activePath={activePath} onOpenNotebook={onOpenNotebook} />
    </div>}
    {notebook
      ? <div className="workspace-tree" aria-label="Notebook outline" hidden={active !== "outline"}>
          <OutlinePanel notebook={notebook} active={active === "outline"} onJump={onJumpToCell} onHoverBlock={onHoverBlock} />
        </div>
      : active === "outline" && <div className="tree-empty">Open a notebook to see its outline.</div>}
    {/* B10. Pinned footer entry. There is no settings screen behind it, so it
        is rendered disabled and says why rather than opening onto nothing. */}
    <div className="sidebar-footer">
      <button type="button" className="sidebar-settings" disabled title="Settings — not built yet"><Icon name="settings" /> Settings</button>
    </div>
  </aside>;
}
