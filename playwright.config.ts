import { defineConfig, devices } from "@playwright/test";
import fs from "node:fs";

const backendPort = process.env.E2E_BACKEND_PORT ?? "8001";
const frontendPort = process.env.E2E_FRONTEND_PORT ?? "5174";
// scripts/dev.py falls back to the interpreter running it when .venv is
// missing; mirror that here. Hardcoding .venv/bin/python leaves a fresh
// worktree with no virtualenv unable to start the webServer at all, and every
// spec then times out with nothing pointing at the cause. Repo-root relative,
// like the rest of the command.
const python = fs.existsSync(".venv/bin/python") ? ".venv/bin/python" : "python3";

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "test-results/playwright-report", open: "never" }]],
  outputDir: "test-results/playwright-artifacts",
  use: {
    baseURL: `http://127.0.0.1:${frontendPort}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "mobile", use: { ...devices["Pixel 5"], viewport: { width: 390, height: 844 } } },
  ],
  webServer: {
    command: `exec ${python} scripts/dev.py --test-agent --backend-port ${backendPort} --frontend-port ${frontendPort}`,
    url: `http://127.0.0.1:${frontendPort}`,
    reuseExistingServer: false,
    timeout: 30_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
  },
});
