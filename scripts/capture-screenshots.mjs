// Capture the reference screenshots in docs/screenshots/.
//
// These shots exist for design work — handing a redesign agent (or a human
// designer) the current surfaces without making them stand up the stack. They
// are documentation, not assertions: nothing compares them, so a stale shot
// fails silently. Re-run this after any visual change.
//
// Deliberately minimal: eight shots, one per distinct visual system, not one
// per screen. Every extra near-duplicate is another image that goes stale
// silently and another thing a reader has to diff by eye. The script still
// drives the app through every intermediate state — scoping cells, spending a
// real agent turn, opening the tuner — it just photographs fewer of them. If
// you need a state that is not here, add a shot() call rather than a new run.
//
// This deliberately does NOT use playwright.config.ts. That config starts the
// dev server with --test-agent, and the agent panel is one of the surfaces
// being photographed — a canned transcript would misrepresent it. Start the
// app yourself with the real Claude CLI first:
//
//   .venv/bin/python scripts/dev.py --backend-port 8010 --frontend-port 5183
//   node scripts/capture-screenshots.mjs
//
// Env: SHOT_URL (default http://127.0.0.1:5183), SHOT_ONLY (substring filter),
//      SHOT_SKIP_AGENT=1 to skip the shots that need a real agent turn.
//
// Side effect: this runs every cell of the example notebooks, and
// examples/ml-pipeline.ipynb writes model_weights.txt into the working
// directory. Delete it afterwards, or ignore it.

import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..");
const OUT = path.join(ROOT, "docs", "screenshots");
const URL_BASE = process.env.SHOT_URL ?? "http://127.0.0.1:5183";
const ONLY = process.env.SHOT_ONLY ?? "";
const SKIP_AGENT = process.env.SHOT_SKIP_AGENT === "1";

const WORKSPACE = path.join(ROOT, "examples");
const PIPELINE = path.join(WORKSPACE, "ml-pipeline.ipynb");
const PLOTS = path.join(WORKSPACE, "plot-tuning.ipynb");

const captured = [];

async function shot(page, name, target, options = {}) {
  if (ONLY && !name.includes(ONLY)) return false;
  const file = path.join(OUT, `${name}.png`);
  const locator = typeof target === "string" ? page.locator(target) : target;
  if (target === null) await page.screenshot({ path: file, ...options });
  else await locator.screenshot({ path: file, ...options });
  captured.push(name);
  console.log(`  ✓ ${name}.png`);
  return true;
}

// Close whatever session is open. One notebook session exists per backend, and
// closing needs the current session id and revision — the revision can move
// under us if a cell is still running, so this retries.
async function closeNotebook(page) {
  await page.goto(URL_BASE);
  await page.evaluate(async () => {
    for (let attempt = 0; attempt < 10; attempt += 1) {
      const current = await fetch("/api/notebooks/current");
      if (!current.ok) break;
      const { sessionId, revision } = await current.json();
      const closed = await fetch("/api/notebooks/current", {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId, expectedDocumentRevision: revision }),
      });
      await closed.text();
      if (closed.ok) break;
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  });
  await page.reload();
}

// Walk the picker from its starting folder (the backend defaults to $HOME) down
// to `dir`, one folder row per path segment.
//
// Opening through the picker rather than POSTing to /api/notebooks/open is what
// makes the rail show both tabs: the workspace folder lives in React state, so a
// notebook opened by fetch-then-reload comes back with no folder and an
// outline-only rail. It is also the flow a real user takes.
async function navigateTo(page, dir) {
  const relative = path.relative(process.env.HOME ?? "/", dir);
  if (relative.startsWith("..")) throw new Error(`${dir} is not under $HOME; the picker cannot reach it`);
  for (const segment of relative.split(path.sep)) {
    await page.getByRole("button", { name: segment, exact: true }).click();
    await page.locator(".file-picker-path", { hasText: segment }).waitFor();
  }
}

