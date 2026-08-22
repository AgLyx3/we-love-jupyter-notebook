import { AlertTriangle, BookOpen, Code2, File, RotateCcw, Send, Square, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type AgentMode, type AgentModel, type AgentTurn, type ExecutionAttempt, type ExecutionOperation, type FileMatch, type NotebookSnapshot, type StructuralOp, type TurnOptions, type TurnScope, type WriteScope } from "../api/client";
import RiskyExecutionDialog from "../execution/RiskyExecutionDialog";
import TurnScopePanel from "../turnScope/TurnScopePanel";
import { attachmentLabel, type SelectionAttachment } from "../notebook/selectionEdit";
import { applyMention, detectMention, mentionKey, type MentionToken } from "./fileMention";

const activeStates = new Set(["created", "agent_running", "validating", "applying", "executing", "cleaning_up"]);

function structuralSummary(ops: StructuralOp[]): string {
  const counts: Record<string, number> = {};
  for (const op of ops) counts[op.op] = (counts[op.op] ?? 0) + 1;
  const order: StructuralOp["op"][] = ["add", "delete", "move", "retype", "edit"];
  const parts = order.filter((kind) => counts[kind]).map((kind) => `${counts[kind]} ${kind}`);
  return parts.length ? parts.join(" · ") : "no change";
}
export interface TurnRecord { turn: AgentTurn; editableCellIds: string[]; contextCellIds: string[]; prompt: string }

