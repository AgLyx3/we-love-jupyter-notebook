import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

const plotNotebook = path.resolve("examples/plot-tuning.ipynb");
const backendUrl = `http://127.0.0.1:${process.env.E2E_BACKEND_PORT ?? "8001"}`;

// examples/plot-tuning.ipynb, by position. The accessible names the app builds
// are `${cellType} cell ${index + 1}`, so these are the two cells this spec
// drives: the literals it rewrites, and the plot it rewrites them for.
const parameters = "Source for code cell 4";
const plot = "Source for code cell 7";

function cellOf(page: Page, sourceLabel: string) {
  return page.locator(".notebook-cell").filter({ has: page.getByLabel(sourceLabel) });
}

function sourceText(cell: { source: string | string[] }) {
  return Array.isArray(cell.source) ? cell.source.join("") : cell.source;
}

// Deliberately a copy of notebook-editor.spec.ts's `openSample` rather than a
// shared import: e2e/ has no helper module, and the two specs are meant to be
// independently readable. The parts that matter are the same — one notebook
// session exists per backend and Playwright reuses the server across specs, so
// whatever a previous test left open has to be closed with its own session and
// revision before another notebook can be loaded.
async function openPlotNotebook(page: Page) {
  await page.goto("/");
  const opened = page.waitForResponse((response) => response.url().includes("/api/notebooks/open") && response.request().method() === "POST");
  await page.evaluate(async (notebookPath) => {
    const current = await fetch("/api/notebooks/current");
    if (current.ok) {
      const { sessionId, revision } = await current.json();
      const closed = await fetch("/api/notebooks/current", {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId, expectedDocumentRevision: revision }),
      });
      await closed.text();
    }
    const response = await fetch("/api/notebooks/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: notebookPath }),
    });
    // Drain the body: the reload below aborts anything still in flight, which
    // a requestfailed listener would report as an unexpected failure.
    await response.text();
  }, plotNotebook);
  expect((await opened).ok()).toBeTruthy();
  await page.reload();
  await expect(page.getByText("plot-tuning.ipynb", { exact: true })).toBeVisible();
}

async function documentRevision(page: Page) {
  const snapshot = await page.request.get(`${backendUrl}/notebooks/current`).then((response) => response.json());
  return snapshot.revision as number;
}

// The Tune button is gated on the cell having an image output, so the notebook
// has to have run once before the feature exists at all.
async function runEverything(page: Page) {
  await page.getByLabel("Run all cells").click();
  await expect(cellOf(page, plot).locator(".image-output")).toBeVisible({ timeout: 90_000 });
  await expect(page.locator(".kernel-state")).toContainText("Kernel idle", { timeout: 90_000 });
}

// The layout rules the main spec enforces globally, narrowed to the panel.
// Worth repeating here because the panel is the only place in the app that
// makes the output region two columns, and because the knob rail is fixed-width
// inside a pane that can be much narrower than the viewport: both are ways to
// push .notebook-cell wider than it is allowed to be.
async function assertPanelLayout(page: Page) {
  const geometry = await page.locator(".tuning-panel").evaluate((panel) => {
    const visible = (node: Element) => {
      const box = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0;
    };
    const controls = [...panel.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled)")].filter(visible);
    // Measure what the user actually presses. A checkbox is 16px by design and
    // sits inside its own <label>, which is the hit area that has to clear the
    // bar; every other control here is its own target.
    const hitTarget = (node: HTMLElement) => node.closest("label") ?? node;
    const cell = panel.closest(".notebook-cell")!;
    return {
      tiny: controls
        .filter((node) => { const box = hitTarget(node).getBoundingClientRect(); return box.width < 28 || box.height < 28; })
        .map((node) => node.getAttribute("aria-label") ?? node.textContent),
      clipped: cell.scrollWidth > cell.clientWidth + 2 && getComputedStyle(cell).overflowX === "visible",
      documentWidth: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    };
  });
  expect(geometry.tiny).toEqual([]);
  expect(geometry.clipped).toBe(false);
  expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewport + 1);
}