async function openViaPicker(page, notebookPath) {
  await closeNotebook(page);
  await page.getByLabel("Open a notebook or folder").click();
  await page.locator(".file-picker").waitFor();
  await navigateTo(page, path.dirname(notebookPath));
  await page.getByRole("button", { name: "Use this folder" }).click();
  await page.getByRole("button", { name: path.basename(notebookPath), exact: true }).click();
  await page.locator(".brand strong").filter({ hasText: path.basename(notebookPath) }).waitFor();
  await page.getByRole("tab", { name: "Outline" }).waitFor();
}

// Risk classification pauses cells that touch files, the network, or the shell
// and waits for a human decision. examples/ml-pipeline.ipynb has one (it writes
// model_weights.txt), so an unattended capture deadlocks on the modal unless
// something answers it. Skip rather than approve: a screenshot run should not
// write files into the repo as a side effect.
async function skipRiskyCell(page) {
  const skip = page.getByRole("button", { name: "Skip cell" });
  if (await skip.count()) { console.log("  · skipped a risky cell"); await skip.click(); return true; }
  return false;
}

// waitFor, but answering the risky-execution modal if it appears meanwhile.
async function waitDismissingRisky(page, locator, timeout, label) {
  const deadline = Date.now() + timeout;
  for (;;) {
    if (await locator.count() && await locator.first().isVisible()) return;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
    await skipRiskyCell(page);
    await page.waitForTimeout(500);
  }
}

async function runAll(page) {
  await page.getByLabel("Run all cells").click();
  await waitDismissingRisky(page, page.locator(".kernel-state.state-idle"), 180_000, "an idle kernel");
  // An idle kernel is not a quiescent app. The execution *operation* settles
  // after the kernel does, and until it does `mutationsDisabled` is true, which
  // silently blocks agent submission (canSubmit in AgentChatPanel). Waiting on
  // the composer button is the only honest signal that the app is ready again.
  await settled(page);
  await page.waitForTimeout(800);
}

// True when the app will accept an agent turn again. Typing into the composer
// is what makes the button's enabled state meaningful — with an empty prompt it
// is disabled regardless of whether anything is still running.
async function settled(page, timeout = 120_000) {
  const composer = page.locator(".prompt-form textarea");
  const deadline = Date.now() + timeout;
  await composer.fill("ready?");
  try {
    while (await composerButton(page).isDisabled()) {
      if (Date.now() > deadline) throw new Error("app never became ready for an agent turn");
      await skipRiskyCell(page);
      await page.waitForTimeout(500);
    }
  } finally {
    await composer.fill("");
  }
}

// The submit button's label changes with scope and mode: Ask / Send /
// Send · Trusted / Plan.
function composerButton(page) {
  return page.locator(".prompt-form button[type='submit']");
}

