import { AlertTriangle, BookOpen, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, connectEvents, type AgentTurn, type ExecutionAttempt, type ExecutionOperation, type KernelStatus, type NotebookSnapshot, type TurnScope } from "./api/client";
import AgentChatPanel from "./agentChat/AgentChatPanel";
import type { TurnRecord } from "./agentChat/AgentChatPanel";
import KernelControls from "./execution/KernelControls";
import FileToolbar from "./fileOperations/FileToolbar";
import NotebookView from "./notebook/NotebookView";

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
  const [operation, setOperation] = useState<ExecutionOperation | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "error" | "warning"; text: string } | null>(null);
  const [draftResetRequest, setDraftResetRequest] = useState<{ cellId: string; generation: number } | null>(null);
  const [polling, setPolling] = useState(false);
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
      setNotebook(nextNotebook);
      setScope(nextScope);
      setKernel(nextKernel);
      const hydrated = (status.turnHistory ?? []).map(turnToRecord).slice(0, 50);
      setHistory((items) => reconcileHistory(hydrated, items, nextNotebook));
      setTurn(status.activeTurn ? reconcileTurnChanges(status.activeTurn, nextNotebook) : hydrated[0]?.turn ?? null);
      setSelectedTurnId((selected) => hydrated.some((item) => item.turn.turnId === selected) ? selected : hydrated[0]?.turn.turnId ?? null);
      setOperation(status.activeExecution);
      return nextNotebook;
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) { setNotebook(null); setScope(emptyScope); return null; }
      throw error;
    }
  }, []);

  useEffect(() => { refresh().catch((error) => showError(error)).finally(() => setLoading(false)); }, [refresh]);

  const fetchTurn = useCallback(async (id: string) => {
    const epoch = ++resourceEpochRef.current;
    const generation = (turnGenerationRef.current.get(id) ?? 0) + 1;
    turnGenerationRef.current.set(id, generation);
    try {
      const next = await api.turn(id);
      if (turnGenerationRef.current.get(id) !== generation || resourceEpochRef.current !== epoch) return;
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
      if (terminalTurns.has(next.state)) await refresh();
    } catch (error) { showError(error); }
  }, [refresh]);

  useEffect(() => {
    const selected = history.find((item) => item.turn.turnId === selectedTurnId)?.turn;
    if (selected?.historyTruncated) void fetchTurn(selected.turnId);
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
      else setKernel(await api.kernel());
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
    await mutate(async () => { const uploaded = await api.upload(file, current ?? undefined); return { uploaded, kernel: await api.kernel() }; }, { refreshAfter: false }, ({ uploaded, kernel }) => { setNotebook(uploaded); setScope(emptyScope); setTurn(null); setHistory([]); setSelectedTurnId(null); setOperation(null); setKernel(kernel); });
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
  const requestCellFocus = (cellId: string) => setFocusRequest((current) => ({ cellId, requestId: (current?.requestId ?? 0) + 1 }));

  if (loading) return <div className="loading-screen"><span className="spinner" />Loading notebook…</div>;

  if (!notebook) return <div className="app-shell empty-shell">
    <header className="topbar"><div className="brand"><BookOpen /><strong>Notebook Agent</strong></div><FileToolbar notebook={null} onUpload={handleUpload} onDownload={() => void handleDownload()} /></header>
    <main className="upload-state"><BookOpen /><h1>Open a notebook to begin</h1><p>Upload a local <code>.ipynb</code> file. Edits remain in this editor until downloaded.</p><label className="upload-button">Choose notebook<input type="file" accept=".ipynb,application/x-ipynb+json" onChange={(event) => { const file = event.target.files?.[0]; if (file) void handleUpload(file); }} /></label></main>
    {notice && <Notice notice={notice} onClose={() => setNotice(null)} />}
  </div>;

  const activeTurn = history.find((item) => !terminalTurns.has(item.turn.state))?.turn ?? (turn && !terminalTurns.has(turn.state) ? turn : null);
  const activeExecution = operation && !terminalExecutions.has(operation.state) ? operation : null;
  const mutationsDisabled = Boolean(activeTurn || activeExecution);
  const selectedTurn = history.find((item) => item.turn.turnId === selectedTurnId)?.turn ?? turn;

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand"><BookOpen /><strong>{notebook.filename}</strong><span className={notebook.dirty ? "dirty" : ""}>{notebook.dirty ? "Unsaved" : "Clean"}</span><span>Revision {notebook.revision}</span></div>
      <div className="toolbar-actions"><KernelControls status={kernel} mutationDisabled={mutationsDisabled || busy} onRunAll={() => void mutate(() => api.runAll(notebook), { refreshAfter: false }, setOperation)} onInterrupt={() => void mutate(() => api.interrupt(notebook, kernel))} onRestart={() => void mutate(() => api.restart(notebook, kernel))} /><FileToolbar notebook={notebook} uploadDisabled={mutationsDisabled || busy} onUpload={handleUpload} onDownload={() => void handleDownload()} /></div>
    </header>
    {notice && <Notice notice={notice} onClose={() => setNotice(null)} />}
    <div className="editor-layout">
      <NotebookView notebook={notebook} scope={scope} turn={selectedTurn} disabled={mutationsDisabled || busy} focusRequest={focusRequest} draftResetRequest={draftResetRequest}
        onSave={(cellId, source) => void mutate(
          () => api.saveSource(notebook, cellId, source),
          {
            conflictText: "Notebook changed elsewhere. Your unsaved edit was not applied; the latest revision has been loaded.",
            onConflict: () => setDraftResetRequest((current) => ({ cellId, generation: (current?.generation ?? 0) + 1 })),
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
        onRevert={(turnId, cellId) => void mutate(() => api.revertCell(notebook, turnId, cellId), {}, (updated) => {
          setNotebook(updated);
          setHistory((items) => updateTurnRecord(items, turnId, (item) => ({ ...item, changes: item.changes.filter((change) => change.cellId !== cellId) })));
          setTurn((item) => item?.turnId === turnId ? { ...item, changes: item.changes.filter((change) => change.cellId !== cellId) } : item);
        })} />
      <AgentChatPanel notebook={notebook} scope={scope} turn={selectedTurn} activeTurn={activeTurn} history={history} operation={operation} busy={busy} mutationsDisabled={mutationsDisabled}
        onClearScope={() => void mutate(() => api.clearScope(notebook), { refreshAfter: false }, setScope)}
        onSubmit={(prompt) => { const frozen = { ...scope, editableCellIds: [...scope.editableCellIds], contextCellIds: [...scope.contextCellIds] }; void mutate(() => api.startTurn(notebook, prompt), { refreshAfter: false }, (result) => { setTurn(result); setSelectedTurnId(result.turnId); setHistory((items) => upsertRecord(items, result, frozen, prompt)); }); }}
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

function reconcileHistory(summaries: TurnRecord[], existing: TurnRecord[], notebook: NotebookSnapshot): TurnRecord[] {
  return summaries.map((summary) => {
    const detail = existing.find((item) => item.turn.turnId === summary.turn.turnId);
    const turn = summary.turn.historyTruncated && detail && !detail.turn.historyTruncated
      ? {
        ...summary.turn,
        prompt: detail.turn.prompt,
        editableCellIds: detail.turn.editableCellIds,
        contextCellIds: detail.turn.contextCellIds,
        finalOutput: detail.turn.finalOutput,
        changes: detail.turn.changes,
        error: detail.turn.error,
        historyTruncated: false,
      }
      : summary.turn;
    return {
      turn: reconcileTurnChanges(turn, notebook),
      editableCellIds: detail?.editableCellIds ?? summary.editableCellIds,
      contextCellIds: detail?.contextCellIds ?? summary.contextCellIds,
      prompt: detail && summary.turn.historyTruncated ? detail.prompt : summary.prompt,
    };
  });
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
