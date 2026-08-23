import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

const sample = path.resolve("examples/sample.ipynb");
const terminalTurn = /completed|validation incomplete/;
const backendUrl = `http://127.0.0.1:${process.env.E2E_BACKEND_PORT ?? "8001"}`;
const frontendUrl = `http://127.0.0.1:${process.env.E2E_FRONTEND_PORT ?? "5174"}`;

function sourceText(cell: { source: string | string[] }) {
  return Array.isArray(cell.source) ? cell.source.join("") : cell.source;
}

async function replaceEditor(page: Page, label: string, source: string) {
  const editor = page.getByLabel(label).locator(".cm-content");
  await editor.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.insertText(source);
  // .cm-line only: diff decorations render removed lines and the per-hunk
  // Keep/Discard widgets inside .cm-content, so its innerText is the document plus
  // review chrome. The document lines are what this helper is asserting on.
  await expect.poll(() => editor.evaluate((node) =>
    [...node.querySelectorAll(".cm-line")].map((line) => (line as HTMLElement).innerText).join("\n"),
  )).toBe(source);
}

function cellOf(page: Page, sourceLabel: string) {
  return page.locator(".notebook-cell").filter({ has: page.getByLabel(sourceLabel) });
}

// Discard every agent change in one cell.
//
// Review lives on the hunks now: a cell whose changes are all hunks carries a
// Keep/Discard pair per changed region inside the editor and deliberately no
// pair in its header, so there is no single per-cell revert button to click.
// Discard each remaining hunk until the cell is back to its pre-turn source.
async function undoCellChanges(page: Page, sourceLabel: string) {
  const undo = cellOf(page, sourceLabel).getByLabel("Discard this change");
  let remaining = await undo.count();
  expect(remaining).toBeGreaterThan(0);
  while (remaining > 0) {
    await undo.first().click();
    await expect.poll(() => undo.count(), { timeout: 15_000 }).toBeLessThan(remaining);
    remaining = await undo.count();
  }
}

async function waitForTurn(page: Page, expected: RegExp = terminalTurn) {
  await expect(page.locator(".turn-state")).toHaveText(expected, { timeout: 45_000 });
}

// Load the sample notebook into a fresh session.
//
// The app opens notebooks by path now; the file-upload input these tests used
// to drive was removed when the local file/folder selector landed, which is why
// every test here had been failing at the first step. Going through the API
// rather than the file picker keeps the tests about the editor instead of the
// browser dialog, and matches how the app itself opens a file.
async function openSample(page: Page) {
  await page.goto("/");
  const opened = page.waitForResponse((response) => response.url().includes("/api/notebooks/open") && response.request().method() === "POST");
  await page.evaluate(async (path) => {
    // One notebook session exists per backend, and Playwright reuses the server
    // across tests, so close whatever a previous test left open — replacing a
    // loaded notebook requires its session and revision as preconditions.
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
      body: JSON.stringify({ path }),
    });
    // Drain the body: the reload below aborts anything still in flight, which
    // the page's requestfailed listener reports as an unexpected failure.
    await response.text();
  }, sample);
  expect((await opened).ok()).toBeTruthy();
  await page.reload();
  await expect(page.getByText("sample.ipynb", { exact: true })).toBeVisible();
}

type Rect = { left: number; right: number; top: number; bottom: number; width: number; height: number };

function intersectionArea(first: Rect, second: Rect) {
  return Math.max(0, Math.min(first.right, second.right) - Math.max(first.left, second.left))
    * Math.max(0, Math.min(first.bottom, second.bottom) - Math.max(first.top, second.top));
}

async function assertOverlayLayout(page: Page, selector: string, actionSelector: string) {
  const layout = await page.locator(selector).evaluate((overlay, actionsSelector) => {
    const rect = (node: Element) => {
      const box = node.getBoundingClientRect();
      return { left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, height: box.height };
    };
    return {
      overlay: rect(overlay),
      topbar: rect(document.querySelector(".topbar")!),
      actions: [...overlay.querySelectorAll(actionsSelector as string)].map(rect),
      viewport: { width: window.innerWidth, height: window.innerHeight },
    };
  }, actionSelector);
  expect(layout.overlay.left).toBeGreaterThanOrEqual(0);
  expect(layout.overlay.right).toBeLessThanOrEqual(layout.viewport.width + 1);
  expect(intersectionArea(layout.overlay, layout.topbar)).toBe(0);
  for (let index = 0; index < layout.actions.length; index += 1) {
    const action = layout.actions[index];
    expect(action.width).toBeGreaterThanOrEqual(28);
    expect(action.height).toBeGreaterThanOrEqual(28);
    expect(action.left).toBeGreaterThanOrEqual(layout.overlay.left - 1);
    expect(action.right).toBeLessThanOrEqual(layout.overlay.right + 1);
    for (const other of layout.actions.slice(index + 1)) expect(intersectionArea(action, other)).toBe(0);
  }
}

