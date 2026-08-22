import { ShieldAlert } from "lucide-react";
import { useEffect, useRef } from "react";
import type { ExecutionAttempt, ExecutionOperation } from "../api/client";

export default function RiskyExecutionDialog({ operation, attempt, busy, onDecision }: { operation: ExecutionOperation; attempt: ExecutionAttempt; busy: boolean; onDecision: (decision: "approve" | "skip" | "cancel") => void }) {
  // Everything a decision has to carry: the operation, where it is, and which
  // attempt is asking. Deliberately not "has a parent turn" — approve and skip
  // were once turn-gated because only an agent turn's downstream cells could
  // pause here, but a model-initiated run pauses with no turn at all, and
  // requiring one left the person looking at the dialog with nothing enabled
  // but Cancel. The turn id is passed through as-is; the server matches it.
  const correlated = Boolean(operation.operationId && operation.sessionId && operation.currentDocumentRevision != null && attempt.executionAttemptId && attempt.cellId);
  const dialogRef = useRef<HTMLElement>(null);
  const approveRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const firstEnabled = dialogRef.current?.querySelector<HTMLButtonElement>("button:not(:disabled)");
    const target = approveRef.current && !approveRef.current.disabled ? approveRef.current : firstEnabled ?? dialogRef.current;
    target?.focus();
    return () => previous?.focus();
  }, []);
  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape" && correlated && !busy) { event.preventDefault(); onDecision("cancel"); return; }
    if (event.key !== "Tab") return;
    const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
    if (!controls.length) return;
    const first = controls[0]; const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  return <section ref={dialogRef} tabIndex={-1} className="risk-dialog" role="alertdialog" aria-modal="true" aria-labelledby="risk-title" aria-describedby="risk-description" onKeyDown={handleKeyDown}>
    <div className="risk-title"><ShieldAlert /><div><h3 id="risk-title">Execution needs approval</h3><p id="risk-description">Cell {attempt.cellIndex + 1} was paused before running.</p></div></div>
    <ul>{attempt.risk.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
    <pre className="risk-source" aria-label={`Source preview for cell ${attempt.cellIndex + 1}`}>{attempt.sourcePreview || "Source preview unavailable"}</pre>
    {attempt.risk.matchedPatterns.length > 0 && <code>{attempt.risk.matchedPatterns.join(", ")}</code>}
    <div className="risk-actions"><button disabled={busy || !correlated} onClick={() => onDecision("cancel")}>Cancel run</button><button disabled={busy || !correlated} onClick={() => onDecision("skip")}>Skip cell</button><button ref={approveRef} className="primary" disabled={busy || !correlated} onClick={() => onDecision("approve")}>Approve and run</button></div>
  </section>;
}
