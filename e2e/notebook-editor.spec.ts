import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

const sample = path.resolve("examples/sample.ipynb");
const terminalTurn = /completed|validation incomplete/;

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

async function assertUsableLayout(page: Page) {
  const geometry = await page.evaluate(() => {
    const controls = [...document.querySelectorAll<HTMLElement>("button:not(:disabled), .icon-button:not(.disabled)")];
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
      clippedText,
    };
  });
  expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewport + 1);
  expect(geometry.tinyControls).toEqual([]);
  expect(geometry.clippedText).toEqual([]);
  const notebook = page.getByLabel("Notebook cells");
  await expect(notebook).toBeVisible();
  expect((await notebook.boundingBox())?.height ?? 0).toBeGreaterThan(200);
}

test("edits a notebook through scoped agent and execution workflows", async ({ page }, testInfo) => {
  const consoleErrors: string[] = [];
  const unexpectedResponses: string[] = [];
  page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
  page.on("response", (response) => {
    if (response.status() < 400) return;
    const expectedInitial = response.status() === 404 && (response.url().includes("/api/notebooks/current") || response.url().endsWith("/favicon.ico"));
    const expectedConflict = response.status() === 409 && response.url().includes("/api/cells/downstream/source");
    if (!expectedInitial && !expectedConflict) unexpectedResponses.push(`${response.status()} ${response.url()}`);
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
  await expect(page.getByText("Agent change", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Cell output").filter({ hasText: "Total: 18" })).toBeVisible();
  await page.getByRole("button", { name: "Undo turn" }).click();
  await expect(page.getByLabel("Source for code cell 2")).toContainText("values = [2, 4, 6]");

  await page.getByLabel("Allow agent edit code cell 2").click();
  await page.getByLabel("Add code cell 3 as context").click();
  await page.getByLabel("Agent instruction").fill("[risk] Update values with an environment lookup");
  await page.getByRole("button", { name: "Send" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Execution needs approval" });
  await expect(dialog).toBeVisible({ timeout: 45_000 });
  await expect(dialog).toContainText("Reads environment variables that may contain secrets");
  await dialog.getByRole("button", { name: "Approve and run" }).click();
  await waitForTurn(page, /completed/);
  await expect(page.getByLabel("Cell output").filter({ hasText: "Total: 30" })).toBeVisible();
  await page.getByLabel("Revert agent change to code cell 2").click();
  await expect(page.getByLabel("Source for code cell 2")).toContainText("values = [2, 4, 6]");

  await replaceEditor(page, "Source for code cell 4", "average = total / len(values)\nprint('stale save')");
  await page.route("**/api/cells/downstream/source", async (route) => {
    const snapshot = await page.request.get("http://127.0.0.1:8001/notebooks/current").then((response) => response.json());
    const conflict = await page.request.post("http://127.0.0.1:8001/cells/safe-summary/source", { data: {
      sessionId: snapshot.sessionId,
      expectedDocumentRevision: snapshot.revision,
      source: "total = sum(values)\nprint(f'Total: {total}')\n# external revision\n",
    } });
    expect(conflict.ok()).toBeTruthy();
    await route.continue();
  }, { times: 1 });
  await page.getByLabel("Save code cell 4").click();
  await expect(page.getByRole("alert")).toContainText("Notebook changed elsewhere");
  await expect(page.getByLabel("Source for code cell 4").locator(".cm-content")).not.toContainText("stale save");

  const downloadPromise = page.waitForEvent("download");
  await page.getByLabel("Download notebook").click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("sample.ipynb");

  await assertUsableLayout(page);
  await page.screenshot({ path: testInfo.outputPath("notebook-editor.png"), fullPage: true });
  expect(consoleErrors.filter((message) => !message.startsWith("Failed to load resource:"))).toEqual([]);
  expect(unexpectedResponses).toEqual([]);
});
