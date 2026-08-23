import { ShieldAlert } from "lucide-react";
import { useEffect, useRef } from "react";
import type { ExecutionAttempt, ExecutionOperation } from "../api/client";

export default function RiskyExecutionDialog({ operation, attempt, busy, onDecision }: { operation: ExecutionOperation; attempt: ExecutionAttempt; busy: boolean; onDecision: (decision: "approve" | "skip" | "cancel") => void }) {
  const correlated = Boolean(operation.operationId && operation.sessionId && operation.currentDocumentRevision != null && attempt.executionAttemptId && attempt.cellId);
  const turnCorrelated = correlated && Boolean(operation.parentTurnId);
  const dialogRef = useRef<HTMLElement>(null);
  const approveRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const firstEnabled = dialogRef.current?.querySelector<HTMLButtonElement>("button:not(:disabled)");
    const target = approveRef.current && !approveRef.current.disabled ? approveRef.current : firstEnabled ?? dialogRef.current;
    target?.focus();
    return () => previous?.focus();
  }, []);
  // Escape only. This panel is not modal and must not pretend to be: it renders
  // inline in the agent sidebar with no backdrop, and the rest of the editor
  // stays live behind it. Trapping Tab inside it therefore left the keyboard
  // with no non-destructive way out (WCAG 2.1.2), while `aria-modal` told a
  // screen reader the notebook was inert when a sighted person could still
  // click any of it. Both are gone.
  //
  // Modality was the wrong half to keep. The cell under judgement lives in
  // that notebook, so marking it inert would leave an assistive-technology
  // user with *less* context to decide with than everyone else has.
  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape" && correlated && !busy) { event.preventDefault(); onDecision("cancel"); }
  };
  return <section ref={dialogRef} tabIndex={-1} className="risk-dialog" role="alertdialog" aria-labelledby="risk-title" aria-describedby="risk-description" onKeyDown={handleKeyDown}>
    <div className="risk-title"><ShieldAlert /><div><h3 id="risk-title">Execution needs approval</h3><p id="risk-description">Cell {attempt.cellIndex + 1} was paused before running.</p></div></div>
    <ul>{attempt.risk.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
    <pre className="risk-source" aria-label={`Source preview for cell ${attempt.cellIndex + 1}`}>{attempt.sourcePreview || "Source preview unavailable"}</pre>
    {attempt.risk.matchedPatterns.length > 0 && <code>{attempt.risk.matchedPatterns.join(", ")}</code>}
    <div className="risk-actions"><button disabled={busy || !correlated} onClick={() => onDecision("cancel")}>Cancel run</button><button disabled={busy || !turnCorrelated} onClick={() => onDecision("skip")}>Skip cell</button><button ref={approveRef} className="primary" disabled={busy || !turnCorrelated} onClick={() => onDecision("approve")}>Approve and run</button></div>
  </section>;
}
