import { Check, ChevronRight, RotateCcw } from "lucide-react";
import { useState } from "react";
import type { AgentOperation } from "../api/client";

// Review session for the changes an agent turn applied.
//
// Deliberately persistent rather than hover-revealed: reviewing is a state the
// user is *in*, and hiding its controls until hover is what made the previous
// per-cell revert control undiscoverable.
//
// Two safeguards against the failure mode this surface is known for in other
// editors — a destructive "undo all" sitting beside "keep all" in a bar that
// reflows as the count drops, so users repeatedly destroy work by mis-clicking:
//   * the buttons keep fixed positions and widths regardless of the counter, and
//   * undo-all asks for confirmation once anything has been kept, because that
//     is the point where it starts discarding decisions rather than just
//     undoing the agent.
export default function ReviewBar({ total, reviewed, keptCount, disabled, onNext, onKeepAll, onUndoAll }: {
  total: number; reviewed: number; keptCount: number; disabled: boolean;
  onNext: () => void; onKeepAll: () => void; onUndoAll: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  if (total === 0) return null;
  const undoAll = () => {
    if (keptCount > 0 && !confirming) { setConfirming(true); return; }
    setConfirming(false);
    onUndoAll();
  };
  return <div className="review-bar" role="region" aria-label="Review agent changes">
    <span className="review-bar-count">{reviewed} of {total} change{total === 1 ? "" : "s"} reviewed</span>
    {confirming
      ? <div className="review-bar-confirm" role="alertdialog" aria-label="Confirm undo all">
        <span>Undo all also reverses the {keptCount} change{keptCount === 1 ? "" : "s"} you kept.</span>
        <button type="button" onClick={() => setConfirming(false)}>Cancel</button>
        <button type="button" className="review-bar-danger" onClick={undoAll}>Undo everything</button>
      </div>
      : <div className="review-bar-actions">
        <button type="button" disabled={disabled} onClick={onNext}><ChevronRight /> Next change</button>
        <button type="button" disabled={disabled} onClick={onKeepAll}><Check /> Keep all</button>
        <button type="button" className="review-bar-danger" disabled={disabled} onClick={undoAll}><RotateCcw /> Undo all</button>
      </div>}
  </div>;
}
