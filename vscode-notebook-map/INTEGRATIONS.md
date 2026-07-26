# External Integrations — Jupyter Notebook Subsystem (VS Code)

**Analysis Date:** 2026-07-26
**Scope:** `vscode-repo/src/vs/workbench/contrib/notebook/`, `vscode-repo/src/vs/workbench/api/*Notebook*`, `vscode-repo/extensions/ipynb/`, `vscode-repo/extensions/notebook-renderers/`

## Overview: How VS Code Integrates with Jupyter

VS Code core does **not** talk to a Jupyter kernel directly (no ZeroMQ/`jupyter_client` protocol code anywhere in this repo). Instead it defines three seams that a separate extension implements:

1. **`.ipynb` file format** — parsed/serialized by the built-in `ipynb` extension (`extensions/ipynb/`).
2. **`NotebookController` API** — a generic "kernel" abstraction that any extension (in practice the external `ms-toolsai.jupyter` extension) implements to actually run code, using whatever transport it wants (real Jupyter kernels via ZMQ, in-process interpreters, remote kernels, etc.).
3. **Notebook renderer API** — sandboxed webview scripts that turn cell-output mime-types into DOM, contributed by extensions (built-in: `extensions/notebook-renderers/`).

The core VS Code repo owns the document/cell model, the editor UI, the extension-host↔renderer-process RPC, and the output webview messaging; it is deliberately Jupyter-protocol-agnostic.

## The `.ipynb` File Format

**Format ownership:**
- `.ipynb` is registered as notebook type `jupyter-notebook` via `contributes.notebooks` in `extensions/ipynb/package.json`, matched by `filenamePattern": "*.ipynb"`.
- Activation events: `onNotebook:jupyter-notebook`, plus `onNotebookSerializer:interactive` / `onNotebookSerializer:repl` for VS Code's own Interactive Window / REPL editor document types (`INTERACTIVE_WINDOW_EDITOR_ID`, `REPL_EDITOR_ID` in `src/vs/workbench/contrib/notebook/common/notebookCommon.ts`).

**Serialization (extension host, not core):**
- `extensions/ipynb/src/notebookSerializer.ts` — `NotebookSerializerBase` implements `vscode.NotebookSerializer`. `deserializeNotebook()` parses raw `Uint8Array` JSON into a `vscode.NotebookData` object; `serializeNotebook()` converts back to bytes.
- `extensions/ipynb/src/deserializers.ts` (`jupyterNotebookModelToNotebookData`) and `extensions/ipynb/src/serializers.ts` (`serializeNotebookToString`) do the actual nbformat ⇄ `NotebookData` mapping, typed against `@jupyterlab/nbformat` (`import type * as nbformat from '@jupyterlab/nbformat'`).
- `detect-indent` preserves the original file's JSON indentation across round-trips (avoids noisy diffs).
- Guards unsupported formats: throws `Only Jupyter notebooks version 4+ are supported` when `json.nbformat < 4`.
- Platform-specific entry points: `notebookSerializer.node.ts` (desktop, Node fs access) vs `notebookSerializer.web.ts` (browser, uses a Web Worker via `notebookSerializerWorker.web.ts`) — both registered through `registerNotebookSerializer()` (see `src/vscode-dts/vscode.d.ts` line ~14385).
- Webview-backup recovery: if JSON contains `__webview_backup`, the serializer resolves the actual content from `ms-toolsai.jupyter`'s global storage folder (cross-extension coupling with the real Jupyter extension for hot-exit/crash recovery).

**Cell attachments (images pasted into markdown cells):**
- `extensions/ipynb/src/notebookImagePaste.ts` and `notebookAttachmentCleaner.ts` handle the ipynb `attachments` field (base64 images referenced from markdown via `attachment:` URIs).
- Rendered via a dedicated renderer extension entry: `contributes.notebookRenderer` → `vscode.markdown-it-cell-attachment-renderer`, which extends `vscode.markdown-it-renderer` and loads `./notebook-out/cellAttachmentRenderer.js` (compiled from `extensions/ipynb/notebook-src/cellAttachmentRenderer.ts`, a browser/webview-only bundle with its own `tsconfig.json`).

