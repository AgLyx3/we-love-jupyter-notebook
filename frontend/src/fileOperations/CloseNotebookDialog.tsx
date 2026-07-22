import { AlertTriangle } from "lucide-react";
import { useEffect, useRef } from "react";

export default function CloseNotebookDialog({ busy, onCancel, onConfirm }: { busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  const dialogRef = useRef<HTMLElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    cancelRef.current?.focus();
    return () => previous?.focus();
  }, []);
  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape" && !busy) { event.preventDefault(); onCancel(); return; }
    if (event.key !== "Tab") return;
    const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
    if (!controls.length) return;
    const first = controls[0]; const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  return <div className="dialog-backdrop">
    <section ref={dialogRef} tabIndex={-1} className="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="close-dialog-title" aria-describedby="close-dialog-description" onKeyDown={handleKeyDown}>
      <div className="confirm-title"><AlertTriangle /><div><h2 id="close-dialog-title">Discard unsaved notebook?</h2><p id="close-dialog-description">Changes that have not been downloaded will be lost.</p></div></div>
      <div className="confirm-actions"><button ref={cancelRef} disabled={busy} onClick={onCancel}>Keep notebook</button><button className="danger" disabled={busy} onClick={onConfirm}>Discard notebook</button></div>
    </section>
  </div>;
}
