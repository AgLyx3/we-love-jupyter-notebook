import Icon from "./ui/Icon";
import { useCallback, useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";
import { ApiError, api, connectEvents, type AgentOperation, type AgentTurn, type ExecutionAttempt, type ExecutionOperation, type KernelStatus, type NotebookSnapshot, type TuningRecord, type TuningValue, type TurnScope, type WriteScope } from "./api/client";
import { hasImageOutput } from "./notebook/NotebookCell";
import ReviewBar from "./notebook/ReviewBar";
import AgentChatPanel from "./agentChat/AgentChatPanel";
import type { TurnRecord } from "./agentChat/AgentChatPanel";
import KernelControls from "./execution/KernelControls";
import CloseNotebookDialog from "./fileOperations/CloseNotebookDialog";
import FilePicker from "./fileOperations/FilePicker";
import WorkspaceSidebar, { readRailTab, writeRailTab, type RailTab } from "./fileOperations/WorkspaceSidebar";
import FileToolbar from "./fileOperations/FileToolbar";
import NotebookView from "./notebook/NotebookView";
import { composeChatPrompt, composeInlineEditPrompt, defaultAttachmentInstruction, makeAttachment, makeErrorAttachment, type CellSelection, type SelectionAttachment } from "./notebook/selectionEdit";
import { setTheme, useTheme } from "./theme";

const AGENT_MIN_WIDTH = 300;
const AGENT_MAX_WIDTH = 760;
const AGENT_WIDTH_KEY = "notebook-agent-width";
const AUTO_SAVE_KEY = "notebook-auto-save";
const clampAgentWidth = (value: number): number => Math.min(AGENT_MAX_WIDTH, Math.max(AGENT_MIN_WIDTH, Math.round(value)));

const emptyScope: TurnScope = { editableCellIds: [], contextCellIds: [], sessionId: null, notebookRevision: null };
const emptyKernel: KernelStatus = { kernelSessionId: null, state: "not_started", executionAttemptId: null };
const terminalTurns = new Set(["completed", "failed", "cancelled", "validation_incomplete", "timed_out"]);
const terminalExecutions = new Set(["completed", "failed", "cancelled", "validation_incomplete", "timed_out"]);

/** The one control for the light/dark switch the redesign adds (stitch-diff
 *  E1). The default follows the OS; pressing this pins a choice. */
function ThemeToggle() {
  const mode = useTheme();
  const next = mode === "dark" ? "light" : "dark";
  return <button type="button" className="theme-toggle" aria-label={`Switch to ${next} theme`} title={`Switch to the ${next} theme`} onClick={() => setTheme(next)}>
    <Icon name={mode === "dark" ? "light_mode" : "dark_mode"} />
  </button>;
}

export default function App() {
  const [notebook, setNotebook] = useState<NotebookSnapshot | null>(null);
  const [scope, setScope] = useState<TurnScope>(emptyScope);
  const [kernel, setKernel] = useState<KernelStatus>(emptyKernel);
  const [turn, setTurn] = useState<AgentTurn | null>(null);
  const [history, setHistory] = useState<TurnRecord[]>([]);
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const [focusRequest, setFocusRequest] = useState<{ cellId: string; requestId: number } | null>(null);
  const [attachments, setAttachments] = useState<SelectionAttachment[]>([]);
  const [operation, setOperation] = useState<ExecutionOperation | null>(null);
  // The last Apply from a tuning panel. Held here rather than fetched with the
  // rest of the session because a tune record has no live state to poll: it is
  // created complete, and only this window's Keep/Undo moves it afterwards.
  const [tuningRecord, setTuningRecord] = useState<TuningRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "error" | "warning"; text: string } | null>(null);
  const [dirtyCellIds, setDirtyCellIds] = useState<Set<string>>(() => new Set());
  const [polling, setPolling] = useState(false);
  const [closeTarget, setCloseTarget] = useState<{ sessionId: string; revision: number } | null>(null);
  const [picking, setPicking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [workspaceFolder, setWorkspaceFolder] = useState<string | null>(null);
  const [sidebarHidden, setSidebarHidden] = useState(false);
  const [railTab, setRailTab] = useState<RailTab>("files");
  // Cells the hovered outline block covers, highlighted in the gutter.
  const [outlineHighlight, setOutlineHighlight] = useState<ReadonlySet<string> | null>(null);
  const [agentWidth, setAgentWidth] = useState<number>(() => {
    const saved = Number(localStorage.getItem(AGENT_WIDTH_KEY));
    return Number.isFinite(saved) && saved > 0 ? clampAgentWidth(saved) : 360;
  });
  useEffect(() => { localStorage.setItem(AGENT_WIDTH_KEY, String(agentWidth)); }, [agentWidth]);
  const [autoSave, setAutoSave] = useState<boolean>(() => localStorage.getItem(AUTO_SAVE_KEY) === "on");
  useEffect(() => { localStorage.setItem(AUTO_SAVE_KEY, autoSave ? "on" : "off"); }, [autoSave]);
  // Write scope (Blocking/Trusted) is sticky and lifted here because it changes
  // several sibling views: the composer, the notebook gutter (Trusted blocks the
  // "allow agent edit" button), the scope panel, and how a dropped cell is scoped
  // (Trusted defaults a drop to context, since "editable" is removed in Trusted).
  const [writeScope, setWriteScope] = useState<WriteScope>(() => {
    try { return localStorage.getItem("agent.writeScope") === "trusted" ? "trusted" : "blocking"; } catch { return "blocking"; }
  });
  useEffect(() => { try { localStorage.setItem("agent.writeScope", writeScope); } catch { /* storage unavailable */ } }, [writeScope]);
  const trustedScope = writeScope === "trusted";
  const startAgentResize = useCallback((event: PointerEvent) => {
    event.preventDefault();
    const onMove = (moveEvent: globalThis.PointerEvent) => setAgentWidth(clampAgentWidth(window.innerWidth - moveEvent.clientX));
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      document.body.classList.remove("resizing-agent");
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    document.body.classList.add("resizing-agent");
  }, []);
  const nudgeAgentResize = useCallback((event: KeyboardEvent) => {
    if (event.key === "ArrowLeft") { event.preventDefault(); setAgentWidth((width) => clampAgentWidth(width + 24)); }
    else if (event.key === "ArrowRight") { event.preventDefault(); setAgentWidth((width) => clampAgentWidth(width - 24)); }
  }, []);
  const snapshotRef = useRef(notebook);
  const scopeRef = useRef(scope);
  const eventCursorRef = useRef({ sessionId: "", sequence: 0 });
  const refreshGenerationRef = useRef(0);
  const turnGenerationRef = useRef(new Map<string, number>());
  const executionGenerationRef = useRef(new Map<string, number>());
  const resourceEpochRef = useRef(0);
  const mutationGenerationRef = useRef(0);
  useEffect(() => { snapshotRef.current = notebook; }, [notebook]);
  useEffect(() => { scopeRef.current = scope; }, [scope]);
  useEffect(() => { setDirtyCellIds(new Set()); setAttachments([]); setTuningRecord(null); }, [notebook?.sessionId]);
  // Tab memory is per notebook, keyed by path (spec §7.1). Save As changes the
  // path and so counts as a new notebook; an uploaded notebook has no path and
  // falls back to the default rather than inheriting the last file's choice.
  useEffect(() => { setRailTab(readRailTab(notebook?.notebookPath, "files")); }, [notebook?.notebookPath]);
  useEffect(() => { setOutlineHighlight(null); }, [notebook?.sessionId]);
  useEffect(() => {
    setCloseTarget((target) => target && (target.sessionId !== notebook?.sessionId || target.revision !== notebook.revision) ? null : target);
  }, [notebook?.sessionId, notebook?.revision]);

  const refresh = useCallback(async () => {
    const epoch = ++resourceEpochRef.current;
    const generation = ++refreshGenerationRef.current;
    try {
      const current = await api.current();
      const [nextScope, nextKernel, status] = await Promise.all([api.scope(), api.kernel(), api.status()]);
      if (generation !== refreshGenerationRef.current || resourceEpochRef.current !== epoch) return snapshotRef.current;
      const existing = snapshotRef.current;
      if (existing && existing.sessionId === current.sessionId && existing.revision > current.revision) return existing;
      const nextNotebook = current;
      snapshotRef.current = nextNotebook;
      setNotebook(nextNotebook);
      setScope(nextScope);
      setKernel(nextKernel);
      const hydrated = (status.turnHistory ?? []).map(turnToRecord).slice(0, 50);
      setHistory((items) => reconcileHistory(hydrated, items, nextNotebook, status.turnHistoryTruncated ?? false));
      setTurn(status.activeTurn ? reconcileTurnChanges(status.activeTurn, nextNotebook) : hydrated[0]?.turn ?? null);
      setSelectedTurnId((selected) => selected && (hydrated.some((item) => item.turn.turnId === selected) || status.turnHistoryTruncated) ? selected : hydrated[0]?.turn.turnId ?? null);
      setOperation(status.activeExecution);
      return nextNotebook;
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) { setNotebook(null); setScope(emptyScope); return null; }
      throw error;
    }
  }, []);

  useEffect(() => { refresh().catch((error) => showError(error)).finally(() => setLoading(false)); }, [refresh]);

  const fetchTurn = useCallback(async (id: string, refreshTerminal = true) => {
    const epoch = ++resourceEpochRef.current;
    const generation = (turnGenerationRef.current.get(id) ?? 0) + 1;
    turnGenerationRef.current.set(id, generation);
    try {
      const next = await api.turn(id);
      if (turnGenerationRef.current.get(id) !== generation || resourceEpochRef.current !== epoch) return;
      if (terminalTurns.has(next.state) && refreshTerminal) {
        const refreshedNotebook = await refresh();
        if (turnGenerationRef.current.get(id) !== generation) return;
        const detailEpoch = resourceEpochRef.current;
        const detailGeneration = (turnGenerationRef.current.get(id) ?? 0) + 1;
        turnGenerationRef.current.set(id, detailGeneration);
        const refreshed = await api.turn(id);
        if (turnGenerationRef.current.get(id) !== detailGeneration || resourceEpochRef.current !== detailEpoch) return;
        const detailed = reconcileTurnChanges(refreshed, refreshedNotebook);
        setTurn(detailed); setSelectedTurnId((selected) => selected ?? id);
        setHistory((items) => upsertRecord(items, detailed, scopeRef.current, items.find((item) => item.turn.turnId === id)?.prompt ?? "Agent turn"));
        return;
      }
      const detailed = reconcileTurnChanges(next, snapshotRef.current);
      setTurn(detailed); setSelectedTurnId((selected) => selected ?? id);
      setHistory((items) => upsertRecord(items, detailed, scopeRef.current, items.find((item) => item.turn.turnId === id)?.prompt ?? "Agent turn"));
      if (next.executionOperationId) {
        const operationId = next.executionOperationId;
        const operationGeneration = (executionGenerationRef.current.get(operationId) ?? 0) + 1;
        executionGenerationRef.current.set(operationId, operationGeneration);
        const nextOperation = await api.execution(operationId);
        if (turnGenerationRef.current.get(id) !== generation || executionGenerationRef.current.get(operationId) !== operationGeneration || resourceEpochRef.current !== epoch) return;
        setOperation(nextOperation);
      }
    } catch (error) { showError(error); }
  }, [refresh]);

  useEffect(() => {
    const selected = history.find((item) => item.turn.turnId === selectedTurnId)?.turn;
    if (selected?.historyTruncated) void fetchTurn(selected.turnId, false);
  }, [fetchTurn, history, selectedTurnId]);

  const fetchExecution = useCallback(async (id: string) => {
    const epoch = ++resourceEpochRef.current;
    const generation = (executionGenerationRef.current.get(id) ?? 0) + 1;
    executionGenerationRef.current.set(id, generation);
    try {
      const next = await api.execution(id);
      if (executionGenerationRef.current.get(id) !== generation || resourceEpochRef.current !== epoch) return;
      setOperation(next);
      if (terminalExecutions.has(next.state)) await refresh();
      else {
        const nextKernel = await api.kernel();
        if (executionGenerationRef.current.get(id) !== generation || resourceEpochRef.current !== epoch) return;
        setKernel(nextKernel);
      }
    } catch (error) { showError(error); }
  }, [refresh]);

  useEffect(() => {
    if (!notebook) return;
    if (eventCursorRef.current.sessionId !== notebook.sessionId) eventCursorRef.current = { sessionId: notebook.sessionId, sequence: 0 };
    setPolling(false);
    return connectEvents(notebook.sessionId, eventCursorRef.current.sequence, {
      notebook: () => void refresh(), turn: (id) => void fetchTurn(id), execution: (id) => void fetchExecution(id), disconnected: () => setPolling(true), connected: () => setPolling(false), cursor: (sequence) => { eventCursorRef.current.sequence = Math.max(eventCursorRef.current.sequence, sequence); },
    });
  }, [notebook?.sessionId, refresh, fetchTurn, fetchExecution]);

  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(() => {
      void refresh();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [polling, refresh, fetchTurn, fetchExecution]);

  // Which cells are worth offering a Tune button on. §3.5 gates it on the scan
  // having actually found knobs, not merely on there being a picture: a button
  // that opens onto "nothing here can be tuned" is worse than no button.
  // `POST /tuning/open` is pure analysis and starts no kernel, so this costs one
  // request per plot cell, cached against a fingerprint of the source at and
  // above that cell — the answer changes only when that source does, so running
  // cells and bumping the revision do not re-ask.
  const [tunableCellIds, setTunableCellIds] = useState<ReadonlySet<string>>(() => new Set<string>());
  const tuningProbeRef = useRef(new Map<string, boolean>());
  useEffect(() => {
    const candidates = (notebook?.cells ?? []).filter((cell) => cell.cellType === "code" && hasImageOutput(cell.outputs));
    if (!notebook || !candidates.length) { setTunableCellIds((current) => current.size ? new Set<string>() : current); return; }
    const cache = tuningProbeRef.current;
    // One entry per distinct source state; the cap stops a long editing session
    // from growing the map without bound.
    if (cache.size > 500) cache.clear();
    const keys = new Map<string, string>();
    let hash = 5381;
    let length = 0;
    // Rolling, and stopping at the last plot cell: this runs on every notebook
    // refresh, and a run-all fires a lot of them, so it must not walk the whole
    // source of a large notebook each time.
    const last = candidates[candidates.length - 1].index;
    for (const cell of notebook.cells) {
      for (let index = 0; index < cell.source.length; index += 1) hash = ((hash * 33) ^ cell.source.charCodeAt(index)) >>> 0;
      length += cell.source.length + 1;
      keys.set(cell.cellId, `${notebook.sessionId}:${cell.cellId}:${length}:${hash.toString(36)}`);
      if (cell.index >= last) break;
    }
    let live = true;
    const settle = () => {
      if (!live) return;
      const next = new Set(candidates.filter((cell) => cache.get(keys.get(cell.cellId) ?? "")).map((cell) => cell.cellId));
      setTunableCellIds((current) => current.size === next.size && [...next].every((id) => current.has(id)) ? current : next);
    };
    settle();
    void (async () => {
      for (const cell of candidates) {
        const key = keys.get(cell.cellId) as string;
        if (cache.has(key)) continue;
        try {
          const plan = await api.openTuning(notebook, cell.cellId);
          cache.set(key, plan.knobs.length > 0);
        } catch (error) {
          // A stale-revision conflict says nothing about this cell — a newer
          // snapshot is already on its way and will ask again. Caching it would
          // hide the button for that source forever. Any other refusal (a
          // markdown target, an unparsable chain) really is "no knobs", and
          // none of this is worth a notice: the user did not ask for it.
          if (error instanceof ApiError && error.isConflict) return;
          cache.set(key, false);
        }
        if (!live) return;
        settle();
      }
    })();
    return () => { live = false; };
  }, [notebook]);

  function showError(error: unknown) {
    const text = error instanceof Error ? error.message : "The operation could not be completed";
    setNotice({ tone: "error", text });
  }

  async function mutate<T>(work: () => Promise<T>, options: { conflictText?: string; refreshAfter?: boolean; onConflict?: () => void } = {}, commit?: (result: T) => void) {
    const mutationGeneration = ++mutationGenerationRef.current;
    resourceEpochRef.current += 1;
    setBusy(true); setNotice(null);
    try {
      const result = await work();
      if (mutationGenerationRef.current !== mutationGeneration) return null;
      resourceEpochRef.current += 1;
      commit?.(result);
      if (options.refreshAfter !== false) await refresh();
      return result;
    } catch (error) {
      if (mutationGenerationRef.current !== mutationGeneration) return null;
      resourceEpochRef.current += 1;
      if (error instanceof ApiError && error.isConflict) {
        await refresh();
        options.onConflict?.();
        setNotice({ tone: "warning", text: options.conflictText ?? "Notebook changed elsewhere. The latest revision has been loaded." });
      } else showError(error);
      return null;
    } finally { if (mutationGenerationRef.current === mutationGeneration) setBusy(false); }
  }

  const handleOpen = async (path: string, workspaceRoot?: string) => {
    const current = snapshotRef.current;
    const opened = await mutate(() => api.open(path, current ?? undefined, workspaceRoot), { refreshAfter: false }, (next) => {
      setNotebook(next); setScope(emptyScope); setTurn(null); setHistory([]); setSelectedTurnId(null); setOperation(null); setKernel(emptyKernel); setDirtyCellIds(new Set());
      setWorkspaceFolder(next.workspaceRoot ?? workspaceRoot ?? null);
    });
    if (opened) await mutate(() => api.kernel(), { refreshAfter: false }, setKernel);
  };
  const handleOpenFolder = (path: string) => { setWorkspaceFolder(path); setSidebarHidden(false); };
  const handleSaveAs = (path: string) => { if (notebook) void mutate(() => api.saveAs(notebook, path, workspaceFolder ?? undefined), { refreshAfter: false }, (saved) => { setNotebook(saved); setWorkspaceFolder(saved.workspaceRoot ?? workspaceFolder); }); };
  const handleSave = () => {
    if (!notebook?.notebookPath || !notebook.dirty || busy || dirtyCellIds.size > 0) return;
    const active = history.find((item) => !terminalTurns.has(item.turn.state)) ?? (turn && !terminalTurns.has(turn.state));
    if (active || (operation && !terminalExecutions.has(operation.state))) return;
    void mutate(() => api.save(notebook), { refreshAfter: false }, setNotebook);
  };
  const saveShortcutRef = useRef(handleSave);
  saveShortcutRef.current = handleSave;
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (!((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s")) return;
      event.preventDefault();
      saveShortcutRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const closeNotebook = (target: { sessionId: string; revision: number }) => {
    setCloseTarget(null);
    void mutate(() => api.close(target), { refreshAfter: false }, (result) => {
      const current = snapshotRef.current;
      if (!current || current.sessionId !== target.sessionId || current.revision !== target.revision) return;
      snapshotRef.current = null;
      setNotebook(null);
      setScope(emptyScope);
      setKernel(emptyKernel);
      setTurn(null);
      setHistory([]);
      setSelectedTurnId(null);
      setOperation(null);
      setDirtyCellIds(new Set());
      setFocusRequest(null);
      setPolling(false);
      eventCursorRef.current = { sessionId: "", sequence: 0 };
      turnGenerationRef.current.clear();
      executionGenerationRef.current.clear();
      if (result.cleanupErrors.length > 0) setNotice({ tone: "warning", text: `Notebook closed, but cleanup was incomplete: ${result.cleanupErrors.join("; ")}` });
    });
  };
  const handleClose = () => {
    if (!notebook) return;
    const target = { sessionId: notebook.sessionId, revision: notebook.revision };
    if (notebook.dirty) setCloseTarget(target);
    else closeNotebook(target);
  };
  const confirmClose = () => {
    if (!closeTarget || !notebook || closeTarget.sessionId !== notebook.sessionId || closeTarget.revision !== notebook.revision) {
      setCloseTarget(null);
      return;
    }
    closeNotebook(closeTarget);
  };
  const requestCellFocus = (cellId: string) => setFocusRequest((current) => ({ cellId, requestId: (current?.requestId ?? 0) + 1 }));

  // Accept changes no document state, so it neither refreshes the notebook nor
  // advances the revision — it only settles the ledger.
  const acceptOperations = (notebook: NotebookSnapshot, turnId: string, operationIds?: string[]) =>
    void mutate(() => api.acceptOperations(notebook, turnId, operationIds), { refreshAfter: false }, (updated) => {
      // Reconcile before committing. The raw response still carries every
      // change the turn made, so storing it verbatim leaves a fully-reviewed
      // cell showing its header and — because no pending hunks remain for the
      // ledger overlay — falling back to the legacy whole-change diff.
      const settled = reconcileTurnChanges(updated, snapshotRef.current);
      setHistory((items) => updateTurnRecord(items, turnId, () => settled));
      setTurn((item) => item?.turnId === turnId ? settled : item);
    });
  const rejectOperations = (notebook: NotebookSnapshot, turnId: string, operationId?: string) =>
    void mutate(() => api.rejectOperations(notebook, turnId, operationId), {
      conflictText: "This cell changed after the agent edited it, so that change can no longer be undone individually.",
    }, setNotebook);

  // Plot tuning. Scan, warm and preview are deliberately *not* routed through
  // `mutate`: none of them touches the document, and `mutate`'s global `busy`
  // would disable the very knobs being dragged. Apply is a document mutation
  // like any other and goes through it.
  const applyTuning = async (notebook: NotebookSnapshot, shadowId: string, values: Record<string, TuningValue>) => {
    const record = await mutate(() => api.applyTuning(notebook, shadowId, values), {
      conflictText: "The notebook changed while the tuning panel was open, so nothing was applied. Re-open Tune against the latest revision.",
    }, (applied) => setTuningRecord(applied));
    // `mutate` reports the failure and hands back null. The panel is still
    // sitting in "applying" waiting on this promise, so turn that null back
    // into a rejection rather than letting it render a record that never was.
    if (!record) throw new Error("These values were not applied — see the message at the top of the window.");
    return record;
  };
  const keepTunedOperations = (notebook: NotebookSnapshot, recordId: string, operationIds: string[]) =>
    void mutate(() => api.acceptTuningOperations(notebook, recordId, operationIds), { refreshAfter: false },
      (updated) => setTuningRecord(reconcileTuningChanges(updated, snapshotRef.current)));
  const undoTunedOperations = (notebook: NotebookSnapshot, recordId: string, operationIds: string[]) =>
    void mutate(() => api.rejectTuningOperations(notebook, recordId, operationIds), {
      conflictText: "This cell changed after you tuned it, so those values can no longer be undone individually.",
    }, (updated) => setTuningRecord(reconcileTuningChanges(updated, snapshotRef.current)));

  // Selection → "Add to agent chat": the containing cell becomes the editable
  // boundary and the selection is attached to the chat as a reference chip
  // (Cursor-style — cell/line label, not raw code). The code is sent to the
  // agent as context only when the turn is submitted.
  const addSelectionToChat = (notebook: NotebookSnapshot, selection: CellSelection) => {
    const cell = notebook.cells.find((item) => item.cellId === selection.cellId);
    if (!cell) return;
    void mutate(() => api.addScope(notebook, selection.cellId, true), { refreshAfter: false }, setScope);
    const attachment = makeAttachment(selection, cell.index, cell.cellType);
    setAttachments((current) => [...current.filter((item) => item.id !== attachment.id), attachment]);
  };

  // Selection → "Add to chat" from a cell's error output: attach the selected
  // traceback text as a reference chip and scope the erroring cell as editable.
  const addErrorToChat = (notebook: NotebookSnapshot, cellId: string, errorText: string) => {
    const cell = notebook.cells.find((item) => item.cellId === cellId);
    if (!cell) return;
    void mutate(() => api.addScope(notebook, cellId, true), { refreshAfter: false }, setScope);
    const attachment = makeErrorAttachment(cellId, errorText, cell.index, cell.cellType);
    setAttachments((current) => [...current.filter((item) => item.id !== attachment.id), attachment]);
  };

  // Selection right-click → "Edit inline": scope the containing cell as editable
  // and immediately start a turn focused on the selected region. The enforced
  // edit boundary stays the whole cell; the selection only focuses the agent.
  const inlineEditSelection = (notebook: NotebookSnapshot, selection: CellSelection, instruction: string) => {
    const frozen: TurnScope = {
      ...scope,
      editableCellIds: Array.from(new Set([...scope.editableCellIds, selection.cellId])),
      contextCellIds: scope.contextCellIds.filter((id) => id !== selection.cellId),
    };
    void mutate(async () => {
      await api.addScope(notebook, selection.cellId, true);
      return api.startTurn(notebook, composeInlineEditPrompt(instruction, selection));
    }, { refreshAfter: false }, (result) => {
      setScope(frozen);
      setTurn(result);
      setSelectedTurnId(result.turnId);
      setHistory((items) => upsertRecord(items, result, frozen, instruction.trim() || "Inline edit"));
    });
  };

  if (loading) return <div className="loading-screen"><span className="spinner" />Loading notebook…</div>;

  const openPicker = picking ? <FilePicker mode="open" onOpenNotebook={(path, root) => { setPicking(false); void handleOpen(path, root); }} onOpenFolder={(path) => { setPicking(false); handleOpenFolder(path); }} onClose={() => setPicking(false)} /> : null;

  if (!notebook && !workspaceFolder) return <div className="app-shell empty-shell">
    <header className="topbar"><div className="brand"><Icon name="menu_book" /><strong>Notebook Agent</strong></div><div className="toolbar-actions"><ThemeToggle /><FileToolbar notebook={null} onBrowse={() => setPicking(true)} onSave={handleSave} onSaveAs={() => setSaving(true)} onClose={handleClose} /></div></header>
    <main className="upload-state"><Icon name="menu_book" /><h1>Open a notebook or a folder to begin</h1><p>Open a local <code>.ipynb</code> file, or a project folder to browse and edit its notebooks in place.</p><button className="primary" onClick={() => setPicking(true)}>Open…</button></main>
    {notice && <Notice notice={notice} onClose={() => setNotice(null)} />}
    {openPicker}
  </div>;

  const activeTurn = history.find((item) => !terminalTurns.has(item.turn.state))?.turn ?? (turn && !terminalTurns.has(turn.state) ? turn : null);
  const activeExecution = operation && !terminalExecutions.has(operation.state) ? operation : null;
  const mutationsDisabled = Boolean(activeTurn || activeExecution);
  const hasDirtyDrafts = dirtyCellIds.size > 0;
  const selectedTurn = history.find((item) => item.turn.turnId === selectedTurnId)?.turn ?? turn;

  const fileLocked = mutationsDisabled || busy || hasDirtyDrafts;
  // Same test the agent panel uses to raise the approval dialog, so the toolbar
  // and the dialog can never disagree about whether a decision is outstanding.
  const awaitingApproval = Boolean(operation?.attempts.some((attempt) => attempt.state === "awaiting_approval" && !attempt.decision));
  // Which record the review bar is driving. A tuning Apply produces the same
  // per-hunk ledger an agent turn does, so it gets the same navigation rather
  // than a second bespoke surface — without it a multi-cell tune lands with no
  // way to find what moved. The tune wins when both are live: it is the more
  // recent decision, and it is the user's own edit.
  const tunedUnsettledOps = (tuningRecord?.operations ?? []).filter(
    (item) => item.state === "pending" || item.state === "stale");
  const reviewingTune = tunedUnsettledOps.length > 0;
  const reviewOrigin: "agent" | "tune" = reviewingTune ? "tune" : "agent";
  const reviewOperations = reviewingTune
    ? (tuningRecord?.operations ?? [])
    : (selectedTurn?.operations ?? []);
  // "Unsettled" must mean the same thing everywhere: here, in
  // reconcileTurnChanges, and in Next-change navigation. Stale counts as
  // unsettled — the user has not decided about it — and stays settleable,
  // because Keep needs no composition guard even when Undo can no longer apply.
  const reviewUnsettled = reviewOperations.filter((item) => item.state === "pending" || item.state === "stale");
  const reviewKept = reviewOperations.filter((item) => item.state === "accepted").length;
  // Step through the cells that still have something unreviewed, in either
  // direction, wrapping at both ends. Reuses the existing chat-to-cell focus
  // plumbing rather than adding a second way to scroll the notebook. Stale
  // cells are included: landing on one is how the user finds out why it can no
  // longer be undone.
  // The counter says "go to the first change", so it must not simply advance
  // from wherever the cursor happens to be — after stepping to change 3, "first"
  // would land on 4.
  const focusFirstChange = () => {
    if (!notebook || !reviewUnsettled.length) return;
    const first = notebook.cells.map((cell) => cell.cellId)
      .find((cellId) => reviewUnsettled.some((item) => item.cellId === cellId));
    if (first) requestCellFocus(first);
  };
  const focusChange = (direction: 1 | -1) => {
    if (!notebook || !reviewUnsettled.length) return;
    const order = notebook.cells.map((cell) => cell.cellId).filter((cellId) => reviewUnsettled.some((item) => item.cellId === cellId));
    if (!order.length) return;
    const from = order.indexOf(focusRequest?.cellId ?? "");
    // Nothing focused yet: forward starts at the first change, back at the last,
    // so the first press lands somewhere useful either way.
    const next = from < 0
      ? (direction === 1 ? 0 : order.length - 1)
      : (from + direction + order.length) % order.length;
    requestCellFocus(order[next]);
  };
  return <div className="app-shell">
    <header className="topbar">
      <div className="brand">{(workspaceFolder || notebook) && sidebarHidden && <button className="sidebar-reveal" title="Show sidebar" aria-label="Show sidebar" onClick={() => setSidebarHidden(false)}><Icon name="keyboard_tab" /></button>}<Icon name="menu_book" /><strong>{notebook?.filename ?? "Workspace"}</strong>{notebook && <span className={notebook.dirty ? "dirty" : ""}>{notebook.dirty ? "Unsaved" : "Clean"}</span>}{notebook && <span>Revision {notebook.revision}</span>}</div>
      <div className="toolbar-actions"><ThemeToggle />{notebook && <button className={`autosave-toggle ${autoSave ? "on" : ""}`} role="switch" aria-checked={autoSave} aria-label="Auto-save" title={autoSave ? "Auto-save is on — click to turn off" : "Auto-save is off — click to turn on"} onClick={() => setAutoSave((value) => !value)}><Icon name="save" /> Auto-save {autoSave ? "on" : "off"}</button>}{notebook && <KernelControls status={kernel} mutationDisabled={fileLocked} runAwaitingApproval={awaitingApproval} onRunAll={() => void mutate(() => api.runAll(notebook), { refreshAfter: false }, setOperation)} onInterrupt={() => void mutate(() => api.interrupt(notebook, kernel))} onRestart={() => void mutate(() => api.restart(notebook, kernel))} />}<FileToolbar notebook={notebook} saveDisabled={fileLocked || !notebook?.notebookPath || !notebook?.dirty} saveAsDisabled={fileLocked} closeDisabled={fileLocked} onBrowse={() => setPicking(true)} onSave={handleSave} onSaveAs={() => setSaving(true)} onClose={handleClose} /></div>
    </header>
    {notice && <Notice notice={notice} onClose={() => setNotice(null)} />}
    {openPicker}
    {saving && notebook && <FilePicker mode="save" defaultName={notebook.filename} initialPath={notebook.workspaceRoot ?? workspaceFolder ?? undefined} onSaveAs={(path) => { setSaving(false); handleSaveAs(path); }} onClose={() => setSaving(false)} />}
    {closeTarget && <CloseNotebookDialog busy={busy} onCancel={() => setCloseTarget(null)} onConfirm={confirmClose} />}
    <div className="workspace-layout">
      {(workspaceFolder || notebook) && !sidebarHidden && <WorkspaceSidebar
        root={workspaceFolder}
        activePath={notebook?.notebookPath ?? null}
        notebook={notebook}
        tab={railTab}
        onTabChange={(tab) => { setRailTab(tab); writeRailTab(notebook?.notebookPath, tab); }}
        onOpenNotebook={(path) => { if (workspaceFolder) void handleOpen(path, workspaceFolder); }}
        onCollapse={() => setSidebarHidden(true)}
        onJumpToCell={requestCellFocus}
        onHoverBlock={(cellIds) => setOutlineHighlight(cellIds ? new Set(cellIds) : null)} />}
      {notebook ? <div className="editor-layout" style={{ "--agent-width": `${agentWidth}px` } as CSSProperties}>
      <div className="notebook-pane">
      {/* Gated on unsettled work, so finishing a review clears the bar rather
          than leaving a live "Reject All" behind a "0 Pending Reviews" counter.
          Keyed by turn so no confirmation survives a switch to another turn.
          Trusted turns carry no ledger, so this never shows for them. */}
      {(reviewingTune ? Boolean(tuningRecord) : Boolean(selectedTurn)) && reviewUnsettled.length > 0 && <ReviewBar
        key={reviewingTune ? tuningRecord!.recordId : selectedTurn!.turnId}
        origin={reviewOrigin}
        // What the turn was asked to do. The history record's prompt is the
        // label the composer submitted under (it covers an attachment-only
        // turn, which has no typed prompt); the turn's own is the fallback for
        // a turn recovered from the backend.
        taskLabel={reviewingTune ? undefined : (history.find((item) => item.turn.turnId === selectedTurn!.turnId)?.prompt ?? selectedTurn!.prompt)}
        total={reviewOperations.length} reviewed={reviewOperations.length - reviewUnsettled.length} keptCount={reviewKept}
        undoableCount={reviewUnsettled.filter((item) => item.state === "pending").length}
        disabled={mutationsDisabled || busy || hasDirtyDrafts}
        onPrevious={() => focusChange(-1)}
        onNext={() => focusChange(1)}
        onFirst={() => focusFirstChange()}
        // Only what is still undecided: posting every id would include the ones
        // already rejected, and accept_operations conflicts on the first of
        // those, so Keep-all 409'd after any per-cell Undo.
        onKeepAll={() => reviewingTune
          ? keepTunedOperations(notebook, tuningRecord!.recordId, reviewUnsettled.map((item) => item.operationId))
          : acceptOperations(notebook, selectedTurn!.turnId)}
        onUndoAll={() => reviewingTune
          ? undoTunedOperations(notebook, tuningRecord!.recordId, reviewUnsettled.filter((item) => item.state === "pending").map((item) => item.operationId))
          : rejectOperations(notebook, selectedTurn!.turnId)} />}
      <NotebookView notebook={notebook} scope={scope} turn={selectedTurn} tuningRecord={tuningRecord} trusted={trustedScope} disabled={mutationsDisabled || busy} sourceActionsDisabled={hasDirtyDrafts} autoSave={autoSave} focusRequest={focusRequest}
        outlinedCellIds={outlineHighlight}
        tunableCellIds={tunableCellIds}
        tuningControls={{
          revision: notebook.revision,
          onScan: (cellId) => api.openTuning(notebook, cellId),
          onWarm: (cellId) => api.warmTuning(notebook, cellId),
          onPreview: (shadowId, values) => api.previewTuning(shadowId, values),
          onApply: (shadowId, values) => applyTuning(notebook, shadowId, values),
          // Best-effort teardown: the shadow may already be gone (idle timeout,
          // revision change), and that is the success case, not an error to
          // report to someone who has closed the panel.
          onDiscardShadow: (shadowId) => { void api.closeTuning(shadowId).catch(() => undefined); },
        }}
        onKeepTuned={(recordId, operationIds) => { if (operationIds.length) keepTunedOperations(notebook, recordId, operationIds); }}
        onUndoTuned={(recordId, operationIds) => { if (operationIds.length) undoTunedOperations(notebook, recordId, operationIds); }}
        onKeepCell={(turnId, cellId) => {
          // One request per cell, not per hunk: keeping a cell is a single
          // settle, and a mid-loop failure used to leave it half-kept.
          const ids = pendingOperations(selectedTurn, cellId).map((item) => item.operationId);
          if (ids.length) acceptOperations(notebook, turnId, ids);
        }}
        onKeepOperation={(turnId, operationId) => acceptOperations(notebook, turnId, [operationId])}
        onUndoOperation={(turnId, operationId) => rejectOperations(notebook, turnId, operationId)}
        onDirtyChange={(cellId, dirty) => setDirtyCellIds((current) => {
          if (current.has(cellId) === dirty) return current;
          const next = new Set(current); if (dirty) next.add(cellId); else next.delete(cellId); return next;
        })}
        onSave={(cellId, source) => void mutate(
          () => api.saveSource(notebook, cellId, source),
          {
            conflictText: "Notebook changed elsewhere. Your unsaved edit is still in the editor; review it against the latest revision before saving again.",
          },
          (saved) => setNotebook((current) => current && current.sessionId === saved.sessionId ? {
            ...current,
            revision: saved.revision,
            dirty: saved.dirty,
            cells: current.cells.map((cell) => cell.cellId === saved.cellId ? { ...cell, source: saved.source } : cell),
          } : current),
        )}
        onRun={(cellId) => void mutate(() => api.runCell(notebook, cellId), { refreshAfter: false }, setOperation)}
        onScope={(cellId, editable) => void mutate(() => api.addScope(notebook, cellId, editable), { refreshAfter: false }, setScope)}
        onScopeMany={(cellIds, editable) => { if (cellIds.length) void mutate(async () => { let latest: TurnScope | undefined; for (const cellId of cellIds) latest = await api.addScope(notebook, cellId, editable); return latest as TurnScope; }, { refreshAfter: false }, setScope); }}
        onAddSelectionToChat={(selection) => addSelectionToChat(notebook, selection)}
        onInlineEdit={(selection, instruction) => inlineEditSelection(notebook, selection, instruction)}
        onAddErrorToChat={(cellId, errorText) => addErrorToChat(notebook, cellId, errorText)}
        onRevert={(turnId, cellId) => void mutate(() => api.revertCell(notebook, turnId, cellId), {}, (updated) => {
          setNotebook(updated);
          setHistory((items) => updateTurnRecord(items, turnId, (item) => ({ ...item, changes: item.changes.filter((change) => change.cellId !== cellId) })));
          setTurn((item) => item?.turnId === turnId ? { ...item, changes: item.changes.filter((change) => change.cellId !== cellId) } : item);
        })} />
      </div>
      <div className="editor-resizer" role="separator" aria-orientation="vertical" aria-label="Resize agent panel" tabIndex={0} onPointerDown={startAgentResize} onKeyDown={nudgeAgentResize} />
      <AgentChatPanel notebook={notebook} scope={scope} turn={selectedTurn} activeTurn={activeTurn} history={history} operation={operation} busy={busy} mutationsDisabled={mutationsDisabled || hasDirtyDrafts}
        onClearScope={() => void mutate(() => api.clearScope(notebook), { refreshAfter: false }, setScope)}
        onRemoveScopeCell={(cellId) => void mutate(() => api.removeScope(notebook, cellId), { refreshAfter: false }, setScope)}
        onSubmit={(prompt, options) => { const frozen = { ...scope, editableCellIds: [...scope.editableCellIds], contextCellIds: [...scope.contextCellIds] }; const composed = composeChatPrompt(prompt, attachments); const label = prompt.trim() || (attachments.length ? defaultAttachmentInstruction(attachments) : "Agent turn"); void mutate(() => api.startTurn(notebook, composed, options), { refreshAfter: false }, (result) => { setTurn(result); setSelectedTurnId(result.turnId); setHistory((items) => upsertRecord(items, result, frozen, label)); setAttachments([]); }); }}
        attachments={attachments} onRemoveAttachment={(id) => setAttachments((current) => current.filter((item) => item.id !== id))}
        onCancel={() => activeTurn && void mutate(() => api.cancelTurn(notebook, activeTurn.turnId), { refreshAfter: false }, (result) => { setTurn(result); setHistory((items) => upsertRecord(items, result)); })}
        onUndo={() => selectedTurn && void mutate(() => api.undoTurn(notebook, selectedTurn.turnId), {}, (updated) => {
          setNotebook(updated);
          setHistory((items) => updateTurnRecord(items, selectedTurn.turnId, (item) => ({ ...item, undoEligible: false, changes: [] })));
          setTurn((item) => item?.turnId === selectedTurn.turnId ? { ...item, undoEligible: false, changes: [] } : item);
        })}
        onDecision={(attempt: ExecutionAttempt, decision) => operation && void mutate(() => api.decide(operation, attempt, decision), { refreshAfter: false }, setOperation)}
        writeScope={writeScope} onWriteScopeChange={setWriteScope}
        onSelectTurn={setSelectedTurnId} onFocusCell={requestCellFocus} onDropCell={(cellId) => void mutate(() => api.addScope(notebook, cellId, !trustedScope), { refreshAfter: false }, setScope)}
        onDropCells={(cellIds) => { if (cellIds.length) void mutate(async () => { let latest: TurnScope | undefined; for (const cellId of cellIds) latest = await api.addScope(notebook, cellId, !trustedScope); return latest as TurnScope; }, { refreshAfter: false }, setScope); }} />
      </div> : <main className="workspace-placeholder"><Icon name="menu_book" /><h2>Select a notebook</h2><p>Choose a <code>.ipynb</code> from the file tree to open it in place.</p></main>}
    </div>
  </div>;
}

function upsertRecord(items: TurnRecord[], turn: AgentTurn, scope?: TurnScope, prompt?: string): TurnRecord[] {
  const existing = items.find((item) => item.turn.turnId === turn.turnId);
  const record: TurnRecord = {
    turn,
    editableCellIds: existing?.editableCellIds ?? (scope ? [...scope.editableCellIds] : []),
    contextCellIds: existing?.contextCellIds ?? (scope ? [...scope.contextCellIds] : []),
    prompt: prompt ?? existing?.prompt ?? "Agent turn",
  };
  return [record, ...items.filter((item) => item.turn.turnId !== turn.turnId)].slice(0, 50);
}

function turnToRecord(turn: AgentTurn): TurnRecord {
  return { turn, editableCellIds: turn.editableCellIds, contextCellIds: turn.contextCellIds, prompt: turn.prompt };
}

function reconcileHistory(summaries: TurnRecord[], existing: TurnRecord[], notebook: NotebookSnapshot, truncated: boolean): TurnRecord[] {
  const reconciled = summaries.map((summary) => {
    const detail = existing.find((item) => item.turn.turnId === summary.turn.turnId);
    const turn = summary.turn.historyTruncated && detail && !detail.turn.historyTruncated
      ? {
        ...summary.turn,
        prompt: detail.turn.prompt,
        editableCellIds: detail.turn.editableCellIds,
        contextCellIds: detail.turn.contextCellIds,
        changes: detail.turn.changes,
      }
      : summary.turn;
    return {
      turn: reconcileTurnChanges(turn, notebook),
      editableCellIds: detail?.editableCellIds ?? summary.editableCellIds,
      contextCellIds: detail?.contextCellIds ?? summary.contextCellIds,
      prompt: detail && summary.turn.historyTruncated ? detail.prompt : summary.prompt,
    };
  });
  if (!truncated) return reconciled;
  const returnedIds = new Set(reconciled.map((item) => item.turn.turnId));
  const cachedTail = existing
    .filter((item) => item.turn.sessionId === notebook.sessionId && !returnedIds.has(item.turn.turnId))
    .map((item) => ({
      ...item,
      turn: reconcileTurnChanges({ ...item.turn, undoEligible: false, historyTruncated: true }, notebook),
    }));
  return [...reconciled, ...cachedTail].slice(0, 50);
}

// Which of a turn's changes still have something to review.
//
// Before the operation ledger this compared the cell's source to `nextSource`,
// which is only correct while a change is all-or-nothing: undo one hunk and the
// equality breaks, so the whole cell's diff — including the hunks nobody has
// looked at yet — would silently disappear.
//
// With a ledger, `nextSource` describes what the turn originally proposed, not
// what is in the cell, so it is history rather than a render source. A change
// stays visible while it still has unsettled operations. Accepting is therefore
// what clears a diff, which is the review gesture the UI previously lacked.
//
// Turns served before the ledger existed (or with operations dropped by summary
// truncation) fall back to the old equality so their diffs still resolve.
function reconcileTurnChanges(turn: AgentTurn, notebook: NotebookSnapshot | null): AgentTurn {
  if (!notebook) return turn;
  const operations = turn.operations;
  if (!operations?.length) {
    return { ...turn, changes: turn.changes.filter((change) => notebook.cells.find((cell) => cell.cellId === change.cellId)?.source === change.nextSource) };
  }
  const governed = new Set(operations.map((item) => item.cellId));
  const unsettled = new Set(operations.filter((item) => item.state !== "accepted" && item.state !== "rejected").map((item) => item.cellId));
  return {
    ...turn,
    changes: turn.changes.filter((change) =>
      // A cell the ledger does not govern keeps its change: on a Trusted turn,
      // cells caught up in a retype carry a diff for review but no operations,
      // and treating "no operations" as "fully reviewed" hid both their diff
      // and the note explaining that only whole-turn undo applies.
      (governed.has(change.cellId) ? unsettled.has(change.cellId) : true)
      && notebook.cells.some((cell) => cell.cellId === change.cellId)),
  };
}

// The tuning equivalent of reconcileTurnChanges: a record's diff stays on
// screen while the cell still has something unreviewed. Simpler than the agent
// version because every tuned change is governed by operations — a tune with no
// ledger cannot exist — so there is no pre-ledger fallback to keep working.
function reconcileTuningChanges(record: TuningRecord, notebook: NotebookSnapshot | null): TuningRecord {
  if (!notebook) return record;
  const unsettled = new Set(record.operations.filter((item) => item.state !== "accepted" && item.state !== "rejected").map((item) => item.cellId));
  return {
    ...record,
    changes: record.changes.filter((change) => unsettled.has(change.cellId) && notebook.cells.some((cell) => cell.cellId === change.cellId)),
  };
}

export function pendingOperations(turn: AgentTurn | null | undefined, cellId?: string): AgentOperation[] {
  return (turn?.operations ?? []).filter((item) => item.state === "pending" && (cellId === undefined || item.cellId === cellId));
}

function updateTurnRecord(items: TurnRecord[], turnId: string, update: (turn: AgentTurn) => AgentTurn): TurnRecord[] {
  return items.map((item) => item.turn.turnId === turnId ? { ...item, turn: update(item.turn) } : item);
}

function Notice({ notice, onClose }: { notice: { tone: "error" | "warning"; text: string }; onClose: () => void }) {
  return <div className={`notice ${notice.tone}`} role="alert"><Icon name="warning" /><span>{notice.text}</span><button aria-label="Dismiss message" onClick={onClose}><Icon name="close" /></button></div>;
}
