import { CircleStop, Play, RefreshCcw } from "lucide-react";
import type { KernelStatus } from "../api/client";

export default function KernelControls({ status, disabled, onRunAll, onInterrupt, onRestart }: { status: KernelStatus; disabled: boolean; onRunAll: () => void; onInterrupt: () => void; onRestart: () => void }) {
  return <div className="kernel-controls">
    <span className={`kernel-state state-${status.state}`}><i />Kernel {status.state.replaceAll("_", " ")}</span>
    <button title="Run all cells" aria-label="Run all cells" disabled={disabled} onClick={onRunAll}><Play /></button>
    <button title="Interrupt kernel" aria-label="Interrupt kernel" disabled={!status.kernelSessionId || status.state !== "busy"} onClick={onInterrupt}><CircleStop /></button>
    <button title="Restart kernel" aria-label="Restart kernel" disabled={!status.kernelSessionId} onClick={onRestart}><RefreshCcw /></button>
  </div>;
}
