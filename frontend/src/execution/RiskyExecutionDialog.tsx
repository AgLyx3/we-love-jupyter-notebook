import { ShieldAlert } from "lucide-react";
import type { ExecutionAttempt, ExecutionOperation } from "../api/client";

export default function RiskyExecutionDialog({ operation, attempt, busy, onDecision }: { operation: ExecutionOperation; attempt: ExecutionAttempt; busy: boolean; onDecision: (decision: "approve" | "skip" | "cancel") => void }) {
  const correlated = Boolean(operation.operationId && operation.sessionId && operation.currentDocumentRevision != null && attempt.executionAttemptId && attempt.cellId);
  const turnCorrelated = correlated && Boolean(operation.parentTurnId);
  return <section className="risk-dialog" role="alertdialog" aria-labelledby="risk-title">
    <div className="risk-title"><ShieldAlert /><div><h3 id="risk-title">Execution needs approval</h3><p>Cell {attempt.cellIndex + 1} was paused before running.</p></div></div>
    <ul>{attempt.risk.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
    {attempt.risk.matchedPatterns.length > 0 && <code>{attempt.risk.matchedPatterns.join(", ")}</code>}
    <div className="risk-actions"><button disabled={busy || !correlated} onClick={() => onDecision("cancel")}>Cancel run</button><button disabled={busy || !turnCorrelated} onClick={() => onDecision("skip")}>Skip cell</button><button className="primary" disabled={busy || !turnCorrelated} onClick={() => onDecision("approve")}>Approve and run</button></div>
  </section>;
}
