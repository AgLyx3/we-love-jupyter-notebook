import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

const sample = path.resolve("examples/sample.ipynb");
const terminalTurn = /completed|validation incomplete/;
const backendUrl = `http://127.0.0.1:${process.env.E2E_BACKEND_PORT ?? "8001"}`;

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
  const consoleErrors: string[] = [];
  const unexpectedResponses: string[] = [];
  const allowedConsoleCounts = new Map<string, number>();
  const unexpectedRequestFailures: string[] = [];
  let eventStreamTeardowns = 0;
  page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
  page.on("requestfailed", (request) => {
    const failure = request.failure()?.errorText ?? "unknown failure";
    if (request.url().includes("/api/events?") && failure === "net::ERR_ABORTED") {
      eventStreamTeardowns += 1;
      return;
    }
    unexpectedRequestFailures.push(`${request.url()} ${failure}`);
  });
  page.on("response", (response) => {
    if (response.status() < 400) return;
    const expectedInitial = response.status() === 404 && response.url().includes("/api/notebooks/current");
    const expectedConflict = response.status() === 409 && response.url().includes("/api/cells/downstream/source");
    if (!expectedInitial && !expectedConflict) {
      unexpectedResponses.push(`${response.status()} ${response.url()}`);
      return;
    }
    const statusText = response.status() === 404 ? "Not Found" : "Conflict";
    const message = `Failed to load resource: the server responded with a status of ${response.status()} (${statusText})`;
    allowedConsoleCounts.set(message, (allowedConsoleCounts.get(message) ?? 0) + 1);
  });
  await page.goto("/");
  const uploadFinished = page.waitForResponse((response) => response.url().includes("/api/notebooks/upload") && response.request().method() === "POST");
  await page.locator('input[type="file"]').first().setInputFiles(sample);
  expect((await uploadFinished).ok()).toBeTruthy();
  await expect(page.getByText("sample.ipynb", { exact: true })).toBeVisible();
  await expect(page.locator('input[type="file"]').first()).toBeEnabled();

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
  await page.getByLabel("Save code cell 4").click();
  await expect(page.getByRole("alert")).toContainText("Notebook changed elsewhere");
  await assertOverlayLayout(page, ".notice", "button");
  await expect(page.getByLabel("Source for code cell 4").locator(".cm-content")).not.toContainText("stale save");

  const downloadPromise = page.waitForEvent("download");
  await page.getByLabel("Download notebook").click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("sample.ipynb");

  await assertUsableLayout(page);
  await page.screenshot({ path: testInfo.outputPath("notebook-editor.png"), fullPage: true });
  const observedConsoleCounts = new Map<string, number>();
  for (const message of consoleErrors) observedConsoleCounts.set(message, (observedConsoleCounts.get(message) ?? 0) + 1);
  for (const [message, count] of observedConsoleCounts) expect(count).toBeLessThanOrEqual(allowedConsoleCounts.get(message) ?? 0);
  expect(unexpectedResponses).toEqual([]);
  expect(unexpectedRequestFailures).toEqual([]);
  expect(eventStreamTeardowns).toBeLessThanOrEqual(1);
});
