import { AlertTriangle, BookOpen, FileUp, Save, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type CSSProperties, type DragEvent, type KeyboardEvent, type PointerEvent } from "react";
import { extractNotebookFiles } from "./fileOperations/dropUpload";
import { ApiError, api, connectEvents, type AgentTurn, type ExecutionAttempt, type ExecutionOperation, type KernelStatus, type NotebookSnapshot, type TurnScope } from "./api/client";
import AgentChatPanel from "./agentChat/AgentChatPanel";
import type { TurnRecord } from "./agentChat/AgentChatPanel";
import KernelControls from "./execution/KernelControls";
import CloseNotebookDialog from "./fileOperations/CloseNotebookDialog";
import FileToolbar from "./fileOperations/FileToolbar";
import NotebookView from "./notebook/NotebookView";
import { composeChatPrompt, composeInlineEditPrompt, makeAttachment, makeErrorAttachment, type CellSelection, type SelectionAttachment } from "./notebook/selectionEdit";

const AGENT_MIN_WIDTH = 300;
const AGENT_MAX_WIDTH = 760;
const AGENT_WIDTH_KEY = "notebook-agent-width";
const AUTO_SAVE_KEY = "notebook-auto-save";
const clampAgentWidth = (value: number): number => Math.min(AGENT_MAX_WIDTH, Math.max(AGENT_MIN_WIDTH, Math.round(value)));

const emptyScope: TurnScope = { editableCellIds: [], contextCellIds: [], sessionId: null, notebookRevision: null };
const emptyKernel: KernelStatus = { kernelSessionId: null, state: "not_started", executionAttemptId: null };
const terminalTurns = new Set(["completed", "failed", "cancelled", "validation_incomplete", "timed_out"]);
const terminalExecutions = new Set(["completed", "failed", "cancelled", "validation_incomplete", "timed_out"]);