export default function AgentChatPanel({ notebook, scope, turn, activeTurn, history, operation, busy, mutationsDisabled, writeScope: writeScopeProp, onWriteScopeChange, attachments = [], onSubmit, onCancel, onUndo, onClearScope, onDecision, onSelectTurn, onFocusCell, onDropCell, onDropCells, onRemoveAttachment, onRemoveScopeCell }: {
  notebook: NotebookSnapshot; scope: TurnScope; turn: AgentTurn | null; activeTurn: AgentTurn | null; history: TurnRecord[]; operation: ExecutionOperation | null; busy: boolean; mutationsDisabled: boolean;
  writeScope?: WriteScope; onWriteScopeChange?: (next: WriteScope) => void;
  attachments?: SelectionAttachment[];
  onSubmit: (prompt: string, options: TurnOptions) => void; onCancel: () => void; onUndo: () => void; onClearScope: () => void; onDecision: (attempt: ExecutionAttempt, decision: "approve" | "skip" | "cancel") => void; onSelectTurn: (id: string) => void; onFocusCell: (id: string) => void; onDropCell: (id: string) => void; onDropCells?: (ids: string[]) => void;
  onRemoveAttachment?: (id: string) => void; onRemoveScopeCell?: (id: string) => void;
}) {
  const keptCount = (turn?.operations ?? []).filter((item) => item.state === "accepted").length;
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState<AgentModel>("default");
  const [mode, setMode] = useState<AgentMode>("edit");
  // Write scope is STICKY and normally controlled by the parent (App), which
  // shares it with the notebook gutter and drop routing. When rendered standalone
  // (tests) it falls back to internal sticky state persisted to localStorage.
  const [internalWriteScope, setInternalWriteScope] = useState<WriteScope>(() => {
    try { return localStorage.getItem("agent.writeScope") === "trusted" ? "trusted" : "blocking"; } catch { return "blocking"; }
  });
  const writeScope = writeScopeProp ?? internalWriteScope;
  const updateWriteScope = (next: WriteScope) => {
    if (onWriteScopeChange) { onWriteScopeChange(next); return; }
    setInternalWriteScope(next);
    try { localStorage.setItem("agent.writeScope", next); } catch { /* storage unavailable */ }
  };

  // "@"-mention: type "@" in the prompt to search workspace files and insert a
  // path as context. Purely a text-insertion aid — the agent still reads the
  // referenced file itself; nothing is uploaded here.
  const workspaceRoot = notebook.workspaceRoot ?? null;
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const [mention, setMention] = useState<MentionToken | null>(null);
  const [matches, setMatches] = useState<FileMatch[]>([]);
  const [activeMatch, setActiveMatch] = useState(0);
  const dismissedKeyRef = useRef<string | null>(null);
  const pendingCaretRef = useRef<number | null>(null);
  const menuOpen = Boolean(mention && workspaceRoot && matches.length && mentionKey(mention) !== dismissedKeyRef.current);

  const syncMention = (target: HTMLTextAreaElement) => {
    const next = detectMention(target.value, target.selectionStart ?? target.value.length);
    setMention(next);
    if (!next) setMatches([]);
    else if (mentionKey(next) !== dismissedKeyRef.current) dismissedKeyRef.current = null;
  };

  useEffect(() => {
    if (!mention || !workspaceRoot || mentionKey(mention) === dismissedKeyRef.current) { setMatches([]); return; }
    let live = true;
    const timer = window.setTimeout(() => {
      api.searchFiles(workspaceRoot, mention.query)
        .then((result) => { if (live) { setMatches(result.matches); setActiveMatch(0); } })
        .catch(() => { if (live) setMatches([]); });
    }, 120);
    return () => { live = false; window.clearTimeout(timer); };
  }, [mention, workspaceRoot]);

  // Restore the caret after a mention insertion re-renders the textarea.
  useLayoutEffect(() => {
    if (pendingCaretRef.current == null) return;
    const target = promptRef.current;
    if (target) { target.focus(); target.setSelectionRange(pendingCaretRef.current, pendingCaretRef.current); }
    pendingCaretRef.current = null;
  }, [prompt]);

  const chooseMatch = (match: FileMatch | undefined) => {
    if (!mention || !match) return;
    const caret = promptRef.current?.selectionStart ?? prompt.length;
    const { text, caret: nextCaret } = applyMention(prompt, mention.start, caret, match.relativePath);
    pendingCaretRef.current = nextCaret;
    setPrompt(text);
    setMention(null);
    setMatches([]);
  };

  const closeMention = () => {
    if (mention) dismissedKeyRef.current = mentionKey(mention);
    setMatches([]);
  };
  const awaiting = operation?.attempts.find((attempt) => attempt.state === "awaiting_approval" && !attempt.decision);
  const manualAttempt = operation?.attempts.find((item) => item.executionAttemptId === operation.currentExecutionAttemptId);
  const manualCorrelated = Boolean(operation?.operationId && operation.sessionId && operation.currentDocumentRevision != null && operation.parentTurnId === null && manualAttempt?.executionAttemptId && manualAttempt.cellId);
  const active = turn && activeStates.has(turn.state);
  // "editable" is removed under Trusted (structural): the gutter edit button is
  // blocked, drops default to context, and the scope panel shows attention-only.
  // The composer note/label additionally require Edit mode (Plan writes nothing).
  const trustedScope = writeScope === "trusted";
  const trusted = trustedScope && mode === "edit";
  const readOnly = scope.editableCellIds.length === 0 && !trusted;
  // An attached error output is enough to submit on its own — the user can add a
  // cell's error and hit Send without typing (a default "fix the error" prompt
  // is composed from the attachment).
  const hasErrorAttachment = attachments.some((attachment) => attachment.kind === "error");
  const canSubmit = !busy && !mutationsDisabled && (Boolean(prompt.trim()) || hasErrorAttachment);
  const submitPrompt = () => {
    if (!canSubmit) return;
    const value = prompt.trim();
    onSubmit(value, { model, mode, writeScope });
    setPrompt("");
    setMention(null);
    setMatches([]);
  };
  return <aside className="agent-panel" aria-label="Agent workspace" onDragOver={(event) => { if (!mutationsDisabled) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; } }} onDrop={(event) => { event.preventDefault(); if (mutationsDisabled) return; const many = event.dataTransfer.getData("application/x-notebook-cells"); if (many) { try { const ids = JSON.parse(many) as string[]; if (ids?.length) { if (onDropCells) onDropCells(ids); else ids.forEach(onDropCell); return; } } catch { /* fall through to single */ } } const id = event.dataTransfer.getData("application/x-notebook-cell"); if (id) onDropCell(id); }}>
    <header>
      <div className="agent-panel-title"><h1>Notebook Agent</h1><span>Scoped local edits</span></div>
      <label className="prompt-select scope-header">
        <span>Scope</span>
        <select aria-label="Write scope" value={writeScope} disabled={busy || mode === "plan"}
          title={mode === "plan" ? "Plan mode writes nothing, so write scope has no effect." : "Blocking: the agent may only edit cells you mark editable. Trusted: the agent may add, delete, reorder, and edit any cell in the notebook."}
          onChange={(event) => updateWriteScope(event.target.value as WriteScope)}>
          <option value="blocking">Blocking</option>
          <option value="trusted">Trusted</option>
        </select>
      </label>
    </header>
    <TurnScopePanel notebook={notebook} scope={scope} disabled={mutationsDisabled} trusted={trustedScope} onClear={onClearScope} onFocusCell={onFocusCell} onDropCell={onDropCell} onRemoveCell={onRemoveScopeCell} />
    <section className="conversation" aria-live="polite">
      {history.length > 0 && <div className="turn-history" aria-label="Turn history">{history.map((record) => <button className={record.turn.turnId === turn?.turnId ? "selected" : ""} key={record.turn.turnId} onClick={() => onSelectTurn(record.turn.turnId)}><span>{record.prompt}</span><small>{(record.turn.writeScope === "trusted" ? "all editable" : `${record.editableCellIds.length} editable`) + " · " + record.turn.state.replaceAll("_", " ")}</small></button>)}</div>}
      {activeTurn && activeTurn.turnId !== turn?.turnId && <button className="manual-cancel" onClick={onCancel}><Square /> Cancel active turn</button>}
      {!turn && <div className="empty-conversation"><p>No agent turn yet</p><span>Select cells to edit, or just ask a read-only question.</span></div>}
      {turn && <div className="turn-status">
        <div className="turn-state"><span className={active ? "activity-dot" : ""} />{turn.state.replaceAll("_", " ")}</div>
        {turn.finalOutput && <div className="turn-output"><ReactMarkdown>{turn.finalOutput}</ReactMarkdown></div>}
        {turn.error && <p className="error-text">{turn.error.message}</p>}
        {turn.changes.length > 0 && turn.writeScope !== "trusted" && <p>{turn.changes.length} cell{turn.changes.length === 1 ? "" : "s"} changed. Review the inline diff.</p>}
        {turn.structuralOps && turn.structuralOps.length > 0 && <p className="structural-summary">Structural changes: {structuralSummary(turn.structuralOps)}. Review the inline diff — undo reverts the whole turn.</p>}
        {/* Unlike "Undo all" in the review bar, restoring the checkpoint also
            reverses changes the user explicitly kept, so say so rather than
            letting the label imply it only undoes outstanding work. */}
        {keptCount > 0 && turn.undoEligible && !active && <p className="turn-undo-note" role="note">Undoing the turn also reverses the {keptCount} change{keptCount === 1 ? "" : "s"} you kept.</p>}
        <div className="turn-actions">{active && <button onClick={onCancel}><Square /> Cancel turn</button>}{turn.undoEligible && !active && <button disabled={mutationsDisabled} onClick={onUndo}><RotateCcw /> Undo entire turn</button>}</div>
      </div>}
      {operation && <div className={`execution-status ${operation.error ? "has-error" : ""}`}><span>Execution: {operation.state.replaceAll("_", " ")}</span>{operation.error && <p>{operation.error.message}</p>}{operation.attempts.filter((attempt) => attempt.error).map((attempt) => <p key={attempt.executionAttemptId}>Cell {attempt.cellIndex + 1}: {attempt.error!.message}</p>)}{operation.attempts.filter((attempt) => attempt.outputsTruncated).map((attempt) => <p key={`${attempt.executionAttemptId}-output-truncated`}>Cell {attempt.cellIndex + 1}: Retained execution output was truncated.</p>)}</div>}
      {operation && operation.parentTurnId === null && !["completed", "failed", "cancelled", "validation_incomplete", "timed_out"].includes(operation.state) && manualAttempt && <button disabled={!manualCorrelated} className="manual-cancel" onClick={() => onDecision(manualAttempt, "cancel")}><Square /> Cancel run</button>}
      {operation && awaiting && <RiskyExecutionDialog operation={operation} attempt={awaiting} busy={busy} onDecision={(decision) => onDecision(awaiting, decision)} />}
    </section>
    <form className="prompt-form" onSubmit={(event) => { event.preventDefault(); submitPrompt(); }}>
      <label htmlFor="agent-prompt">Agent instruction</label>
      {attachments.length > 0 && <div className="chat-attachments" aria-label="Referenced selections">{attachments.map((attachment) => <span className={`attachment-chip ${attachment.kind}`} key={attachment.id}>
        <button type="button" className="attachment-focus" title="Reveal selection" onClick={() => onFocusCell(attachment.cellId)}>{attachment.kind === "error" ? <AlertTriangle /> : <Code2 />} {attachmentLabel(attachment)}</button>
        <button type="button" className="attachment-remove" aria-label={`Remove ${attachmentLabel(attachment)}`} onClick={() => onRemoveAttachment?.(attachment.id)}><X /></button>
      </span>)}</div>}
      <div className="mention-anchor">
        {menuOpen && <ul className="mention-menu" role="listbox" aria-label="Workspace files">
          {matches.map((match, index) => <li key={match.path} role="option" aria-selected={index === activeMatch}>
            <button type="button" className={`mention-option ${index === activeMatch ? "active" : ""}`}
              onMouseDown={(event) => { event.preventDefault(); chooseMatch(match); }}
              onMouseEnter={() => setActiveMatch(index)}>
              {match.kind === "notebook" ? <BookOpen /> : <File />}
              <span className="mention-name">{match.name}</span>
              <span className="mention-path">{match.relativePath}</span>
            </button>
          </li>)}
        </ul>}
        <textarea id="agent-prompt" ref={promptRef} value={prompt}
          onChange={(event) => { setPrompt(event.target.value); syncMention(event.target); }}
          onClick={(event) => syncMention(event.currentTarget)}
          onKeyUp={(event) => { if (!["Enter", "Tab", "ArrowUp", "ArrowDown", "Escape"].includes(event.key)) syncMention(event.currentTarget); }}
          onKeyDown={(event) => {
            if (menuOpen) {
              if (event.key === "ArrowDown") { event.preventDefault(); setActiveMatch((index) => (index + 1) % matches.length); return; }
              if (event.key === "ArrowUp") { event.preventDefault(); setActiveMatch((index) => (index - 1 + matches.length) % matches.length); return; }
              if (event.key === "Enter" || event.key === "Tab") { event.preventDefault(); chooseMatch(matches[activeMatch]); return; }
              if (event.key === "Escape") { event.preventDefault(); closeMention(); return; }
            }
            if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
            event.preventDefault();
            submitPrompt();
          }} placeholder={trusted ? "Ask the agent to change the notebook…" : readOnly ? "Ask about the notebook, or select cells to edit…" : "Change the selected cells…"} rows={3} />
      </div>
      {readOnly && mode === "edit" && <span className="prompt-mode" role="note">Read-only turn — the agent can answer but not write.</span>}
      {trusted && <span className="prompt-mode trusted" role="note">Trusted turn — the agent may add, delete, reorder, and edit any cell. Review the diff before keeping.</span>}
      {mode === "plan" && <span className="prompt-mode" role="note">Plan mode — the agent proposes a plan and writes no changes.</span>}
      <div className="prompt-controls">
        <label className="prompt-select">
          <span>Model</span>
          <select aria-label="Agent model" value={model} disabled={busy} onChange={(event) => setModel(event.target.value as AgentModel)}>
            <option value="default">Default</option>
            <option value="opus">Opus</option>
            <option value="sonnet">Sonnet</option>
            <option value="haiku">Haiku</option>
          </select>
        </label>
        <label className="prompt-select">
          <span>Mode</span>
          <select aria-label="Agent mode" value={mode} disabled={busy} onChange={(event) => setMode(event.target.value as AgentMode)}>
            <option value="edit">Edit</option>
            <option value="plan">Plan</option>
          </select>
        </label>
        <button className={`primary ${trusted ? "trusted" : ""}`} disabled={!canSubmit} type="submit"><Send /> {mode === "plan" ? "Plan" : trusted ? "Send · Trusted" : readOnly ? "Ask" : "Send"}</button>
      </div>
    </form>
  </aside>;
}
