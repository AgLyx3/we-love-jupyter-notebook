import { RotateCcw, Send, Square } from "lucide-react";
import { useState } from "react";
import type { AgentTurn, ExecutionAttempt, ExecutionOperation, NotebookSnapshot, TurnScope } from "../api/client";
import RiskyExecutionDialog from "../execution/RiskyExecutionDialog";
import TurnScopePanel from "../turnScope/TurnScopePanel";

const activeStates = new Set(["created", "running", "validating", "applying", "executing", "awaiting_approval", "cleaning_up"]);

export default function AgentChatPanel({ notebook, scope, turn, operation, busy, onSubmit, onCancel, onUndo, onClearScope, onDecision }: {
  notebook: NotebookSnapshot; scope: TurnScope; turn: AgentTurn | null; operation: ExecutionOperation | null; busy: boolean;
  onSubmit: (prompt: string) => void; onCancel: () => void; onUndo: () => void; onClearScope: () => void; onDecision: (attempt: ExecutionAttempt, decision: "approve" | "skip" | "cancel") => void;
}) {
  const [prompt, setPrompt] = useState("");
  const awaiting = operation?.attempts.find((attempt) => attempt.state === "awaiting_approval" && !attempt.decision);
  const active = turn && activeStates.has(turn.state);
  return <aside className="agent-panel" aria-label="Agent workspace">
    <header><h1>Notebook Agent</h1><span>Scoped local edits</span></header>
    <TurnScopePanel notebook={notebook} scope={scope} onClear={onClearScope} />
    <section className="conversation" aria-live="polite">
      {!turn && <div className="empty-conversation"><p>No agent turn yet</p><span>Select editable cells, then describe the change.</span></div>}
      {turn && <div className="turn-status">
        <div className="turn-state"><span className={active ? "activity-dot" : ""} />{turn.state.replaceAll("_", " ")}</div>
        {turn.finalOutput && <p>{turn.finalOutput}</p>}
        {turn.error && <p className="error-text">{turn.error.message}</p>}
        {turn.changes.length > 0 && <p>{turn.changes.length} cell{turn.changes.length === 1 ? "" : "s"} changed. Review the inline diff.</p>}
        <div className="turn-actions">{active && <button onClick={onCancel}><Square /> Cancel turn</button>}{turn.appliedRevision != null && !active && <button onClick={onUndo}><RotateCcw /> Undo turn</button>}</div>
      </div>}
      {operation && <div className="execution-status">Execution: {operation.state.replaceAll("_", " ")}</div>}
      {operation && awaiting && <RiskyExecutionDialog operation={operation} attempt={awaiting} busy={busy} onDecision={(decision) => onDecision(awaiting, decision)} />}
    </section>
    <form className="prompt-form" onSubmit={(event) => { event.preventDefault(); const value = prompt.trim(); if (!value) return; onSubmit(value); setPrompt(""); }}>
      <label htmlFor="agent-prompt">Agent instruction</label>
      <textarea id="agent-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Change the selected cells…" rows={3} />
      <button className="primary" disabled={busy || !prompt.trim() || scope.editableCellIds.length === 0} type="submit"><Send /> Send</button>
    </form>
  </aside>;
}
