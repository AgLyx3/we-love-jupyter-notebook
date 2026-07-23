import { useEffect, useRef } from "react";

// Debounced auto-save. Calls `save(source)` once the source has stayed idle for
// `delay` ms, the way editors auto-save "after delay". Every change resets the
// timer, and it never fires while `paused` (e.g. an agent turn or execution is
// running, or a save is already in flight) to avoid revision conflicts.
export function useAutoSave(
  source: string,
  dirty: boolean,
  paused: boolean,
  save: (source: string) => void,
  delay = 1000,
): void {
  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    if (!dirty || paused) return;
    const timer = setTimeout(() => saveRef.current(source), delay);
    return () => clearTimeout(timer);
  }, [source, dirty, paused, delay]);
}
