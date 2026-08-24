import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

// The hard fixture: 57 cells, no headings, no defs, out-of-order execution.
// It is the notebook the whole feature was argued over, so it is the one the
// panel gets driven against here.
const notebook = path.resolve("backend/tests/fixtures/messy-exploration.ipynb");
const workspaceRoot = path.resolve("backend/tests/fixtures");

// Deliberately a copy of the other specs' `openSample` rather than a shared
// import: e2e/ has no helper module and each spec is meant to be independently
// readable. One notebook session exists per backend and Playwright reuses the
// server across specs, so whatever a previous test left open has to be closed
// with its own session and revision before another notebook can load.
async function openNotebook(page: Page) {
  await page.goto("/");
  const opened = page.waitForResponse((response) =>
    response.url().includes("/api/notebooks/open") && response.request().method() === "POST");
  await page.evaluate(async ([notebookPath, root]) => {
    // Retry the close: a previous test may have left a cell still executing, so
    // the revision can move between reading it and sending the DELETE. Closing
    // is a precondition here, not the thing under test.
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
    const response = await fetch("/api/notebooks/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: notebookPath, workspaceRoot: root }),
    });
    // Drain the body: the reload below aborts anything still in flight, which a
    // requestfailed listener would report as an unexpected failure.
    await response.text();
  }, [notebook, workspaceRoot]);
  expect((await opened).ok()).toBeTruthy();
  await page.reload();
  // Scoped to the title bar: the fixtures folder is the workspace root, so the
  // filename also appears as a row in the file tree.
  await expect(page.locator(".brand strong")).toHaveText("messy-exploration.ipynb");
}

// Open a workspace folder through the picker, so the rail has both a file tree
// and a notebook and therefore renders tabs. Opening a notebook by path does
// not set the workspace folder on its own, and neither does a reload.
async function openWorkspaceFolder(page: Page) {
  await page.getByLabel("Open a notebook or folder").click();
  await page.getByRole("button", { name: "Open this folder" }).click();
  await expect(page.getByRole("tab", { name: "Outline" })).toBeVisible();
}

// The rail shows the outline. With no folder open there are no tabs and the
// outline already has the whole rail (spec §7.1), so the click is conditional.
async function showOutline(page: Page) {
  const reveal = page.getByLabel("Show sidebar");
  if (await reveal.count()) await reveal.click();
  const tab = page.getByRole("tab", { name: "Outline" });
  if (await tab.count()) await tab.click();
  await expect(page.locator(".outline-panel")).toBeVisible();
}