async function assertUsableLayout(page: Page) {
  const geometry = await page.evaluate(() => {
    const rect = (node: Element) => {
      const box = node.getBoundingClientRect();
      return { left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, height: box.height };
    };
    const visible = (node: Element) => {
      const box = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0
        && box.bottom > 0 && box.top < window.innerHeight;
    };
    const controls = [...document.querySelectorAll<HTMLElement>("button:not(:disabled), .icon-button:not(.disabled)")].filter(visible);
    const topbarControls = [...document.querySelectorAll<HTMLElement>(".topbar button:not(:disabled), .topbar .icon-button:not(.disabled)")].filter(visible);
    const clippedText = [...document.querySelectorAll<HTMLElement>(".topbar, .agent-panel, .notebook-cell")]
      .filter((node) => node.scrollWidth > node.clientWidth + 2 && getComputedStyle(node).overflowX === "visible")
      .map((node) => node.className);
    return {
      viewport: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      tinyControls: controls.filter((node) => {
        const box = node.getBoundingClientRect();
        return box.width < 28 || box.height < 28;
      }).map((node) => node.getAttribute("aria-label") || node.textContent),
      controlsOutsideViewport: controls.filter((node) => {
        const box = node.getBoundingClientRect();
        return box.left < -1 || box.right > window.innerWidth + 1;
      }).map((node) => node.getAttribute("aria-label") || node.textContent),
      topbarControls: topbarControls.map(rect),
      notebook: rect(document.querySelector(".notebook-surface")!),
      agent: rect(document.querySelector(".agent-panel")!),
      clippedText,
    };
  });
  expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewport + 1);
  expect(geometry.tinyControls).toEqual([]);
  expect(geometry.controlsOutsideViewport).toEqual([]);
  expect(geometry.clippedText).toEqual([]);
  expect(geometry.notebook.width).toBeGreaterThan(300);
  expect(geometry.agent.width).toBeGreaterThan(300);
  expect(geometry.notebook.width * geometry.notebook.height).toBeGreaterThan(50_000);
  expect(geometry.agent.width * geometry.agent.height).toBeGreaterThan(50_000);
  expect(intersectionArea(geometry.notebook, geometry.agent)).toBe(0);
  for (let index = 0; index < geometry.topbarControls.length; index += 1) {
    for (const other of geometry.topbarControls.slice(index + 1)) {
      expect(intersectionArea(geometry.topbarControls[index], other)).toBe(0);
    }
  }
  const notebook = page.getByLabel("Notebook cells");
  await expect(notebook).toBeVisible();
  expect((await notebook.boundingBox())?.height ?? 0).toBeGreaterThan(200);
}

