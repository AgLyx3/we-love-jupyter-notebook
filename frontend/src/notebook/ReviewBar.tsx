import { Check, ChevronLeft, ChevronRight, MapPin, RotateCcw } from "lucide-react";
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
export default function ReviewBar({ origin = "agent", total, reviewed, keptCount, undoableCount, disabled, onPrevious, onNext, onFirst, onKeepAll, onUndoAll }: {
  // A tuning Apply produces the same ledger, so it reuses this bar — but it is
  // the user's own edit and must never be described as the agent's.
  origin?: "agent" | "tune";
  total: number; reviewed: number; keptCount: number; undoableCount: number; disabled: boolean;
  onPrevious: () => void; onNext: () => void; onKeepAll: () => void; onUndoAll: () => void;
  /** Jump to the first change rather than advancing from the cursor. */
  onFirst?: () => void;
}) {
  const tuned = origin === "tune";
  const what = tuned ? "tuned change" : "change";
  const [confirming, setConfirming] = useState(false);
  if (total === 0) return null;
  const undoAll = () => {
    if (disabled) return;
    if (!confirming) { setConfirming(true); return; }
    setConfirming(false);
    onUndoAll();
  };
  return <div className="review-bar" role="region" aria-label={tuned ? "Review tuned changes" : "Review agent changes"}>
    {/* The counter doubles as the way in. "3 changes" is the first thing read
        after an Apply, and the question it raises is "where?" — so it answers
        that itself rather than making the ‹ › the only route to the first one. */}
    <button
      type="button" className="review-bar-count" disabled={disabled}
      title={`Show the first of ${total} ${what}${total === 1 ? "" : "s"}`}
      aria-label={`Go to the first ${what}`}
      onClick={onFirst ?? onNext}
    >
      <MapPin /> {reviewed} of {total} {what}{total === 1 ? "" : "s"} reviewed
    </button>
    {confirming
      ? <div className="review-bar-confirm" role="alertdialog" aria-label="Confirm undo all">
        {/* State what it actually does. Undo all rejects the changes still
            awaiting review; anything already kept stays, and reversing those
            too is what the separate whole-turn undo is for. */}
        <span>Undo {undoableCount} unreviewed {what}{undoableCount === 1 ? "" : "s"}?{keptCount > 0 ? ` The ${keptCount} you kept stay.` : ""}</span>
        <button type="button" onClick={() => setConfirming(false)}>Cancel</button>
        <button type="button" className="review-bar-danger" disabled={disabled} onClick={undoAll}>Undo them</button>
      </div>
      : <div className="review-bar-actions">
        {/* Both directions: reviewing means moving back over a change as often
            as forward, and a one-way control forces a full wrap to revisit the
            one you just passed. Icon-only is the exception the labelled rule
            allows — a paired ‹ › is self-describing and non-destructive — so
            they carry titles and accessible names instead of visible text. */}
        <span className="review-bar-nav">
          <button type="button" disabled={disabled} title={`Previous ${what}`} aria-label={`Previous ${what}`} onClick={onPrevious}><ChevronLeft /></button>
          <button type="button" disabled={disabled} title={`Next ${what}`} aria-label={`Next ${what}`} onClick={onNext}><ChevronRight /></button>
        </span>
        <button type="button" disabled={disabled} onClick={onKeepAll}><Check /> Keep all</button>
        <button type="button" className="review-bar-danger" disabled={disabled || undoableCount === 0} onClick={undoAll}><RotateCcw /> Undo all</button>
      </div>}
  </div>;
}
