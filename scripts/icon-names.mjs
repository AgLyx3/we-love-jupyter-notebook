// Which Material Symbols ligatures this app renders, and which ones the
// vendored font actually carries.
//
// Two callers, and they have to agree or the subsetting in
// `scripts/vendor-fonts.mjs` quietly drops an icon: the script itself, which
// builds the subset from the first list, and `frontend/src/fonts.test.ts`,
// which fails when the two lists have drifted apart.

import { readFile, readdir } from "node:fs/promises";
import path from "node:path";

export const ROOT = path.resolve(import.meta.dirname, "..");
export const SRC = path.join(ROOT, "frontend", "src");
export const FONTS_CSS = path.join(SRC, "fonts.css");

async function sourceFiles(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await sourceFiles(full)));
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

/** Every Material Symbols ligature the app can render.
 *
 *  Two shapes produce one: the <Icon> component, and the hand-built span in
 *  CellEditor's block widget, whose glyph is the third argument to its local
 *  `make`. Both take literals; see the note in ui/Icon.tsx about why they have
 *  to.
 *
 *  `name={mode === "dark" ? "light_mode" : "dark_mode"}` is why this reads the
 *  `name` attribute rather than every literal in the tag: "dark" is a
 *  comparison operand, and Google accepts an unknown icon name silently, so it
 *  would sit in the vendored list looking like an icon forever. Inside a
 *  braced expression only the branches — what follows `?` — are glyphs.
 */
export async function iconNames() {
  const names = new Set();
  const unresolved = [];
  for (const file of await sourceFiles(SRC)) {
    const source = await readFile(file, "utf8");
    for (const [, quoted, braced] of source.matchAll(/<Icon\b[^>]*?\bname=(?:"([a-z][a-z0-9_]*)"|\{([^}]*)\})/g)) {
      if (quoted) { names.add(quoted); continue; }
      const branches = braced.includes("?") ? braced.slice(braced.indexOf("?")) : braced;
      const literals = branches.match(/"[a-z][a-z0-9_]*"/g) ?? [];
      if (!literals.length) unresolved.push(`${path.relative(ROOT, file)}: name={${braced.trim()}}`);
      for (const literal of literals) names.add(literal.slice(1, -1));
    }
    for (const call of source.match(/\bmake\((?:[^)]*)\)/g) ?? []) {
      const args = call.match(/"[a-z][a-z0-9_]*"/gi) ?? [];
      if (args.length >= 3) names.add(args[2].slice(1, -1));
    }
  }
  // A name this cannot see is an icon that renders as a blank box for
  // everyone, and nothing downstream will say so — the subset is built from
  // this list. Loud, not silent.
  if (unresolved.length) throw new Error(`icon names that are not literals:\n  ${unresolved.join("\n  ")}`);
  return [...names].sort();
}

/** The icons `fonts.css` records as being in the vendored subset.
 *
 *  The generated sheet lists them in its header precisely so this is
 *  answerable without decoding a woff2 — and so a diff of that file says which
 *  icon arrived.
 */
export async function vendoredIconNames() {
  const sheet = await readFile(FONTS_CSS, "utf8");
  const header = sheet.slice(0, sheet.indexOf("*/"));
  return [...header.matchAll(/^ \*   ([a-z][a-z0-9_]*)$/gm)].map(([, name]) => name);
}