export default function App() {
  const [notebook, setNotebook] = useState<NotebookSnapshot | null>(null);
  const [scope, setScope] = useState<TurnScope>(emptyScope);
  const [kernel, setKernel] = useState<KernelStatus>(emptyKernel);
  const [turn, setTurn] = useState<AgentTurn | null>(null);
  const [history, setHistory] = useState<TurnRecord[]>([]);
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const [focusRequest, setFocusRequest] = useState<{ cellId: string; requestId: number } | null>(null);
  const [attachments, setAttachments] = useState<SelectionAttachment[]>([]);
  const [fileDragging, setFileDragging] = useState(false);
  const dragDepth = useRef(0);
  const [operation, setOperation] = useState<ExecutionOperation | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "error" | "warning"; text: string } | null>(null);
  const [dirtyCellIds, setDirtyCellIds] = useState<Set<string>>(() => new Set());
  const [polling, setPolling] = useState(false);
  const [closeTarget, setCloseTarget] = useState<{ sessionId: string; revision: number } | null>(null);
  const [agentWidth, setAgentWidth] = useState<number>(() => {
    const saved = Number(localStorage.getItem(AGENT_WIDTH_KEY));
    return Number.isFinite(saved) && saved > 0 ? clampAgentWidth(saved) : 360;
  });
  useEffect(() => { localStorage.setItem(AGENT_WIDTH_KEY, String(agentWidth)); }, [agentWidth]);
  const [autoSave, setAutoSave] = useState<boolean>(() => localStorage.getItem(AUTO_SAVE_KEY) === "on");
  useEffect(() => { localStorage.setItem(AUTO_SAVE_KEY, autoSave ? "on" : "off"); }, [autoSave]);
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
  useEffect(() => { setDirtyCellIds(new Set()); setAttachments([]); }, [notebook?.sessionId]);
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

  const handleUpload = async (file: File) => {
    const current = snapshotRef.current;
    const uploaded = await mutate(() => api.upload(file, current ?? undefined), { refreshAfter: false }, (next) => {
      setNotebook(next); setScope(emptyScope); setTurn(null); setHistory([]); setSelectedTurnId(null); setOperation(null); setKernel(emptyKernel); setDirtyCellIds(new Set());
    });
    if (uploaded) await mutate(() => api.kernel(), { refreshAfter: false }, setKernel);
    return Boolean(uploaded);
  };

  const handleDownload = async () => {
    setNotice(null);
    let url: string | null = null;
    try {
      const blob = await api.download();
      url = URL.createObjectURL(blob);
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = notebook?.filename ?? "notebook.ipynb"; anchor.click();
    } catch (error) { showError(error); }
    finally { if (url) URL.revokeObjectURL(url); }
  };
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

  const handleDrop = async (dataTransfer: DataTransfer) => {
    let files: File[];
    try { files = await extractNotebookFiles(dataTransfer); }
    catch { setNotice({ tone: "error", text: "Could not read the dropped item." }); return; }
    if (!files.length) { setNotice({ tone: "warning", text: "No .ipynb notebook was found in what you dropped." }); return; }
    const opened = await handleUpload(files[0]);
    if (opened && files.length > 1) setNotice({ tone: "warning", text: `Opened ${files[0].name}; ${files.length - 1} other notebook${files.length > 2 ? "s were" : " was"} ignored.` });
  };
  // Drag-and-drop upload (files or folders). Ignores cell drags (no "Files"
  // type) so it never interferes with dragging a cell into the agent panel.
  const dropZone = (accept: boolean) => ({
    onDragEnter: (event: DragEvent) => { if (!event.dataTransfer.types.includes("Files")) return; event.preventDefault(); if (!accept) return; dragDepth.current += 1; setFileDragging(true); },
    onDragOver: (event: DragEvent) => { if (!event.dataTransfer.types.includes("Files")) return; event.preventDefault(); event.dataTransfer.dropEffect = accept ? "copy" : "none"; },
    onDragLeave: (event: DragEvent) => { if (!event.dataTransfer.types.includes("Files")) return; dragDepth.current = Math.max(0, dragDepth.current - 1); if (dragDepth.current === 0) setFileDragging(false); },
    onDrop: (event: DragEvent) => { if (!event.dataTransfer.types.includes("Files")) return; event.preventDefault(); dragDepth.current = 0; setFileDragging(false); if (accept) void handleDrop(event.dataTransfer); },
  });
  const dropOverlay = fileDragging ? <div className="drop-overlay" aria-hidden="true"><div><FileUp /><p>Drop a notebook (.ipynb) file or folder to open</p></div></div> : null;

  if (loading) return <div className="loading-screen"><span className="spinner" />Loading notebook…</div>;

  if (!notebook) return <div className="app-shell empty-shell" {...dropZone(!busy)}>
    <header className="topbar"><div className="brand"><BookOpen /><strong>Notebook Agent</strong></div><FileToolbar notebook={null} onUpload={handleUpload} onDownload={() => void handleDownload()} onClose={handleClose} /></header>
    <main className="upload-state"><BookOpen /><h1>Open a notebook to begin</h1><p>Upload a local <code>.ipynb</code> file, or drag one (or a folder) onto the window. Edits remain in this editor until downloaded.</p><label className="upload-button">Choose notebook<input type="file" accept=".ipynb,application/x-ipynb+json" onChange={(event) => { const file = event.target.files?.[0]; if (file) void handleUpload(file); }} /></label></main>
    {notice && <Notice notice={notice} onClose={() => setNotice(null)} />}
    {dropOverlay}
  </div>;

  const activeTurn = history.find((item) => !terminalTurns.has(item.turn.state))?.turn ?? (turn && !terminalTurns.has(turn.state) ? turn : null);
  const activeExecution = operation && !terminalExecutions.has(operation.state) ? operation : null;
  const mutationsDisabled = Boolean(activeTurn || activeExecution);
  const hasDirtyDrafts = dirtyCellIds.size > 0;
  const selectedTurn = history.find((item) => item.turn.turnId === selectedTurnId)?.turn ?? turn;

  return <div className="app-shell" {...dropZone(!(mutationsDisabled || busy || hasDirtyDrafts))}>
    {dropOverlay}
    <header className="topbar">
      <div className="brand"><BookOpen /><strong>{notebook.filename}</strong><span className={notebook.dirty ? "dirty" : ""}>{notebook.dirty ? "Unsaved" : "Clean"}</span><span>Revision {notebook.revision}</span></div>
      <div className="toolbar-actions"><button className={`autosave-toggle ${autoSave ? "on" : ""}`} role="switch" aria-checked={autoSave} aria-label="Auto-save" title={autoSave ? "Auto-save is on — click to turn off" : "Auto-save is off — click to turn on"} onClick={() => setAutoSave((value) => !value)}><Save /> Auto-save {autoSave ? "on" : "off"}</button><KernelControls status={kernel} mutationDisabled={mutationsDisabled || busy || hasDirtyDrafts} onRunAll={() => void mutate(() => api.runAll(notebook), { refreshAfter: false }, setOperation)} onInterrupt={() => void mutate(() => api.interrupt(notebook, kernel))} onRestart={() => void mutate(() => api.restart(notebook, kernel))} /><FileToolbar notebook={notebook} uploadDisabled={mutationsDisabled || busy || hasDirtyDrafts} closeDisabled={mutationsDisabled || busy || hasDirtyDrafts} onUpload={handleUpload} onDownload={() => void handleDownload()} onClose={handleClose} /></div>
    </header>
    {notice && <Notice notice={notice} onClose={() => setNotice(null)} />}
    {closeTarget && <CloseNotebookDialog busy={busy} onCancel={() => setCloseTarget(null)} onConfirm={confirmClose} />}
    <div className="editor-layout" style={{ "--agent-width": `${agentWidth}px` } as CSSProperties}>
      <NotebookView notebook={notebook} scope={scope} turn={selectedTurn} disabled={mutationsDisabled || busy} sourceActionsDisabled={hasDirtyDrafts} autoSave={autoSave} focusRequest={focusRequest}
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
      <div className="editor-resizer" role="separator" aria-orientation="vertical" aria-label="Resize agent panel" tabIndex={0} onPointerDown={startAgentResize} onKeyDown={nudgeAgentResize} />
      <AgentChatPanel notebook={notebook} scope={scope} turn={selectedTurn} activeTurn={activeTurn} history={history} operation={operation} busy={busy} mutationsDisabled={mutationsDisabled || hasDirtyDrafts}
        onClearScope={() => void mutate(() => api.clearScope(notebook), { refreshAfter: false }, setScope)}
        onRemoveScopeCell={(cellId) => void mutate(() => api.removeScope(notebook, cellId), { refreshAfter: false }, setScope)}
        onSubmit={(prompt, options) => { const frozen = { ...scope, editableCellIds: [...scope.editableCellIds], contextCellIds: [...scope.contextCellIds] }; const composed = composeChatPrompt(prompt, attachments); void mutate(() => api.startTurn(notebook, composed, options), { refreshAfter: false }, (result) => { setTurn(result); setSelectedTurnId(result.turnId); setHistory((items) => upsertRecord(items, result, frozen, prompt)); setAttachments([]); }); }}
        attachments={attachments} onRemoveAttachment={(id) => setAttachments((current) => current.filter((item) => item.id !== id))}
        onCancel={() => activeTurn && void mutate(() => api.cancelTurn(notebook, activeTurn.turnId), { refreshAfter: false }, (result) => { setTurn(result); setHistory((items) => upsertRecord(items, result)); })}
        onUndo={() => selectedTurn && void mutate(() => api.undoTurn(notebook, selectedTurn.turnId), {}, (updated) => {
          setNotebook(updated);
          setHistory((items) => updateTurnRecord(items, selectedTurn.turnId, (item) => ({ ...item, undoEligible: false, changes: [] })));
          setTurn((item) => item?.turnId === selectedTurn.turnId ? { ...item, undoEligible: false, changes: [] } : item);
        })}
        onDecision={(attempt: ExecutionAttempt, decision) => operation && void mutate(() => api.decide(operation, attempt, decision), { refreshAfter: false }, setOperation)}
        onSelectTurn={setSelectedTurnId} onFocusCell={requestCellFocus} onDropCell={(cellId) => void mutate(() => api.addScope(notebook, cellId, true), { refreshAfter: false }, setScope)} />
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

function reconcileTurnChanges(turn: AgentTurn, notebook: NotebookSnapshot | null): AgentTurn {
  if (!notebook) return turn;
  return {
    ...turn,
    changes: turn.changes.filter((change) => notebook.cells.find((cell) => cell.cellId === change.cellId)?.source === change.nextSource),
  };
}

function updateTurnRecord(items: TurnRecord[], turnId: string, update: (turn: AgentTurn) => AgentTurn): TurnRecord[] {
  return items.map((item) => item.turn.turnId === turnId ? { ...item, turn: update(item.turn) } : item);
}

function Notice({ notice, onClose }: { notice: { tone: "error" | "warning"; text: string }; onClose: () => void }) {
  return <div className={`notice ${notice.tone}`} role="alert"><AlertTriangle /><span>{notice.text}</span><button aria-label="Dismiss message" onClick={onClose}><X /></button></div>;
}
