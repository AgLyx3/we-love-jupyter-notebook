/** The vendored icon font carries every icon the app draws.
 *
 *  Material Symbols is subsetted (scripts/vendor-fonts.mjs) to keep it at 12 KB
 *  instead of ~400. The price of that is a trap: adding an `<Icon>` without
 *  re-running the script produces a blank box, and only for people who do not
 *  already have the full font cached from some other site. The person who added
 *  it almost certainly does.
 *
 *  `e2e/offline.spec.ts` measures the real glyphs in a real browser, which is
 *  the stronger check — but Playwright is not in CI, and this is the half of
 *  the trap that actually springs. Comparing the two lists needs no browser, so
 *  it runs everywhere `vitest` does.
 */

import { describe, expect, it } from "vitest";

import { iconNames, vendoredIconNames } from "../../scripts/icon-names.mjs";

describe("the vendored fonts", () => {
  it("subset Material Symbols to exactly the icons the app renders", async () => {
    const used = await iconNames();
    expect(used.length).toBeGreaterThan(20);
    // Both directions. Missing means a blank box in the app; extra means an
    // icon nothing uses any more, and a name Google may not have recognised in
    // the first place — it accepts unknown ones without complaint.
    expect(await vendoredIconNames()).toEqual(used);
  });

  it("declare no font that is fetched from somewhere else", async () => {
    const { readFile } = await import("node:fs/promises");
    const { FONTS_CSS } = await import("../../scripts/icon-names.mjs");
    const sheet = await readFile(FONTS_CSS, "utf8");
    const sources = [...sheet.matchAll(/src:\s*url\(([^)]*)\)/g)].map(([, url]) => url.replace(/["']/g, ""));
    expect(sources.length).toBeGreaterThan(0);
    expect(sources.filter((url) => !url.startsWith("./assets/fonts/"))).toEqual([]);
  });
});
