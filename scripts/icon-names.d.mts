// Types for icon-names.mjs, which stays plain JS because `node
// scripts/vendor-fonts.mjs` has to run without a build step. Only
// frontend/src/fonts.test.ts is typechecked against it — `tsconfig.json`
// includes frontend/src, and e2e/ is transpiled by Playwright.

export declare const ROOT: string;
export declare const SRC: string;
export declare const FONTS_CSS: string;

/** Every Material Symbols ligature the app can render, sorted. */
export declare function iconNames(): Promise<string[]>;

/** The icons `frontend/src/fonts.css` records as vendored, sorted. */
export declare function vendoredIconNames(): Promise<string[]>;