test("edits a notebook through scoped agent and execution workflows", async ({ page }, testInfo) => {
  const consoleErrors: Array<{ text: string; url: string }> = [];
  const pageErrors: string[] = [];
  const unexpectedResponses: string[] = [];
  const allowedConsoleCounts = new Map<string, number>();
  const unexpectedRequestFailures: string[] = [];
  let phase: "initial" | "upload" | "workflow" | "conflict" = "initial";
  let initialNotFoundResponses = 0;
  let intentionalConflictResponses = 0;
  let eventStreamTeardowns = 0;
  let replacedSessionId: string | null = null;
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push({ text: message.text(), url: message.location().url });
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    const failure = request.failure()?.errorText ?? "unknown failure";
    const url = new URL(request.url());
    const expectedEventTeardown = replacedSessionId !== null
      && url.origin === frontendUrl
      && url.pathname === "/api/events"
      && url.searchParams.get("sessionId") === replacedSessionId
      && url.searchParams.get("after") === "0"
      && request.method() === "GET"
      && failure === "net::ERR_ABORTED";
    if (expectedEventTeardown) {
      eventStreamTeardowns += 1;
      return;
    }
    unexpectedRequestFailures.push(`${request.url()} ${failure}`);
  });
  page.on("response", (response) => {
    if (response.status() < 400) return;
    const url = new URL(response.url());
    const expectedInitial = phase === "initial"
      && response.status() === 404
      && response.request().method() === "GET"
      && url.origin === frontendUrl
      && url.pathname === "/api/notebooks/current"
      && url.search === "";
    const expectedConflict = phase === "conflict"
      && response.status() === 409
      && response.request().method() === "POST"
      && url.origin === frontendUrl
      && url.pathname === "/api/cells/downstream/source"
      && url.search === "";
    if (!expectedInitial && !expectedConflict) {
      unexpectedResponses.push(`${response.status()} ${response.url()}`);
      return;
    }
    if (expectedInitial) initialNotFoundResponses += 1;
    if (expectedConflict) intentionalConflictResponses += 1;
    const statusText = response.status() === 404 ? "Not Found" : "Conflict";
    const message = `Failed to load resource: the server responded with a status of ${response.status()} (${statusText})`;
    const key = `${message}\n${response.url()}`;
    allowedConsoleCounts.set(key, (allowedConsoleCounts.get(key) ?? 0) + 1);
  });
  await expect.poll(async () => {
    const response = await page.request.get(`${backendUrl}/health/ready`).catch(() => null);
    return response?.status() ?? 0;
  }, { timeout: 15_000 }).toBe(200);
  const initialCurrent = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.origin === frontendUrl && url.pathname === "/api/notebooks/current" && response.request().method() === "GET";
  });
  await page.goto("/");
  const initialCurrentResponse = await initialCurrent;
  if (initialCurrentResponse.ok()) {
    replacedSessionId = String((await initialCurrentResponse.json()).sessionId);
  }
  phase = "upload";
  const opened = page.waitForResponse((response) => response.url().includes("/api/notebooks/open") && response.request().method() === "POST");
  await page.evaluate(async (path) => {
    const response = await fetch("/api/notebooks/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    await response.text();
  }, sample);
  expect((await opened).ok()).toBeTruthy();
  await page.reload();
  await expect(page.getByText("sample.ipynb", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Open a notebook or folder")).toBeEnabled();
  phase = "workflow";

  await replaceEditor(page, "Source for code cell 4", "average = total / len(values)\nprint(f'Average: {average}')");
  const manualSave = page.waitForResponse((response) => response.url().includes("/api/cells/downstream/source") && response.request().method() === "POST");
  await page.getByLabel("Save code cell 4").click();
  const manualResponse = await manualSave;
  expect(manualResponse.ok()).toBeTruthy();
  expect((await manualResponse.json()).revision).toBe(1);
  await expect(page.getByText("Revision 1")).toBeVisible();

  await page.getByLabel("Allow agent edit code cell 2").click();
  await page.getByLabel("Add code cell 3 as focus").click();
  await expect(page.getByText("1 editable")).toBeVisible();
  await expect(page.getByText("1 focus")).toBeVisible();

  await page.getByLabel("Agent instruction").fill("[safe] Update the parameter values");
  await page.getByLabel("Agent instruction").press("Enter");
  await waitForTurn(page, /completed/);
  // The diff is inline CodeMirror decorations now; the separate .cell-diff
  // panel these assertions targeted no longer exists.
  const safeEdited = cellOf(page, "Source for code cell 2");
  await expect(safeEdited.locator(".cm-diff-removed-line").first()).toContainText("values = [2, 4, 6]");
  await expect(safeEdited.locator(".cm-diff-added-line").first()).toContainText("values = [3, 6, 9]");
  await expect(page.getByLabel("Cell output").filter({ hasText: "Total: 18" })).toBeVisible();
  await page.getByRole("button", { name: "Undo entire turn" }).click();
  await expect(page.getByLabel("Source for code cell 2")).toContainText("values = [2, 4, 6]");

  await page.getByLabel("Allow agent edit code cell 2").click();
  await page.getByLabel("Add code cell 3 as focus").click();
  await page.getByLabel("Agent instruction").fill("[risk] Update values with an environment lookup");
  await page.getByRole("button", { name: "Send" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Execution needs approval" });
  await expect(dialog).toBeVisible({ timeout: 45_000 });
  await assertOverlayLayout(page, ".risk-dialog", ".risk-actions button");
  await expect(dialog).toContainText("Reads environment variables that may contain secrets");
  await dialog.getByRole("button", { name: "Approve and run" }).click();
  await waitForTurn(page, /completed/);
  await expect(page.getByLabel("Cell output").filter({ hasText: "Total: 30" })).toBeVisible();

  // Review controls: one per change, and only where they can act.
  //
  // This is the only suite that can check the per-hunk widgets at all — the
  // vitest specs mock CodeMirror away — and it is where the deduplication rule
  // is enforced end to end: the hunk pair is present, the header pair is not,
  // and both render as the same control rather than two unrelated buttons.
  const changed = cellOf(page, "Source for code cell 2");
  await expect(changed.getByLabel("Discard this change").first()).toBeVisible();
  await expect(changed.getByLabel("Keep this change").first()).toBeVisible();
  await expect(changed.getByLabel("Discard agent change to code cell 2")).toHaveCount(0);
  await expect(changed.getByLabel("Keep agent change to code cell 2")).toHaveCount(0);
  await expect(changed.locator(".cell-review-label")).toContainText("Agent Suggestion");
  // Not assertOverlayLayout: that also forbids overlapping the sticky topbar,
  // which is meaningless for a widget inline in a scrolling document. The part
  // that matters here is the touch target the project enforces elsewhere.
  for (const box of await changed.locator(".cm-hunk-actions button").all()) {
    const rect = (await box.boundingBox())!;
    expect(rect.height).toBeGreaterThanOrEqual(28);
    expect(rect.width).toBeGreaterThanOrEqual(28);
  }

  await undoCellChanges(page, "Source for code cell 2");
  await expect(page.getByLabel("Source for code cell 2")).toContainText("values = [2, 4, 6]");
  // Discarding the last hunk settles the cell, so its review surface clears.
  await expect(changed.getByLabel("Discard this change")).toHaveCount(0);
  await expect(changed.locator(".cell-review-label")).toHaveCount(0);

  await replaceEditor(page, "Source for code cell 4", "average = total / len(values)\nprint('stale save')");
  await page.route("**/api/cells/downstream/source", async (route) => {
    const snapshot = await page.request.get(`${backendUrl}/notebooks/current`).then((response) => response.json());
    const conflict = await page.request.post(`${backendUrl}/cells/safe-summary/source`, { data: {
      sessionId: snapshot.sessionId,
      expectedDocumentRevision: snapshot.revision,
      source: "total = sum(values)\nprint(f'Total: {total}')\n# external revision\n",
    } });
    expect(conflict.ok()).toBeTruthy();
    await route.continue();
  }, { times: 1 });
  phase = "conflict";
  await page.getByLabel("Save code cell 4").click();
  await expect(page.getByRole("alert")).toContainText("Notebook changed elsewhere");
  await assertOverlayLayout(page, ".notice", "button");
  await expect(page.getByLabel("Source for code cell 4").locator(".cm-content")).toContainText("stale save");
  await expect(page.getByLabel("Save code cell 4")).toBeEnabled();
  await expect(page.getByLabel("Run code cell 4")).toBeDisabled();
  await expect(page.getByLabel("Run all cells")).toBeDisabled();

  // The toolbar exports through Save / Save as now; the download button these
  // assertions drove was removed with the local file/folder work. The export
  // endpoint still serves the committed document, so assert on that.
  const exported = await page.request.get(`${backendUrl}/notebooks/download`);
  expect(exported.ok()).toBeTruthy();
  const downloadedText = await exported.text();
  expect(downloadedText.length).toBeGreaterThan(100);
  const downloaded = JSON.parse(downloadedText) as {
    nbformat: number;
    cells: Array<{ id: string; cell_type: string; source: string | string[] }>;
  };
  expect(downloaded.nbformat).toBe(4);
  expect(Array.isArray(downloaded.cells)).toBe(true);
  expect(downloaded.cells.length).toBe(4);
  expect(sourceText(downloaded.cells.find((cell) => cell.id === "parameters")!)).toBe("values = [2, 4, 6]\n");
  expect(sourceText(downloaded.cells.find((cell) => cell.id === "safe-summary")!)).toContain("# external revision");
  expect(sourceText(downloaded.cells.find((cell) => cell.id === "downstream")!)).toContain("Average: {average}");
  expect(downloadedText).not.toContain("stale save");

  await assertUsableLayout(page);
  await page.screenshot({ path: testInfo.outputPath("notebook-editor.png"), fullPage: true });
  const observedConsoleCounts = new Map<string, number>();
  for (const message of consoleErrors) {
    const key = `${message.text}\n${message.url}`;
    observedConsoleCounts.set(key, (observedConsoleCounts.get(key) ?? 0) + 1);
  }
  expect(initialNotFoundResponses).toBe(replacedSessionId === null ? 1 : 0);
  expect(intentionalConflictResponses).toBe(1);
  expect(observedConsoleCounts).toEqual(allowedConsoleCounts);
  expect(unexpectedResponses).toEqual([]);
  expect(unexpectedRequestFailures).toEqual([]);
  expect(eventStreamTeardowns).toBe(replacedSessionId === null ? 0 : 1);
  expect(pageErrors).toEqual([]);
});

test("handles risky decisions and manual kernel controls", async ({ page }) => {
  await openSample(page);

  await page.getByLabel("Allow agent edit code cell 2").click();
  await page.getByLabel("Agent instruction").fill("[risk] Exercise the skip path");
  await page.getByRole("button", { name: "Send" }).click();
  let dialog = page.getByRole("alertdialog", { name: "Execution needs approval" });
  await expect(dialog).toBeVisible({ timeout: 45_000 });
  await dialog.getByRole("button", { name: "Skip cell" }).click();
  await waitForTurn(page, /validation incomplete/);
  await undoCellChanges(page, "Source for code cell 2");
  await expect(page.getByLabel("Source for code cell 2")).toContainText("values = [2, 4, 6]");

  await page.getByLabel("Allow agent edit code cell 2").click();
  await page.getByLabel("Agent instruction").fill("[risk] Exercise the cancel path");
  await page.getByRole("button", { name: "Send" }).click();
  dialog = page.getByRole("alertdialog", { name: "Execution needs approval" });
  await expect(dialog).toBeVisible({ timeout: 45_000 });
  await dialog.getByRole("button", { name: "Cancel run" }).click();
  await waitForTurn(page, /cancelled/);

  await replaceEditor(page, "Source for code cell 2", "import time\ntime.sleep(30)\nprint('finished')");
  await page.getByLabel("Save code cell 2").click();
  await expect(page.getByText(/Revision \d+/)).toBeVisible();
  const beforeRestart = await page.request.get(`${backendUrl}/kernel/status`).then((response) => response.json());
  await page.getByLabel("Run code cell 2").click();
  await expect(page.locator(".kernel-state")).toContainText("Kernel busy", { timeout: 15_000 });
  const manualOperationId = await page.request.get(`${backendUrl}/session/status`).then(async (response) => {
    const status = await response.json();
    return status.activeExecution.operationId as string;
  });
  await page.getByLabel("Interrupt kernel").click();
  await expect.poll(async () => {
    const operation = await page.request.get(`${backendUrl}/execution/${manualOperationId}`).then((response) => response.json());
    return operation.state;
  }, { timeout: 15_000 }).toMatch(/failed|cancelled/);
  await expect(page.locator(".kernel-state")).toContainText("Kernel idle", { timeout: 15_000 });

  await page.getByLabel("Restart kernel").click();
  await expect.poll(async () => {
    const status = await page.request.get(`${backendUrl}/kernel/status`).then((response) => response.json());
    return status.kernelSessionId;
  }).not.toBe(beforeRestart.kernelSessionId);
  await expect(page.locator(".kernel-state")).toContainText("Kernel idle");
});

test("closes the active notebook and returns to upload state", async ({ page }) => {
  await openSample(page);

  await page.getByLabel("Close notebook").click();

  await expect(page.getByText("Open a notebook or a folder to begin")).toBeVisible();
  await expect(page.getByLabel("Close notebook")).toHaveCount(0);
  const current = await page.request.get(`${backendUrl}/notebooks/current`);
  expect(current.status()).toBe(404);
});
