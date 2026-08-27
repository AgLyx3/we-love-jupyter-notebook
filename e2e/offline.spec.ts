// The tab on a host that cannot reach the internet.
//
// This is #51 from the other side. `backend/tests/test_bundle_external_origins.py`
// reads the built files and can only see references it knows how to parse; this
// runs the app with every non-loopback request refused at the network layer, so
// anything the page asks for at runtime — a link, an @import, a fetch built out
// of a string — fails the test by existing.
//
// The second half is the part that actually broke. Material Symbols is a
// ligature webfont subsetted to the names this app uses, and the two ways it
// goes wrong look identical from the outside: the font does not load, or it
// loads without the icon someone just added. Both render the ligature's own
// letters. Both look correct to anyone developing with a network and a warm
// cache, which is why neither was noticed.

import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

// Every icon the vendored subset claims to carry — the same list
// frontend/src/fonts.test.ts holds against what the app actually uses, so this
// checks the other half: that the font really has the glyphs it says it has.
import { vendoredIconNames } from "../scripts/icon-names.mjs";

const sample = path.resolve("examples/sample.ipynb");

async function openSample(page: Page) {
  await page.goto("/");
  await page.evaluate(async (notebook) => {
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
      body: JSON.stringify({ path: notebook }),
    });
    await response.text();
  }, sample);
  await page.reload();
  await expect(page.getByText("sample.ipynb", { exact: true })).toBeVisible();
}

test.describe("with nothing reachable but this machine", () => {
  let attempted: string[];

  test.beforeEach(async ({ context, page }) => {
    attempted = [];
    // Refuse rather than drop, so a page that does reach out fails fast instead
    // of holding the test open until its own timeout.
    await context.route("**/*", (route) => {
      const { hostname, protocol } = new URL(route.request().url());
      if (protocol === "data:" || protocol === "blob:" || ["127.0.0.1", "localhost", "::1"].includes(hostname))
        return route.continue();
      attempted.push(route.request().url());
      return route.abort("connectionreset");
    });
    await openSample(page);
  });

  test("asks nothing of any other origin", async () => {
    expect(attempted).toEqual([]);
  });

  test("loads its own fonts", async ({ page }) => {
    // A face the browser never fetched is not in `document.fonts` as loaded,
    // which is what the CDN version looked like here: an empty list.
    const loaded = await page.evaluate(() =>
      [...document.fonts].filter((face) => face.status === "loaded").map((face) => face.family),
    );
    expect(loaded).toContain("Material Symbols Outlined");
    expect(loaded).toContain("Inter");
  });

  test("draws every vendored icon as a glyph, not as its own name", async ({ page }) => {
    const names = await vendoredIconNames();
    expect(names.length).toBeGreaterThan(20);

    // Measured on a canvas rather than on the rendered spans, for two reasons:
    // it covers every icon in the subset instead of the handful this notebook
    // happens to show, and it is a clean signal. A Material Symbols glyph is
    // one em wide, so at 100px a formed ligature measures about 100 and an
    // unformed one measures the width of the word — `keyboard_tab_rtl` in
    // fallback letters is over 700. Nothing lands between.
    const widths = await page.evaluate(async (iconNames) => {
      await document.fonts.load('100px "Material Symbols Outlined"');
      const context = document.createElement("canvas").getContext("2d")!;
      context.font = '100px "Material Symbols Outlined"';
      return Object.fromEntries(iconNames.map((name) => [name, context.measureText(name).width]));
    }, names);

    const notGlyphs = Object.entries(widths).filter(([, width]) => width > 140);
    expect(notGlyphs).toEqual([]);
  });
});