**In-memory document/cell model (core, format-agnostic):**
- `src/vs/workbench/contrib/notebook/common/model/notebookTextModel.ts` — `NotebookTextModel`, analogous to Monaco's `ITextModel` but for a whole notebook (ordered list of cells + metadata), supports undo/redo via `UndoRedoGroup`.
- `src/vs/workbench/contrib/notebook/common/model/notebookCellTextModel.ts` — per-cell model (source text + language + outputs + internal metadata).
- `src/vs/workbench/contrib/notebook/common/model/notebookCellOutputTextModel.ts` — output model backing `ICellOutput`.
- `src/vs/workbench/contrib/notebook/common/notebookCommon.ts` defines the wire-level DTOs: `IOutputItemDto { mime, data: VSBuffer }`, `IOutputDto { outputs, outputId, metadata }`, `CellKind` enum (`Markup = 1`, `Code = 2`), and `NOTEBOOK_DISPLAY_ORDER` / `ACCESSIBLE_NOTEBOOK_DISPLAY_ORDER` (mime-type preference lists used when a cell output has multiple representations, e.g. prefers `application/json` > `text/html` > `image/svg+xml` > `image/png` > plain text).
- URI scheme for addressing individual cells/outputs/metadata as virtual documents: `generate`/`parse`/`generateMetadataUri`/`parseMetadataUri`/`extractCellOutputDetails` in `src/vs/workbench/services/notebook/common/notebookDocumentService.ts` (imported by `notebookCommon.ts`).

## Jupyter Kernels / the `NotebookController` API

**Extension-facing API surface (`vscode-repo/src/vscode-dts/vscode.d.ts`):**
- `vscode.NotebookController` (line ~16043) — extension-implemented "kernel". Key members: `id`, `notebookType`, `label`, `supportedLanguages`, `executeHandler: (cells, notebook, controller) => void | Thenable<void>` (invoked when the user runs a cell/cells), optional `interruptHandler`, `updateNotebookAffinity()`, `onDidChangeSelectedNotebooks`.
- `vscode.window.createNotebookController(id, notebookType, label, handler?)` (line ~16363) creates one; comment in the .d.ts explicitly documents the two-part contract: *"1. `NotebookSerializer` enable the editor to open, show, and save notebooks. 2. `NotebookController` own the execution of notebooks, e.g. they create output from code cells."*
- `vscode.NotebookCellExecution` (line ~16168) — object a controller uses to mutate a cell's outputs/execution state during a run (start/end time, success/failure, replace/append outputs), obtained from `controller.createNotebookCellExecution(cell)`.
- Proposed/experimental extension points (not yet stable API), each in its own `.d.ts`: `vscode.proposed.notebookKernelSource.d.ts` (kernel-picker "source" grouping, e.g. how the Jupyter extension surfaces "Python Environments" vs "Existing Jupyter Server" as pickable sources), `vscode.proposed.notebookExecution.d.ts` / `notebookCellExecution.d.ts`, `vscode.proposed.notebookVariableProvider.d.ts` (variable explorer data), `vscode.proposed.notebookLiveShare.d.ts`, `vscode.proposed.notebookReplDocument.d.ts` (REPL/Interactive Window), `vscode.proposed.contribNotebookStaticPreloads.d.ts` (renderer preload scripts), `vscode.proposed.notebookMessaging.d.ts` (renderer↔extension messaging), `vscode.proposed.notebookMime.d.ts`.

