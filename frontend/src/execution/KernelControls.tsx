import Icon from "../ui/Icon";
import type { KernelStatus } from "../api/client";

/** What the kernel chip says on hover: the environment the cells run in.
 *
 *  `ModuleNotFoundError: No module named 'pandas'` means one of two opposite
 *  things — this environment is missing a package, or it is the wrong
 *  environment — and until this was reported nothing on the page told them
 *  apart (#52). Naming the interpreter is what does.
 *
 *  Naming the *source* matters as much: passing `--kernel-python` and still
 *  landing in the wrong environment looks identical to never having passed it.
 *
 *  Deliberately a tooltip rather than a visible chip. The path is long, the
 *  toolbar is full, and the moment a person needs this is when a cell has just
 *  failed — which is where it belongs on screen, attached to the failure. That
 *  is the other half of #52 and is not this change.
 */
function interpreterTitle(status: KernelStatus): string | undefined {
  if (!status.interpreter) return undefined;
  const chosen = status.interpreterSource === "kernel-python";
  return `Cells run in ${status.interpreter}${chosen ? " (--kernel-python)" : ""}`;
}

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
    <span className={`kernel-state state-${status.state}`} title={interpreterTitle(status)}><i />Kernel {status.state.replaceAll("_", " ")}</span>
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