test("tunes a plot: preview costs nothing, apply rewrites the source and re-renders", async ({ page }) => {
  await openPlotNotebook(page);
  await runEverything(page);

  const plotCell = cellOf(page, plot);
  const committedImage = plotCell.locator(".cell-output-region .image-output");
  const beforeImage = await committedImage.getAttribute("src");
  expect(beforeImage).toBeTruthy();

  // Opening the panel is analysis only — no kernel, no document write — so the
  // Tune button can only appear once the scan has actually found knobs.
  await plotCell.getByLabel(`Tune ${plot.replace("Source for ", "")}`).click();
  const panel = plotCell.locator(".tuning-panel");
  // The warm-up replays the chain in a shadow kernel; until it lands the knobs
  // do not exist yet.
  await expect(panel).toHaveAttribute("data-state", "ready-clean", { timeout: 90_000 });

  // All seven of the fixture's knobs, covering every control type in §3.3.
  for (const name of ["N_POINTS", "NOISE", "BINS", "ALPHA", "COLOR", "GRID", "FIGSIZE"]) {
    await expect(panel.getByRole("group", { name, exact: true })).toBeVisible();
  }
  await expect(panel.getByText("Preview — not in your notebook yet")).toHaveCount(0);
  await assertPanelLayout(page);

  const revisionBeforePreview = await documentRevision(page);
  // By role, not by label: the knob's group, its slider and its numeric field
  // all carry the knob's name, and only the numeric field is being typed into.
  await panel.getByRole("spinbutton", { name: "BINS", exact: true }).fill("12");

  // The band is the promise that this picture is not the notebook's.
  await expect(panel.getByText("Preview — not in your notebook yet")).toBeVisible();
  const previewImage = panel.locator(".tuning-preview-frame .image-output");
  // Never blank while a preview is in flight: the last picture is the one the
  // new one is being compared against, so losing it defeats the feature.
  await expect(previewImage).toBeVisible();
  await expect.poll(() => previewImage.getAttribute("src"), { timeout: 60_000 }).not.toBe(beforeImage);
  await expect(panel).toHaveAttribute("data-state", "dirty-ready");

  // The load-bearing invariant of the whole design (D2): playing is free. The
  // committed document is untouched by a preview, and so is the cell's own
  // output — the notebook still shows the picture it was executed with.
  expect(await documentRevision(page)).toBe(revisionBeforePreview);
  const stored = await page.request.get(`${backendUrl}/notebooks/download`).then((response) => response.json());
  expect(sourceText(stored.cells.find((cell: { id: string }) => cell.id === "params-cell"))).toContain("BINS = 30");

  // Confirming is what costs, and the button says so before it is pressed.
  await panel.getByRole("button", { name: "Apply 1 change and re-run" }).click();
  await expect(panel).toHaveAttribute("data-state", "applied", { timeout: 120_000 });
  await expect(panel.getByText("Applied 1 change to your notebook.")).toBeVisible();
  // The live kernel's run-all was not this feature's, so apply cannot prove the
  // prefix is resident and widens the re-run to the whole notebook (§3.6). That
  // is a bigger bill than the Apply button quoted, so the panel says it out loud.
  await expect(panel.getByText("the whole notebook was re-run")).toBeVisible();

  await panel.getByRole("button", { name: "Close", exact: true }).click();
  await expect(panel).toHaveCount(0);

  // 1. The source changed — in the editor, and in the committed document.
  await expect(page.getByLabel(parameters)).toContainText("BINS = 12");
  const applied = await page.request.get(`${backendUrl}/notebooks/download`).then((response) => response.json());
  const rewritten = sourceText(applied.cells.find((cell: { id: string }) => cell.id === "params-cell"));
  expect(rewritten).toContain("BINS = 12");
  // Only the one literal moved; the other six knobs kept their values.
  expect(rewritten).toContain("N_POINTS = 800");
  expect(rewritten).toContain("COLOR = 'steelblue'");

  // 2. The outputs were re-rendered, by the live kernel, with the new value.
  await expect.poll(() => committedImage.getAttribute("src"), { timeout: 120_000 }).not.toBe(beforeImage);

  // The edit is the user's, and is reviewed as the user's.
  const tunedCell = cellOf(page, parameters);
  await expect(tunedCell.locator(".cell-review-label")).toContainText("You tuned this cell");
  await expect(tunedCell.getByLabel("Undo tuned change to code cell 4")).toBeVisible();
});

test("undoes a tuned change from the cell's own review controls", async ({ page }) => {
  await openPlotNotebook(page);
  await runEverything(page);

  const plotCell = cellOf(page, plot);
  await plotCell.getByLabel(`Tune ${plot.replace("Source for ", "")}`).click();
  const panel = plotCell.locator(".tuning-panel");
  await expect(panel).toHaveAttribute("data-state", "ready-clean", { timeout: 90_000 });

  // A bool knob this time: a toggle takes a different path through the rail and
  // through the literal rewrite than the numeric fields above.
  await panel.getByRole("checkbox", { name: "GRID", exact: true }).uncheck();
  await expect(panel).toHaveAttribute("data-state", /dirty-(previewing|ready)/);
  await panel.getByRole("button", { name: "Apply 1 change and re-run" }).click();
  await expect(panel).toHaveAttribute("data-state", "applied", { timeout: 120_000 });
  await panel.getByRole("button", { name: "Close", exact: true }).click();

  const applied = await page.request.get(`${backendUrl}/notebooks/download`).then((response) => response.json());
  expect(sourceText(applied.cells.find((cell: { id: string }) => cell.id === "params-cell"))).toContain("GRID = False");

  // Wait for apply's live re-run to finish before undoing. Undo is guarded on
  // the document revision and every re-executed cell bumps it, so an undo fired
  // mid-run loses a 409 race — and loses it *quietly*, because the notice is at
  // the top of the window and the cell still reads correctly.
  await expect(page.locator(".kernel-state")).toContainText("Kernel idle", { timeout: 120_000 });
  const tunedCell = cellOf(page, parameters);
  await tunedCell.getByLabel("Undo tuned change to code cell 4").click();

  // The review surface clearing is the assertion that the undo landed. The
  // editor's text is not: while the diff is on screen it renders the removed
  // line inside the editor, so "GRID = True" is in there either way.
  await expect(tunedCell.locator(".cell-review-label")).toHaveCount(0);
  await expect(page.getByLabel(parameters)).toContainText("GRID = True");
  const restored = await page.request.get(`${backendUrl}/notebooks/download`).then((response) => response.json());
  expect(sourceText(restored.cells.find((cell: { id: string }) => cell.id === "params-cell"))).toContain("GRID = True");
});
