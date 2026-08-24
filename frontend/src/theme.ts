import { useSyncExternalStore } from "react";

/** Light or dark, as a single global rather than React state.
 *
 *  The switch has to reach two places that are not in one tree: the
 *  `data-theme` attribute on <html>, which every CSS token hangs off, and the
 *  CodeMirror theme extension inside each cell editor. A store both can read
 *  keeps those two from disagreeing — the boundary between the stylesheet and
 *  the editor is the one place a half-applied theme is visible.
 *
 *  Default is the OS preference; an explicit choice is remembered. */
export type ThemeMode = "light" | "dark";

const KEY = "notebook-theme";
const listeners = new Set<() => void>();

function preferred(): ThemeMode {
  if (typeof window === "undefined") return "light";
  try {
    const stored = window.localStorage.getItem(KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch { /* private mode; fall through to the OS preference */ }
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

let current: ThemeMode = preferred();

function apply(next: ThemeMode) {
  current = next;
  if (typeof document !== "undefined") document.documentElement.dataset.theme = next;
  listeners.forEach((listener) => listener());
}

/** Paint the stored preference before the app renders, so there is no flash of
 *  the wrong theme on load. */
export function initTheme(): void { apply(current); }

export function setTheme(next: ThemeMode): void {
  try { window.localStorage.setItem(KEY, next); } catch { /* the choice just will not persist */ }
  apply(next);
}

export function useTheme(): ThemeMode {
  return useSyncExternalStore(
    (onChange) => { listeners.add(onChange); return () => { listeners.delete(onChange); }; },
    () => current,
    () => current,
  );
}