**Core-side kernel bookkeeping (protocol-agnostic — no ZMQ, no Jupyter wire protocol):**
- `src/vs/workbench/contrib/notebook/common/notebookKernelService.ts` / impl `src/vs/workbench/contrib/notebook/browser/services/notebookKernelServiceImpl.ts` — registry of `INotebookKernel`s (an internal DTO, not a real connection), kernel-to-notebook-type matching, "preferred kernel" affinity persistence, and kernel-source-action providers.
- `src/vs/workbench/contrib/notebook/common/notebookExecutionService.ts` / `notebookExecutionStateService.ts` — tracks execution state (`ICellExecutionError`, running/idle) per cell, independent of what actually executes the code.
- `src/vs/workbench/contrib/notebook/browser/contrib/kernelDetection/notebookKernelDetection.ts` — listens for extension activation events prefixed `onNotebook:` and drives lazy activation of kernel-providing extensions per notebook type (so `ms-toolsai.jupyter` is only activated when a `.ipynb` is actually opened).
- `src/vs/workbench/contrib/notebook/browser/contrib/notebookVariables/` — variable-explorer UI consuming `NotebookVariableProvider` data (kernel-supplied variable state, e.g. via Jupyter's `%variables`-style introspection), fully decoupled from any specific kernel implementation.

**Extension-host ⇄ main-process RPC for kernels (the actual protocol boundary in this repo):**
- Proxy identifiers declared in `src/vs/workbench/api/common/extHost.protocol.ts`: `MainContext.MainThreadNotebookKernels` and `ExtHostContext.ExtHostNotebookKernels` (paired with the general `MainThreadNotebook` / `ExtHostNotebook` and `MainThreadNotebookDocuments`/`Editors` identifiers, lines ~4094–4171).
- `src/vs/workbench/api/common/extHostNotebookKernels.ts` — extension-host side; wraps a `vscode.NotebookController` registered by an extension, forwards RPC calls like `$executeCells`/`$cancelCells` from the main thread into the extension's `executeHandler`, and relays `INotebookKernelDto2` kernel metadata to the main thread via `$addKernel`.
- `src/vs/workbench/api/browser/mainThreadNotebookKernels.ts` — main-thread (renderer process) side; `$addKernel(handle, data: INotebookKernelDto2)` registers a kernel proxy with `INotebookKernelService`, `$addKernelDetectionTask` / `$addKernelSourceActionProvider` support the kernel-picker UI. This is the RPC boundary between the extension host process (where an extension like the Jupyter extension actually opens a ZMQ connection to a kernel, entirely outside this repo) and the UI process that shows execution state/outputs.
- Cell execution output updates cross this same RPC boundary as `ICellExecuteUpdateDto` / `NotebookOutputDto` objects (see imports in `extHostNotebookKernels.ts`), then get applied to `NotebookTextModel` and pushed to the output webview.

## Notebook Renderer API (mime-type → DOM)

**Contribution point:**
- Extensions declare renderers via `contributes.notebookRenderer` in `package.json` (`entrypoint`, `mimeTypes`, optional `requiresMessaging`). Example — built-in renderer `extensions/notebook-renderers/package.json`:
  - id `vscode.builtin-renderer`, entrypoint `./renderer-out/index.js`, `requiresMessaging: "never"`.
  - Declared `mimeTypes`: `image/gif`, `image/png`, `image/jpeg`, `image/svg+xml`, `text/html`, `application/javascript`, `application/vnd.code.notebook.error`, `application/vnd.code.notebook.stdout` / `application/x.notebook.stdout` / `application/x.notebook.stream`, `application/vnd.code.notebook.stderr` / `application/x.notebook.stderr`, `text/plain`.
- Renderer JS runs **inside the sandboxed output webview**, not the extension host, and imports the ambient `vscode-notebook-renderer` module (typed by `@types/vscode-notebook-renderer`) to get an `ActivationFunction` (`renderOutputItem(outputItem, element)` / `disposeOutputItem`).
- Built-in renderer implementation: `extensions/notebook-renderers/src/index.ts` (entry/dispatch), `ansi.ts`/`color.ts`/`colorMap.ts` (ANSI SGR → HTML for stdout/stderr), `stackTraceHelper.ts` (Python traceback formatting for the `error` mime type), `linkify.ts` (turns file paths/URLs in text output into clickable links), `htmlHelper.ts`, `textHelper.ts` (output truncation, line limiting).
- Core-side renderer registry/orchestration: `src/vs/workbench/contrib/notebook/common/notebookOutputRenderer.ts` (renderer metadata, mime-type matching, priority) and `src/vs/workbench/api/browser/mainThreadNotebookRenderers.ts` / `src/vs/workbench/api/common/extHostNotebookRenderers.ts` (RPC-registers renderer contributions and forwards renderer↔extension `postMessage` traffic when `requiresMessaging` is enabled).
- Mime-type preference: when a cell output carries multiple mime representations, `NOTEBOOK_DISPLAY_ORDER` (`notebookCommon.ts`) picks which renderer wins by default; users can override per-mime-type renderer choice in the UI.

## Output Webview: Process/Protocol Boundaries

**Renderer-process webview (not extension host):**
- `src/vs/workbench/contrib/notebook/browser/view/renderers/backLayerWebView.ts` creates one `IWebviewElement` per notebook editor via the generic `IWebviewService.createWebviewElement({ ..., allowScripts: true, ... })` (`src/vs/workbench/webview/browser/webview.js` — not notebook-specific).
- `generateContent(baseUrl)` builds the webview's HTML shell inline, embedding a nonce'd `<style>`/`<meta>` CSP tag (opt-in via `notebook.experimental.enableCsp` setting): `default-src 'none'; script-src ... 'unsafe-inline' 'unsafe-eval'; img-src ... https: http: data:; connect-src https:; child-src https: data:;`.
- `webviewPreloads.ts` compiles to the actual JS preload injected into that webview: renderer bootstrapping, output DOM diffing, resize/scroll observation, markdown preview rendering, drag-and-drop between cells — all communicating back to the main thread purely through `postMessage`.

**Message contract (both directions), all defined in `src/vs/workbench/contrib/notebook/browser/view/renderers/webviewMessages.ts`:**
- Webview → main thread: `WebviewInitialized`, `IDimensionMessage` (output resize), `IMouseEnterMessage`/`IMouseLeaveMessage`, `IOutputFocusMessage`/`IOutputBlurMessage`, `IScrollToRevealMessage`/`IScrollAckMessage`, `IClickedDataUrlMessage`, `IClickMarkupCellMessage`, `IClickedLinkMessage`, `IRenderedMarkupMessage`, `IRenderedCellOutputMessage`, `ICustomRendererMessage` (renderer-specific payloads), `ICustomKernelMessage` (kernel↔renderer messaging pass-through).
- Main thread → webview: `ICreationRequestMessage` (create output DOM for `OutputItemEntry[]`), `IClearOutputRequestMessage`/`IClearMessage`, `IHideOutputMessage`/`IShowOutputMessage`, `IUpdateControllerPreloadsMessage`/`IUpdateRenderersMessage` (dynamically add renderer/kernel-preload scripts without recreating the iframe), `IUpdateDecorationsMessage`, `IScrollRequestMessage`/`IViewScrollTopRequestMessage`, `ICreateMarkupCellMessage`/`IDeleteMarkupCellMessage`/`IShowMarkupCellMessage`/`IHideMarkupCellMessage`.
- This message-passing boundary is the **only** channel between untrusted output content (which may include arbitrary HTML/JS from kernel output or a malicious `.ipynb`) and the trusted main thread — the main thread never executes output content directly.

## Extension-Host ⇄ Main-Thread RPC Summary (document/editor lifecycle)

All under `src/vs/workbench/api/`, paired `mainThread*`/`extHost*` files, proxy identifiers in `extHost.protocol.ts`:
- `mainThreadNotebook.ts` / (ext host: `extHostNotebook.ts`) — serializer registration/backup, notebook-type contribution wiring.
- `mainThreadNotebookDocuments.ts` / `extHostNotebookDocuments.ts` — document open/close/dirty/edit events, cell edits.
- `mainThreadNotebookDocumentsAndEditors.ts` — combined document+editor lifecycle sync (added/removed notebooks and their visible editors) pushed to the extension host.
- `mainThreadNotebookEditors.ts` / `extHostNotebookEditors.ts` — editor-level operations (selection, viewColumn, reveal).
- `mainThreadNotebookDto.ts` — DTO (de)serialization helpers shared by the above (`ICellOutput`/`IOutputDto` ⇄ wire format, using `VSBuffer` for binary-safe output payloads).
- `mainThreadNotebookKernels.ts` / `extHostNotebookKernels.ts` — kernel registration and execution (see above).
- `mainThreadNotebookRenderers.ts` / `extHostNotebookRenderers.ts` — renderer messaging registration (see above).
- `mainThreadNotebookSaveParticipant.ts` — hooks extension-contributed save participants (`extHostNotebookDocumentSaveParticipant.ts`) into the save flow (e.g. format-on-save for notebooks).
- `extHostTypes/notebooks.ts` — extension-host-side concrete classes implementing the `vscode.d.ts` notebook types (`NotebookCellOutput`, `NotebookControllerAffinity2`, etc.).

## Environment / Secrets

- No API keys, tokens, or connection secrets for Jupyter live in this repo — kernel connection details (e.g. a remote Jupyter server URL/token) are owned entirely by the external Jupyter extension and stored via its own `SecretStorage`/settings, outside the scope of `vscode-repo`.
- `.env`/credential files: none found under the notebook-scoped directories analyzed.

---

*Integration audit: 2026-07-26*
