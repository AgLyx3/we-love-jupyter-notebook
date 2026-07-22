import { RotateCcw, Send, Square } from "lucide-react";
import { useState } from "react";
import type { AgentTurn, ExecutionAttempt, ExecutionOperation, NotebookSnapshot, TurnScope } from "../api/client";
import RiskyExecutionDialog from "../execution/RiskyExecutionDialog";
import TurnScopePanel from "../turnScope/TurnScopePanel";

const activeStates = new Set(["created", "agent_running", "validating", "applying", "executing", "cleaning_up"]);
export interface TurnRecord { turn: AgentTurn; editableCellIds: string[]; contextCellIds: string[]; prompt: string }

export default function AgentChatPanel({ notebook, scope, turn, activeTurn, history, operation, busy, mutationsDisabled, onSubmit, onCancel, onUndo, onClearScope, onDecision, onSelectTurn, onFocusCell, onDropCell }: {
  notebook: NotebookSnapshot; scope: TurnScope; turn: AgentTurn | null; activeTurn: AgentTurn | null; history: TurnRecord[]; operation: ExecutionOperation | null; busy: boolean; mutationsDisabled: boolean;
  onSubmit: (prompt: string) => void; onCancel: () => void; onUndo: () => void; onClearScope: () => void; onDecision: (attempt: ExecutionAttempt, decision: "approve" | "skip" | "cancel") => void; onSelectTurn: (id: string) => void; onFocusCell: (id: string) => void; onDropCell: (id: string) => void;
}) {
  const [prompt, setPrompt] = useState("");
  const awaiting = operation?.attempts.find((attempt) => attempt.state === "awaiting_approval" && !attempt.decision);
  const manualAttempt = operation?.attempts.find((item) => item.executionAttemptId === operation.currentExecutionAttemptId);
  const manualCorrelated = Boolean(operation?.operationId && operation.sessionId && operation.currentDocumentRevision != null && operation.parentTurnId === null && manualAttempt?.executionAttemptId && manualAttempt.cellId);
  const active = turn && activeStates.has(turn.state);
  return <aside className="agent-panel" aria-label="Agent workspace" onDragOver={(event) => { if (!mutationsDisabled) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; } }} onDrop={(event) => { event.preventDefault(); const id = event.dataTransfer.getData("application/x-notebook-cell"); if (id && !mutationsDisabled) onDropCell(id); }}>
    <header><h1>Notebook Agent</h1><span>Scoped local edits</span></header>
    <TurnScopePanel notebook={notebook} scope={scope} disabled={mutationsDisabled} onClear={onClearScope} onFocusCell={onFocusCell} onDropCell={onDropCell} />
    <section className="conversation" aria-live="polite">
      {history.length > 0 && <div className="turn-history" aria-label="Turn history">{history.map((record) => <button className={record.turn.turnId === turn?.turnId ? "selected" : ""} key={record.turn.turnId} onClick={() => onSelectTurn(record.turn.turnId)}><span>{record.prompt}</span><small>{record.editableCellIds.length} editable · {record.contextCellIds.length} context · {record.turn.state.replaceAll("_", " ")}</small></button>)}</div>}
      {activeTurn && activeTurn.turnId !== turn?.turnId && <button className="manual-cancel" onClick={onCancel}><Square /> Cancel active turn</button>}
      {!turn && <div className="empty-conversation"><p>No agent turn yet</p><span>Select editable cells, then describe the change.</span></div>}
      {turn && <div className="turn-status">
        <div className="turn-state"><span className={active ? "activity-dot" : ""} />{turn.state.replaceAll("_", " ")}</div>
        {turn.finalOutput && <p>{turn.finalOutput}</p>}
        {turn.error && <p className="error-text">{turn.error.message}</p>}
        {turn.changes.length > 0 && <p>{turn.changes.length} cell{turn.changes.length === 1 ? "" : "s"} changed. Review the inline diff.</p>}
        <div className="turn-actions">{active && <button onClick={onCancel}><Square /> Cancel turn</button>}{turn.appliedRevision != null && !active && <button disabled={mutationsDisabled} onClick={onUndo}><RotateCcw /> Undo turn</button>}</div>
      </div>}
      {operation && <div className="execution-status">Execution: {operation.state.replaceAll("_", " ")}</div>}
      {operation && operation.kind === "manual" && !["completed", "failed", "cancelled", "validation_incomplete", "timed_out"].includes(operation.state) && manualAttempt && <button disabled={!manualCorrelated} className="manual-cancel" onClick={() => onDecision(manualAttempt, "cancel")}><Square /> Cancel run</button>}
      {operation && awaiting && <RiskyExecutionDialog operation={operation} attempt={awaiting} busy={busy} onDecision={(decision) => onDecision(awaiting, decision)} />}
    </section>
    <form className="prompt-form" onSubmit={(event) => { event.preventDefault(); const value = prompt.trim(); if (!value) return; onSubmit(value); setPrompt(""); }}>
      <label htmlFor="agent-prompt">Agent instruction</label>
      <textarea id="agent-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Change the selected cells…" rows={3} />
      <button className="primary" disabled={busy || mutationsDisabled || !prompt.trim() || scope.editableCellIds.length === 0} type="submit"><Send /> Send</button>
    </form>
  </aside>;
}
