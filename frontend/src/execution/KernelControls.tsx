import Icon from "../ui/Icon";
import type { KernelStatus } from "../api/client";

export default function KernelControls({ status, mutationDisabled, onRunAll, onInterrupt, onRestart }: { status: KernelStatus; mutationDisabled: boolean; onRunAll: () => void; onInterrupt: () => void; onRestart: () => void }) {
  return <div className="kernel-controls">
    <span className={`kernel-state state-${status.state}`}><i />Kernel {status.state.replaceAll("_", " ")}</span>
    <button title="Run all cells" aria-label="Run all cells" disabled={mutationDisabled} onClick={onRunAll}><Icon name="play_arrow" /></button>
    <button title="Interrupt kernel" aria-label="Interrupt kernel" disabled={!status.kernelSessionId || status.state !== "busy"} onClick={onInterrupt}><Icon name="stop_circle" /></button>
    <button title="Restart kernel" aria-label="Restart kernel" disabled={!status.kernelSessionId} onClick={onRestart}><Icon name="refresh" /></button>
  </div>;
}