test.describe("notebook overview panel", () => {
  test.beforeEach(async ({ page }) => { await openNotebook(page); });

  test("toggles the rail and lists blocks without calling the model", async ({ page }) => {
    // Opening a notebook must never spend anything (spec §3.2), so nothing may
    // hit /overview/generate until the button is pressed.
    let generated = 0;
    page.on("request", (request) => {
      if (request.url().includes("/api/overview/generate")) generated += 1;
    });

    await showOutline(page);
    const outline = page.locator(".outline-panel");
    await expect(outline).toBeVisible();
    // The panel is never empty: with no cached map it shows the deterministic
    // blocks, and with one it shows that. Either way nothing was spent.
    await expect(outline.locator(".outline-block")).not.toHaveCount(0);
    expect(generated).toBe(0);
  });

  test("clicking a block focuses its first cell", async ({ page }) => {
    await showOutline(page);
    const blocks = page.locator(".outline-block");
    await expect(blocks).not.toHaveCount(0);

    // The second block, so the target is not already at the top of the viewport.
    const target = blocks.nth(1);
    const start = Number(await target.getAttribute("data-start"));
    expect(Number.isFinite(start)).toBeTruthy();

    await target.locator(".outline-jump").click();
    // The app's existing focus path scrolls the cell in and focuses it. Cell
    // labels are 1-based, block indices 0-based.
    const focused = page.locator(".notebook-cell:focus, .notebook-cell:focus-within");
    await expect(focused).toHaveCount(1);
    await expect(focused).toHaveAttribute("aria-label", new RegExp(`cell ${start + 1}$`));
  });

  test("builds a named map on request and keeps it across a cell run", async ({ page }) => {
    await showOutline(page);
    // "Build map" the first time, "Rebuild map" once one is cached — the cache
    // is keyed by notebook path and deliberately survives a reopen.
    await page.getByRole("button", { name: /build map/i }).click();

    // Names are model output, so this asserts the shape: every block named,
    // ranges partitioning the notebook, a sane count.
    const names = page.locator(".outline-name.generated");
    await expect(names).not.toHaveCount(0, { timeout: 60_000 });
    await expect(page.getByRole("button", { name: /Building…/ })).toHaveCount(0, { timeout: 60_000 });
    const count = await names.count();
    expect(count).toBeGreaterThan(1);
    expect(count).toBeLessThanOrEqual(57);
    for (const text of await names.allInnerTexts()) expect(text.trim()).not.toBe("");

    const ranges = await page.locator(".outline-block").evaluateAll((nodes) =>
      nodes.map((node) => [Number(node.getAttribute("data-start")), Number(node.getAttribute("data-end"))]));
    const covered = ranges.flatMap(([lo, hi]) => {
      const span: number[] = [];
      for (let index = lo; index <= hi; index += 1) span.push(index);
      return span;
    });
    // A valid partition of all 57 cells: contiguous, ordered, no gaps or overlaps.
    expect(covered).toEqual(Array.from({ length: 57 }, (_, index) => index));

    // Running cells must not invalidate the map (spec §5) — the frequent case,
    // and the whole reason the cache is keyed on sources rather than revision.
    const revisionBefore = await page.evaluate(() =>
      fetch("/api/notebooks/current").then((r) => r.json()).then((s) => s.revision));
    await page.getByLabel("Run all cells").click();
    await expect.poll(async () => page.evaluate(() =>
      fetch("/api/notebooks/current").then((r) => r.json()).then((s) => s.revision)),
      { timeout: 90_000 }).toBeGreaterThan(revisionBefore);
    // Let the run settle so the next spec's close is not racing the kernel.
    await expect(page.locator(".kernel-state")).toContainText(/Kernel idle/, { timeout: 120_000 });

    await expect(page.getByText(/Cells changed since this map was built/)).toHaveCount(0);
    await expect(page.locator(".outline-name.generated")).toHaveCount(count);
  });

  test("switches between Files and Outline in one rail", async ({ page }) => {
    await openWorkspaceFolder(page);
    // One rail, two tabs — not a second region beside the tree (spec §7.1).
    await expect(page.locator(".workspace-sidebar")).toHaveCount(1);
    await expect(page.getByLabel("File tree")).toBeVisible();

    await page.getByRole("tab", { name: "Outline" }).click();
    await expect(page.locator(".outline-panel")).toBeVisible();
    // Hidden, not unmounted (#33). Both panes stay mounted so a Build map in
    // flight survives a tab switch; `hidden` keeps the inactive one out of the
    // accessibility tree, which is what "not showing" has to mean for a screen
    // reader as well as an eye.
    await expect(page.getByLabel("File tree")).toBeHidden();

    await page.getByRole("tab", { name: "Files" }).click();
    await expect(page.getByLabel("File tree")).toBeVisible();
  });

  test("hides and reveals the rail from the top-left button", async ({ page }) => {
    await showOutline(page);
    await page.getByLabel("Hide sidebar").click();
    await expect(page.locator(".workspace-sidebar")).toHaveCount(0);
    // The existing sidebar-reveal button, extended rather than duplicated.
    await expect(page.locator(".brand .sidebar-reveal")).toHaveCount(1);
    await page.getByLabel("Show sidebar").click();
    await expect(page.locator(".outline-panel")).toBeVisible();
  });

  test("remembers the chosen tab for this notebook", async ({ page }) => {
    await openWorkspaceFolder(page);
    await page.getByRole("tab", { name: "Outline" }).click();
    await expect(page.locator(".outline-panel")).toBeVisible();

    const stored = await page.evaluate(() =>
      Object.entries(localStorage).filter(([key]) => key.startsWith("notebook-rail-tab:")));
    // Keyed by notebook path, so another notebook does not inherit this choice.
    expect(stored).toEqual([[`notebook-rail-tab:${await page.evaluate(() =>
      fetch("/api/notebooks/current").then((r) => r.json()).then((s) => s.notebookPath))}`, "outline"]]);
  });
});
