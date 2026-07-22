import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
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
  await expect.poll(() => editor.evaluate((node) => (node as HTMLElement).innerText)).toBe(source);
}

async function waitForTurn(page: Page, expected: RegExp = terminalTurn) {
  await expect(page.locator(".turn-state")).toHaveText(expected, { timeout: 45_000 });
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
    const expectedEventTeardown = testInfo.project.name === "mobile"
      && replacedSessionId !== null
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
    const expectedInitial = testInfo.project.name === "desktop"
      && phase === "initial"
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
  const uploadFinished = page.waitForResponse((response) => response.url().includes("/api/notebooks/upload") && response.request().method() === "POST");
  await page.locator('input[type="file"]').first().setInputFiles(sample);
  expect((await uploadFinished).ok()).toBeTruthy();
  await expect(page.getByText("sample.ipynb", { exact: true })).toBeVisible();
  await expect(page.locator('input[type="file"]').first()).toBeEnabled();
  phase = "workflow";

  await replaceEditor(page, "Source for code cell 4", "average = total / len(values)\nprint(f'Average: {average}')");
  const manualSave = page.waitForResponse((response) => response.url().includes("/api/cells/downstream/source") && response.request().method() === "POST");
  await page.getByLabel("Save code cell 4").click();
  const manualResponse = await manualSave;
  expect(manualResponse.ok()).toBeTruthy();
  expect((await manualResponse.json()).revision).toBe(1);
  await expect(page.getByText("Revision 1")).toBeVisible();

  await page.getByLabel("Allow agent edit code cell 2").click();
  await page.getByLabel("Add code cell 3 as context").click();
  await expect(page.getByText("1 editable")).toBeVisible();
  await expect(page.getByText("1 context")).toBeVisible();

  await page.getByLabel("Agent instruction").fill("[safe] Update the parameter values");
  await page.getByRole("button", { name: "Send" }).click();
  await waitForTurn(page, /completed/);
  const diff = page.locator(".cell-diff").first();
  await diff.locator("summary").click();
  await expect(diff.locator(".diff-removed")).toContainText("values = [2, 4, 6]");
  await expect(diff.locator(".diff-added")).toContainText("values = [3, 6, 9]");
  await expect(page.getByLabel("Cell output").filter({ hasText: "Total: 18" })).toBeVisible();
  await page.getByRole("button", { name: "Undo turn" }).click();
  await expect(page.getByLabel("Source for code cell 2")).toContainText("values = [2, 4, 6]");

  await page.getByLabel("Allow agent edit code cell 2").click();
  await page.getByLabel("Add code cell 3 as context").click();
  await page.getByLabel("Agent instruction").fill("[risk] Update values with an environment lookup");
  await page.getByRole("button", { name: "Send" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Execution needs approval" });
  await expect(dialog).toBeVisible({ timeout: 45_000 });
  await assertOverlayLayout(page, ".risk-dialog", ".risk-actions button");
  await expect(dialog).toContainText("Reads environment variables that may contain secrets");
  await dialog.getByRole("button", { name: "Approve and run" }).click();
  await waitForTurn(page, /completed/);
  await expect(page.getByLabel("Cell output").filter({ hasText: "Total: 30" })).toBeVisible();
  await page.getByLabel("Revert agent change to code cell 2").click();
  await expect(page.getByLabel("Source for code cell 2")).toContainText("values = [2, 4, 6]");

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
  await expect(page.getByLabel("Source for code cell 4").locator(".cm-content")).not.toContainText("stale save");

  const downloadPromise = page.waitForEvent("download");
  await page.getByLabel("Download notebook").click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("sample.ipynb");
  const downloadedPath = await download.path();
  expect(downloadedPath).not.toBeNull();
  const downloadedText = await readFile(downloadedPath!, "utf8");
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
  expect(initialNotFoundResponses).toBe(testInfo.project.name === "desktop" ? 1 : 0);
  expect(intentionalConflictResponses).toBe(1);
  expect(observedConsoleCounts).toEqual(allowedConsoleCounts);
  expect(unexpectedResponses).toEqual([]);
  expect(unexpectedRequestFailures).toEqual([]);
  expect(eventStreamTeardowns).toBe(testInfo.project.name === "mobile" ? 1 : 0);
  expect(pageErrors).toEqual([]);
});
