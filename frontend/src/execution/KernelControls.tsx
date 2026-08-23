import Icon from "../ui/Icon";
import type { KernelStatus } from "../api/client";

export default function KernelControls({ status, mutationDisabled, runAwaitingApproval = false, onRunAll, onInterrupt, onRestart }: { status: KernelStatus; mutationDisabled: boolean; runAwaitingApproval?: boolean; onRunAll: () => void; onInterrupt: () => void; onRestart: () => void }) {
  // A run parked at `awaiting_approval` is waiting on a person, and restarting
  // underneath it strands the operation: the approval it is waiting for can no
  // longer be honoured by the kernel it was queued against. The approval panel
  // is not modal — deliberately, so the notebook stays readable while deciding
  // — so nothing else stops this button being pressed mid-decision.
  //
  // Disabling is safe rather than a corner to be stuck in: the same panel
  // offers Cancel run, which ends the pause and gives the control straight
  // back. That is the way out, and the title says so.
  const restartBlocked = runAwaitingApproval && Boolean(status.kernelSessionId);
  return <div className="kernel-controls">
    <span className={`kernel-state state-${status.state}`}><i />Kernel {status.state.replaceAll("_", " ")}</span>
    {/* The kernel chip states the kernel's own truth, and during a parked run
        that truth reads as if nothing is happening — a cell awaiting approval
        has not started one, so it says "Kernel not started" while a run is very
        much outstanding. Rather than overload the chip with an execution state
        it does not describe, the pause gets its own word next to it, in a live
        region so it is announced and not only seen. */}
    {runAwaitingApproval && <span className="kernel-paused" role="status">Run paused for approval</span>}
    <button title="Run all cells" aria-label="Run all cells" disabled={mutationDisabled} onClick={onRunAll}><Icon name="play_arrow" /></button>
    <button title="Interrupt kernel" aria-label="Interrupt kernel" disabled={!status.kernelSessionId || status.state !== "busy"} onClick={onInterrupt}><Icon name="stop_circle" /></button>
    <button title={restartBlocked ? "A run is paused for approval — decide or cancel it first" : "Restart kernel"} aria-label="Restart kernel" disabled={!status.kernelSessionId || restartBlocked} onClick={onRestart}><Icon name="refresh" /></button>
  </div>;
}