async function main() {
  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    colorScheme: "light",
  });
  const page = await context.newPage();

  // ---- 1. Empty state, before anything is open -------------------------
  console.log("empty state");
  await closeNotebook(page);

  // ---- 2. The file picker, sitting in the examples folder --------------
  console.log("file picker");
  await page.getByLabel("Open a notebook or folder").click();
  await page.locator(".file-picker").waitFor();
  await navigateTo(page, WORKSPACE);
  await page.getByLabel("Close file picker").click();

  // ---- 3. The whole editor, notebook open, outputs live ----------------
  console.log("editor shell (ml-pipeline, run all)");
  await openViaPicker(page, PIPELINE);
  await runAll(page);
  await shot(page, "01-app-shell", null);

  // A single code cell with its output — the densest component in the app.
  const cellWithOutput = page.locator(".notebook-cell").filter({ has: page.locator(".cell-outputs") }).first();
  await cellWithOutput.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await shot(page, "03-cell-with-output", cellWithOutput);

  // ---- 4. The outline panel (notebook overview) ------------------------
  console.log("outline panel");
  await page.getByRole("tab", { name: "Outline" }).click();
  await page.locator(".outline-panel").waitFor();
  const build = page.getByRole("button", { name: /Build map|Rebuild map/ });
  if (!SKIP_AGENT && await build.count()) {
    await build.click();
    await page.getByRole("button", { name: "Rebuild map" }).waitFor({ timeout: 180_000 });
    await page.waitForTimeout(600);
    await shot(page, "06-outline-panel", ".workspace-sidebar");
  }

  // ---- 5. Turn scope: select cells and mark them editable --------------
  console.log("turn scope");
  await page.getByRole("tab", { name: "Files" }).click();
  const gutters = page.locator(".cell-gutter");
  await gutters.nth(2).click();
  await gutters.nth(4).click({ modifiers: ["Shift"] });
  await gutters.nth(4).click({ button: "right" });
  await page.locator(".cell-context-menu").waitFor();
  await page.getByRole("menuitem", { name: /Add \d+ to edit/ }).click();
  await page.locator(".scope-items").waitFor();
  await page.waitForTimeout(300);

  // ---- 6. A real agent turn: transcript, review bar, inline diff -------
  if (!SKIP_AGENT) {
    console.log("agent turn (real Claude CLI — this takes a minute)");
    const composer = page.locator(".prompt-form textarea");
    await composer.fill("Add a short comment above each scoped cell explaining what it does. Keep the code itself unchanged.");

    // Click the button rather than pressing Enter: both go through canSubmit,
    // but a disabled button fails here as a timeout instead of a silent no-op.
    await composerButton(page).click({ timeout: 60_000 });
    await page.locator(".turn-status").waitFor({ timeout: 60_000 });
    await waitDismissingRisky(page, page.locator(".review-bar"), 600_000, "the review bar");
    // The turn keeps running after the diff lands — it re-executes what it
    // touched — so let it settle, or the panel photographs as a spinner.
    for (let i = 0; i < 180; i += 1) {
      const state = (await page.locator(".turn-state").innerText().catch(() => "")).toLowerCase();
      if (!/executing|running|applying|queued/.test(state)) break;
      await skipRiskyCell(page);
      await page.waitForTimeout(1000);
    }
    await page.waitForTimeout(1500);
    await shot(page, "05-agent-panel", ".agent-panel");
    const diffCell = page.locator(".notebook-cell").filter({ has: page.locator(".cell-review-label") }).first();
    await diffCell.scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    await shot(page, "04-cell-under-review", diffCell);
    await shot(page, "02-app-shell-reviewing", null);
  }

  // ---- 7. Plot tuning --------------------------------------------------
  console.log("plot tuning (plot-tuning.ipynb, run all)");
  await openViaPicker(page, PLOTS);
  await runAll(page);
  const tune = page.getByRole("button", { name: /^Tune / }).first();
  if (await tune.count()) {
    await tune.scrollIntoViewIfNeeded();
    await tune.click();
    await waitDismissingRisky(page, page.locator(".tuning-knobs"), 180_000, "the tuning knobs");
    await page.waitForTimeout(1500);
    // The whole viewport, not `.tuning-panel`. The knobs are a fixed-position
    // popover floating over the notebook now (stitch-diff B6), so they sit
    // outside the panel element's box — cropping to it would photograph the
    // plot and leave the controls out of frame.
    await shot(page, "07-tuning-panel", null);
  } else {
    console.log("  ! no Tune button found — skipping tuning shots");
  }

  // ---- 8. Narrow viewports, either side of the 800px breakpoint --------
  // 900px is the awkward in-between: still three columns, all of them cramped.
  // 390px is past the breakpoint, where the layout stacks and the hover-only
  // affordances become permanently visible.
  console.log("mobile viewport");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(600);
  await shot(page, "08-mobile", null, { fullPage: true });

  await browser.close();

  await writeFile(path.join(OUT, "MANIFEST.txt"),
    `Captured from ${URL_BASE} at 1440x900 @2x, except 08-mobile (390x844, full page).\n\n` +
    captured.map((name) => `${name}.png`).join("\n") + "\n");
  console.log(`\n${captured.length} screenshots in docs/screenshots/`);
}

main().catch((error) => { console.error(error); process.exit(1); });
