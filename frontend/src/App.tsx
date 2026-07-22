import { AlertTriangle, BookOpen, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, connectEvents, type AgentTurn, type ExecutionAttempt, type ExecutionOperation, type KernelStatus, type NotebookSnapshot, type TurnScope } from "./api/client";
import AgentChatPanel from "./agentChat/AgentChatPanel";
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
  const [operation, setOperation] = useState<ExecutionOperation | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "error" | "warning"; text: string } | null>(null);
  const [polling, setPolling] = useState(false);
  const snapshotRef = useRef(notebook);
  const turnRef = useRef(turn);
  const operationRef = useRef(operation);
  useEffect(() => { snapshotRef.current = notebook; }, [notebook]);
  useEffect(() => { turnRef.current = turn; }, [turn]);
  useEffect(() => { operationRef.current = operation; }, [operation]);

  const refresh = useCallback(async () => {
    try {
      const current = await api.current();
      setNotebook(current);
      const [nextScope, nextKernel] = await Promise.all([api.scope(), api.kernel()]);
      setScope(nextScope);
      setKernel(nextKernel);
      return current;
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) { setNotebook(null); setScope(emptyScope); return null; }
      throw error;
    }
  }, []);

  useEffect(() => { refresh().catch((error) => showError(error)).finally(() => setLoading(false)); }, [refresh]);

  const fetchTurn = useCallback(async (id: string) => {
    try {
      const next = await api.turn(id); setTurn(next);
      if (next.executionOperationId) setOperation(await api.execution(next.executionOperationId));
      if (terminalTurns.has(next.state)) await refresh();
    } catch (error) { showError(error); }
  }, [refresh]);

  const fetchExecution = useCallback(async (id: string) => {
    try {
      const next = await api.execution(id); setOperation(next);
      if (terminalExecutions.has(next.state)) await refresh();
      else setKernel(await api.kernel());
    } catch (error) { showError(error); }
  }, [refresh]);

  useEffect(() => {
    if (!notebook) return;
    setPolling(false);
    return connectEvents(notebook.sessionId, {
      notebook: () => void refresh(), turn: (id) => void fetchTurn(id), execution: (id) => void fetchExecution(id), disconnected: () => setPolling(true),
    });
  }, [notebook?.sessionId, refresh, fetchTurn, fetchExecution]);

  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(() => {
      const currentTurn = turnRef.current;
      const currentOperation = operationRef.current;
      if (currentTurn && !terminalTurns.has(currentTurn.state)) void fetchTurn(currentTurn.turnId);
      if (currentOperation && !terminalExecutions.has(currentOperation.state)) void fetchExecution(currentOperation.operationId);
      void refresh();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [polling, refresh, fetchTurn, fetchExecution]);

  function showError(error: unknown) {
    const text = error instanceof Error ? error.message : "The operation could not be completed";
    setNotice({ tone: "error", text });
  }

  async function mutate(work: () => Promise<unknown>, options: { conflictText?: string; refreshAfter?: boolean } = {}) {
    setBusy(true); setNotice(null);
    try {
      const result = await work();
      if (options.refreshAfter !== false) await refresh();
      return result;
    } catch (error) {
      if (error instanceof ApiError && error.isConflict) {
        await refresh();
        setNotice({ tone: "warning", text: options.conflictText ?? "Notebook changed elsewhere. The latest revision has been loaded." });
      } else showError(error);
      return null;
    } finally { setBusy(false); }
  }

  const handleUpload = async (file: File) => {
    const current = snapshotRef.current;
    const uploaded = await mutate(() => api.upload(file, current ?? undefined), { refreshAfter: false });
    if (uploaded) { setNotebook(uploaded as NotebookSnapshot); setScope(emptyScope); setTurn(null); setOperation(null); setKernel(await api.kernel()); }
  };

  if (loading) return <div className="loading-screen"><span className="spinner" />Loading notebook…</div>;

  if (!notebook) return <div className="app-shell empty-shell">
    <header className="topbar"><div className="brand"><BookOpen /><strong>Notebook Agent</strong></div><FileToolbar notebook={null} onUpload={handleUpload} /></header>
    <main className="upload-state"><BookOpen /><h1>Open a notebook to begin</h1><p>Upload a local <code>.ipynb</code> file. Edits remain in this editor until downloaded.</p><label className="upload-button">Choose notebook<input type="file" accept=".ipynb,application/x-ipynb+json" onChange={(event) => { const file = event.target.files?.[0]; if (file) void handleUpload(file); }} /></label></main>
    {notice && <Notice notice={notice} onClose={() => setNotice(null)} />}
  </div>;

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand"><BookOpen /><strong>{notebook.filename}</strong><span className={notebook.dirty ? "dirty" : ""}>{notebook.dirty ? "Unsaved" : "Clean"}</span><span>Revision {notebook.revision}</span></div>
      <div className="toolbar-actions"><KernelControls status={kernel} disabled={busy} onRunAll={() => void mutate(async () => { const result = await api.runAll(notebook); setOperation(result); }, { refreshAfter: false })} onInterrupt={() => void mutate(() => api.interrupt(notebook, kernel))} onRestart={() => void mutate(() => api.restart(notebook, kernel))} /><FileToolbar notebook={notebook} onUpload={handleUpload} /></div>
    </header>
    {notice && <Notice notice={notice} onClose={() => setNotice(null)} />}
    <div className="editor-layout">
      <NotebookView notebook={notebook} scope={scope} turn={turn}
        onSave={(cellId, source) => void mutate(() => api.saveSource(notebook, cellId, source), { conflictText: "Notebook changed elsewhere. Your unsaved edit was not applied; the latest revision has been loaded." })}
        onRun={(cellId) => void mutate(async () => { const result = await api.runCell(notebook, cellId); setOperation(result); }, { refreshAfter: false })}
        onScope={(cellId, editable) => void mutate(async () => setScope(await api.addScope(notebook, cellId, editable)), { refreshAfter: false })}
        onRevert={(turnId, cellId) => void mutate(async () => setNotebook(await api.revertCell(notebook, turnId, cellId)), { refreshAfter: false })} />
      <AgentChatPanel notebook={notebook} scope={scope} turn={turn} operation={operation} busy={busy}
        onClearScope={() => void mutate(async () => setScope(await api.clearScope(notebook)), { refreshAfter: false })}
        onSubmit={(prompt) => void mutate(async () => { const result = await api.startTurn(notebook, prompt); setTurn(result); }, { refreshAfter: false })}
        onCancel={() => turn && void mutate(async () => setTurn(await api.cancelTurn(notebook, turn.turnId)), { refreshAfter: false })}
        onUndo={() => turn && void mutate(async () => setNotebook(await api.undoTurn(notebook, turn.turnId)), { refreshAfter: false })}
        onDecision={(attempt: ExecutionAttempt, decision) => operation && void mutate(async () => setOperation(await api.decide(notebook, operation, attempt, decision)), { refreshAfter: false })} />
    </div>
  </div>;
}

function Notice({ notice, onClose }: { notice: { tone: "error" | "warning"; text: string }; onClose: () => void }) {
  return <div className={`notice ${notice.tone}`} role="alert"><AlertTriangle /><span>{notice.text}</span><button aria-label="Dismiss message" onClick={onClose}><X /></button></div>;
}
