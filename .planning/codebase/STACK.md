# Technology Stack — Jupyter Notebook Subsystem (VS Code)

**Analysis Date:** 2026-07-26
**Scope:** `vscode-repo/src/vs/workbench/contrib/notebook/`, `vscode-repo/src/vs/workbench/api/*Notebook*`, `vscode-repo/extensions/ipynb/`, `vscode-repo/extensions/notebook-renderers/`

## Languages

**Primary:**
- TypeScript — all notebook core UI, controller, API, and extension code. Root project uses TS with strict layering enforced by `vscode-repo/build/checker/layersChecker.ts` (browser/common/node/electron-* tsconfigs).

**Secondary:**
- HTML/CSS — generated inline in `vscode-repo/src/vs/workbench/contrib/notebook/browser/view/renderers/backLayerWebView.ts` (`generateContent()`) to build the output webview document; SCSS/CSS in `vscode-repo/src/vs/workbench/contrib/notebook/browser/media/`.
- JavaScript (webview preload bundle) — `vscode-repo/src/vs/workbench/contrib/notebook/browser/view/renderers/webviewPreloads.ts` compiles to a preload script injected into the sandboxed output webview.

## Runtime

**Environment:**
- Desktop: Electron 42.7.0 (`vscode-repo/package.json` devDependencies `"electron": "42.7.0"`). Notebook UI runs in the Electron renderer process (browser layer); extension code (serializers, kernels) runs in an extension host process (Node.js) or a web worker for the web build.
- Node.js 24.x used for extension host and build tooling (`vscode-repo/.nvmrc` → `24.18.0`; `extensions/ipynb/package.json` devDependency `"@types/node": "24.x"`).
- Web: The whole notebook stack also compiles for `vscode.dev`/`github.dev` — browser-only entry points exist for the extension (`extensions/ipynb/src/ipynbMain.browser.ts`) and for workers (`extensions/ipynb/src/notebookSerializerWorker.web.ts`), and the notebook common layer avoids Node APIs (`src/vs/workbench/contrib/notebook/common/services/notebookWebWorker.ts` runs notebook diffing/model work off the main thread via a Web Worker, usable in both desktop and browser).

**Package Manager:**
- npm (root `vscode-repo/package.json`, `package-lock.json`). Each notebook extension has its own lockfile: `extensions/ipynb/package-lock.json`, `extensions/notebook-renderers/package-lock.json`.

## Frameworks / Rendering Approach

**Core UI:**
- No external UI framework — VS Code's own workbench framework (`IWorkbenchContribution`, `Registry`, DI via `@IInstantiationService` decorators). The notebook editor (`src/vs/workbench/contrib/notebook/browser/notebookEditor.ts`, `notebookEditorWidget.ts`) is a hand-rolled virtualized list view (`src/vs/workbench/contrib/notebook/browser/view/notebookCellList.ts`) built on the base `List`/`ListView` widget from `src/vs/base/browser/ui/list/`.
- Code cells are edited with the Monaco editor itself (`ICodeEditor`/`CodeEditorWidget`), one instance per visible cell, e.g. referenced in `src/vs/workbench/contrib/notebook/browser/view/cellParts/markupCell.ts` and `cellStatusPart.ts`.
- Cell/output rendering is split across "cell parts" composable widgets in `src/vs/workbench/contrib/notebook/browser/view/cellParts/` (status bar, toolbar, drag handle, focus indicator, execution order, etc.), assembled by `cellRenderer.ts`.

**Output Rendering (webview):**
- Outputs (rich mimetypes: images, HTML, JS, errors) are rendered inside a single sandboxed `<iframe>`-based webview per notebook, managed by `src/vs/workbench/contrib/notebook/browser/view/renderers/backLayerWebView.ts` using the generic VS Code `IWebviewService`/`IWebviewElement` (`src/vs/workbench/webview/browser/webview.js`), created with `allowScripts: true`.
- The webview document body/preload script is generated at runtime (`generateContent()` + `preloadsScriptStr()` in `webviewPreloads.ts`) and communicates with the main process purely via `postMessage` (see `webviewMessages.ts` for the message type contracts).
- Optional stricter CSP (`notebook.experimental.enableCsp` setting) locks down `script-src`/`style-src`/`img-src` for the output iframe.

**Testing:**
- Mocha (`vscode-repo/package.json` script `test-node`, `test-browser`) for unit tests under `src/vs/workbench/contrib/notebook/test/` (e.g. `test/browser/contrib`, `test/browser/diff`, `test/browser/view`, plus snapshot fixtures in `test/browser/__snapshots__/`).
- `extensions/ipynb/src/test/` and `extensions/notebook-renderers/src/test/` contain extension-level unit tests (serializer round-trip, ANSI/link handling, stack-trace parsing) run via `@vscode/test-cli`/`vscode-test`.

**Build/Dev tooling:**
- Gulp (`vscode-repo/build/gulpfile.*`) drives `compile-extension:ipynb`, `compile-extension:notebook-renderers`, and the general `compile`/`watch` tasks.
- esbuild is used per-extension for the notebook-side bundle: `extensions/ipynb/esbuild.notebook.mts`, `extensions/ipynb/esbuild.browser.mts`, `extensions/notebook-renderers/esbuild.notebook.mts` (bundles the renderer/attachment-renderer JS that runs *inside* the output webview, separate from the extension host bundle).
- TypeScript project references / multiple tsconfigs enforce layering (`build/checker/layersChecker.ts` validates that `common/` notebook code never imports `browser/`-only or `node/`-only modules).

## Key Dependencies

**Critical (extension-host / notebook parsing):**
- `@jupyterlab/nbformat` (`extensions/ipynb/package.json` devDependency `^3.2.9`) — TypeScript types for the Jupyter `.ipynb` notebook-format JSON schema (nbformat v4), used throughout `extensions/ipynb/src/deserializers.ts` and `serializers.ts`.
- `@enonic/fnv-plus` (`^1.3.0`) — fast FNV hashing, used to derive stable ids (e.g. webview backup file names) in `extensions/ipynb/src/notebookSerializer.ts`.
- `detect-indent` (`^6.0.0`) — detects original JSON indentation of `.ipynb` files so re-serialization preserves formatting (`notebookSerializer.ts`).
- `@types/vscode-notebook-renderer` (root devDependency `^1.72.0`, and `extensions/notebook-renderers/package.json` `^1.60.0`) — typings for the renderer-side API (`vscode-notebook-renderer` module) available to code running inside the output webview.
- `jsdom` (`extensions/notebook-renderers/package.json` devDependency `^28.1.0`) — used to unit test HTML-producing renderer helpers (`htmlHelper.ts`) outside a real browser.

**Rendering/formatting helpers (built-in renderer extension):**
- Hand-written ANSI-to-HTML conversion (`extensions/notebook-renderers/src/ansi.ts`, `color.ts`, `colorMap.ts`) — no external ansi library; implements SGR code parsing for stdout/stderr streams.
- `extensions/notebook-renderers/src/linkify.ts` — custom regex-based linkification of file paths/URLs in text output (no external "linkify" package).
- `extensions/notebook-renderers/src/stackTraceHelper.ts` — custom Python traceback parsing/formatting for `application/vnd.code.notebook.error` output.

**Notebook core (workbench) infra reused:**
- `vscode-textmate` (root dependency `^9.3.2`) — tokenization/highlighting used for markdown/code cell rendering consistency with the text editor.
- Monaco editor core (`src/vs/editor/`) — reused directly for the code-cell editors and diff views (`src/vs/workbench/contrib/notebook/browser/diff/notebookDiffEditor.ts`).
- Standard workbench services reused rather than reinvented: `IWebviewService`, `IUndoRedoService`, `IWorkingCopyService` (`src/vs/workbench/services/workingCopy/common/`), `IEditorResolverService`/`RegisteredEditorPriority` for `.ipynb` file association.

**Not used in-tree for kernel execution:**
- No Jupyter kernel/ZeroMQ client library (e.g. `zeromq`, `jupyter-client`) exists anywhere in this repo. Actual kernel connectivity (ZMQ, `jupyter_client` protocol) is implemented by the separate, external "Jupyter" extension (`ms-toolsai.jupyter`, not part of this repo) which plugs into the `NotebookController` API described in INTEGRATIONS.md.

## Configuration

**Notebook-specific settings surface:**
- Contributed via `configuration` sections registered in `src/vs/workbench/contrib/notebook/browser/notebookExtensionPoint.ts` and consumed with `IConfigurationService` throughout the browser layer (e.g. `notebook.experimental.enableCsp`, output line limit/scrolling/word-wrap options read in `backLayerWebView.ts`).
- `.ipynb`-extension-specific settings: `ipynb.pasteImagesAsAttachments.enabled`, `ipynb.experimental.serialization` (`extensions/ipynb/package.json` → `contributes.configuration`).

**Build config:**
- `extensions/ipynb/tsconfig.json`, `tsconfig.browser.json`, and `notebook-src/tsconfig.json` (separate tsconfig for the in-webview attachment renderer bundle, since it targets a DOM-only, no-Node environment).
- `extensions/notebook-renderers/tsconfig.json`.

## Platform Requirements

**Development:**
- Node.js 24.x, npm, and the standard VS Code build toolchain (gulp + esbuild) as declared at repo root; `vscode-repo/build/checker/*` tsconfigs enforce that `common/` notebook code stays platform-agnostic.

**Production / Target platforms:**
- **Desktop** (Electron): full feature set — file system access, extension host in Node.js, native webview.
- **Web** (browser, e.g. vscode.dev/github.dev): supported via browser-specific entry points (`ipynbMain.browser.ts`, `notebookSerializer.web.ts`, `notebookSerializerWorker.web.ts`) and a Web Worker–based notebook diff/compute service (`notebookWebWorker.ts`), with capability declarations `"virtualWorkspaces": true` and `"untrustedWorkspaces": { "supported": true }` in both extension `package.json` files.
- **Remote (server) scenarios** are supported implicitly since the notebook document model, serializer, and kernel APIs all run in the extension host, which is remote-capable by VS Code's general architecture (not notebook-specific code).

---

*Stack analysis: 2026-07-26*
