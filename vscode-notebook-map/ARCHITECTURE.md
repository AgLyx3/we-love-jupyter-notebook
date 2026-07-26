# Architecture — Jupyter Notebook Subsystem (VS Code)

**Analysis Date:** 2026-07-26
**Scope:** `vscode-repo/src/vs/workbench/contrib/notebook/`, `vscode-repo/src/vs/workbench/api/**/*[Nn]otebook*`, `vscode-repo/extensions/ipynb/`, `vscode-repo/extensions/notebook-renderers/`

## Pattern Overview

**Overall:** A layered Model → ViewModel → View (MVVM-like) architecture, split across two process boundaries:

1. **Main-thread ↔ Extension-host RPC boundary** — the notebook document, kernel, and execution APIs exposed to extensions (e.g. the Python/Jupyter extension) are proxied over an RPC channel (`mainThreadNotebook*.ts` ↔ `extHostNotebook*.ts`). This mirrors the general VS Code extension-host architecture used elsewhere in `src/vs/workbench/api/`.
2. **Renderer isolation boundary** — cell outputs are rendered inside a sandboxed `<webview>` (a separate iframe/process), decoupled from the main renderer process via `postMessage`. Output-rendering code (`notebook-renderers` extension, `webviewPreloads.ts`) never runs in the same context as the notebook's model/view code.

Within the main thread/workbench itself, the pattern is classic VS Code MVVM:
- **Model (`common/model/`)**: source of truth, serialization-agnostic, platform-agnostic (`NotebookTextModel`, `NotebookCellTextModel`).
- **ViewModel (`browser/viewModel/`)**: adds UI-only state (folding, selection, layout, editing state) on top of the model (`NotebookViewModel`, `CodeCellViewModel`, `MarkupCellViewModel`).
- **View (`browser/view/`, `browser/viewParts/`)**: a virtualized list widget (`NotebookCellList`) that renders `ICellViewModel`s into DOM row templates (`CodeCellRenderer`, `MarkupCellRenderer`), plus a single shared webview (`BackLayerWebView`) that hosts all cell outputs.

**Key characteristics:**
- Notebook documents are edited via a single, transactional edit API (`ICellEditOperation[]` applied through `NotebookTextModel.applyEdits`), which is the sole path for all mutations (from UI, from extensions, from undo/redo).
- Cell rendering uses virtualization (`ListView`/`WorkbenchList`) — only visible cell rows are mounted in the DOM; this is why cell templates are pooled/recycled (`notebookCellEditorPool.ts`, `CellPart`/`CellContentPart` composition).
- Outputs are rendered in one long-lived webview per notebook editor (not one per cell), with each output "inset" tracked by id and positioned via absolute offsets computed from the list.
- Kernels are a pluggable abstraction (`INotebookKernel`) — VS Code core has no built-in execution engine; all actual code execution happens in an extension (e.g. Jupyter extension) via `vscode.NotebookController`, relayed through the ext-host RPC layer.
- `.ipynb` file format parsing/serialization lives entirely in the `ipynb` built-in extension, not in core — core only knows the generic `NotebookData` shape.

## Layers

**Common / Model layer:**
- Purpose: Platform-agnostic notebook document model, persisted state, and services usable from both browser and (in principle) other environments (e.g. web workers).
- Location: `vscode-repo/src/vs/workbench/contrib/notebook/common/`
- Contains: `NotebookTextModel`, `NotebookCellTextModel`, cell/document edit types, service interfaces (`INotebookService`, `INotebookKernelService`, `INotebookExecutionStateService`), the notebook editor input/model classes tied into the generic workbench editor+working-copy infrastructure.
- Depends on: `vs/editor/common` (uses `ITextModel` for each cell's text buffer), `vs/platform/*` services (instantiation, undo/redo, files).
- Used by: `browser/` layer (viewModel wraps it), `api/` layer (main-thread proxies read/write it), search/quickdiff/scm integrations elsewhere in the workbench.

**Browser / ViewModel layer:**
- Purpose: Adds editor-only, UI-oriented state around the model: cell selection, folding, layout metrics, per-cell "edit vs. preview" state, find-in-notebook results.
- Location: `vscode-repo/src/vs/workbench/contrib/notebook/browser/viewModel/`
- Contains: `NotebookViewModel` (`notebookViewModelImpl.ts`), `CodeCellViewModel`, `MarkupCellViewModel`, `BaseCellViewModel`, folding model, outline data source.
- Depends on: the common model layer (wraps `NotebookTextModel`/`NotebookCellTextModel` 1:1), `vs/editor/browser` (bulk edits, decorations).
- Used by: the View layer (`NotebookCellList` renders `ICellViewModel`s), toolbar/contribution actions in `browser/contrib` and `browser/controller`.

**Browser / View layer:**
- Purpose: Renders the viewModel into a virtualized DOM list plus a shared output webview; owns the `NotebookEditor`/`NotebookEditorWidget` editor pane.
- Location: `vscode-repo/src/vs/workbench/contrib/notebook/browser/view/`, `.../viewParts/`, `.../notebookEditorWidget.ts`, `.../notebookEditor.ts`
- Contains: `NotebookCellList` (list widget), `NotebookCellListView`/`NotebookCellsLayout` (virtualization/layout math), `cellRenderer.ts` (row template renderers for code/markup cells), `cellParts/*` (composable per-cell UI features: output, toolbars, focus, status bar, drag/drop), `renderers/backLayerWebView.ts` + `renderers/webviewPreloads.ts` (output webview host + injected in-webview runtime), `viewParts/*` (toolbar, sticky scroll, overview ruler, kernel picker UI).
- Depends on: ViewModel layer, `vs/base/browser/ui/list` (generic virtualized list), `vs/workbench/contrib/webview`.
- Used by: `NotebookEditor` (the `EditorPane` registered with the workbench), `browser/contrib/*` feature contributions (find, outline, debug, chat, etc.), `browser/controller/*` command handlers.

**Browser / Controller layer:**
- Purpose: Command/action implementations bound to keybindings, toolbars, and the command palette (run cell, insert cell, cut/copy/paste, fold, chat-in-notebook, etc.).
- Location: `vscode-repo/src/vs/workbench/contrib/notebook/browser/controller/`
- Contains: `executeActions.ts` (run/run-all/interrupt), `insertCellActions.ts`, `editActions.ts`, `cellOperations.ts` (shared cell mutation helpers), `apiActions.ts` (commands the ext-host `vscode.commands` API can call into), `controller/chat/*` (notebook inline-chat actions).
- Depends on: ViewModel + services layer (kernel service, execution service).
- Used by: registered directly with the workbench action/command registries in `notebook.contribution.ts`.

**Browser / Contrib layer:**
- Purpose: Optional/feature-scoped editor contributions attached via `INotebookEditorContribution` (analogous to editor "contrib" pattern in the text editor).
- Location: `vscode-repo/src/vs/workbench/contrib/notebook/browser/contrib/` (subfolders: `find`, `outline`, `debug`, `execute`, `layout`, `kernelDetection`, `notebookVariables`, `saveParticipants`, `undoRedo`, `chat`, etc.)
- Contains: one folder per feature; each typically registers itself via `registerNotebookContribution(...)`.
- Used by: instantiated lazily per `NotebookEditorWidget` instance.

**API / RPC boundary layer:**
- Purpose: Exposes notebook documents, editors, kernels, and renderers to extensions, and relays extension-registered kernels/serializers/renderers back into the main-thread services.
- Location: `vscode-repo/src/vs/workbench/api/browser/mainThreadNotebook*.ts` (main-thread side, runs with full workbench service access) and `vscode-repo/src/vs/workbench/api/common/extHostNotebook*.ts` (extension-host side, runs in a separate process/worker).
- Depends on: common services (`INotebookService`, `INotebookKernelService`, `INotebookExecutionStateService`), `extHost.protocol.ts` shape definitions, `extHostCustomers` registration mechanism.
- Used by: every notebook-providing/consuming extension (`ipynb`, Jupyter, Polyglot Notebooks, etc.) exclusively through the public `vscode.notebooks`/`vscode.workspace.notebookDocuments` API surface — extensions never touch `NotebookTextModel` directly.

**Extension: `ipynb` (`vscode-repo/extensions/ipynb/`):**
- Purpose: Implements the `.ipynb` JSON ⟷ `NotebookData` serializer, cell-attachment (embedded image) handling, and notebook-metadata sync; registers the `jupyter-notebook` view type.
- Location: `vscode-repo/extensions/ipynb/src/`
- Runs as: a normal extension in the extension host (not core), talking to core only via `vscode.workspace.registerNotebookSerializer` and related public APIs.

**Extension: `notebook-renderers` (`vscode-repo/extensions/notebook-renderers/`):**
- Purpose: Provides the built-in output renderers (image, text/ANSI, error/traceback, HTML passthrough) that run **inside the output webview**, not in the extension host.
- Location: `vscode-repo/extensions/notebook-renderers/src/`
- Runs as: JS bundled and loaded by `BackLayerWebView`/`webviewPreloads.ts` directly inside the sandboxed webview iframe, using the `vscode-notebook-renderer` API (`activate(ctx) => ({ renderOutputItem, disposeOutputItem })`).

## Data Flow

**Notebook load flow:**

1. User opens a `.ipynb` file → workbench resolves an editor input via `NotebookEditorInput` (`common/notebookEditorInput.ts`), backed by the generic working-copy/file-editor-model infra (`NotebookEditorModelResolverServiceImpl`, `SimpleNotebookEditorModel` / `NotebookFileWorkingCopyModel` in `common/notebookEditorModel.ts`).
2. `INotebookService.createNotebookTextModel(viewType, uri, stream)` (`common/services/notebookServiceImpl.ts`) resolves the registered serializer for the `viewType` (e.g. `jupyter-notebook`) via `withNotebookDataProvider`.
3. The serializer used is a `SimpleNotebookProviderInfo` wrapping an `INotebookSerializer` that was registered by an extension calling `vscode.workspace.registerNotebookSerializer` → routed over RPC to `MainThreadNotebooks.$registerNotebookSerializer` (`api/browser/mainThreadNotebook.ts`), which itself calls back into the ext-host proxy's `dataToNotebook`/`notebookToData` for the actual bytes ⟷ `NotebookData` conversion (implemented by the `ipynb` extension's `NotebookSerializerBase` in `extensions/ipynb/src/notebookSerializer.ts`).
4. The resulting `NotebookData` (cells + metadata) is used to construct a `NotebookTextModel` (`common/model/notebookTextModel.ts`), which creates one `NotebookCellTextModel` per cell (each cell wraps a lazily-created `ITextModel` for its source).
5. `NotebookEditor`/`NotebookEditorWidget` (`browser/notebookEditor.ts`, `browser/notebookEditorWidget.ts`) constructs a `NotebookViewModel` (`browser/viewModel/notebookViewModelImpl.ts`) over the text model, then hands it to `NotebookCellList` for virtualized rendering.

**State management:**
- All document mutation goes through `NotebookTextModel.applyEdits(edits: ICellEditOperation[], synchronous, ...)`, which validates, applies, fires granular change events (`NotebookCellsChangeType.*`), and records undo/redo elements.
- The `NotebookViewModel` listens to the text model's change events and translates them into view-level events (`INotebookViewCellsUpdateEvent`, layout invalidation) consumed by `NotebookCellList`.
- Cross-cutting UI state (selection, kernel binding, execution state) is centralized in singleton services registered in `notebook.contribution.ts` (`INotebookKernelService`, `INotebookExecutionStateService`, `INotebookEditorService`), not stored on the model — this lets multiple editor instances of the same notebook URI share kernel/execution state.

## Key Abstractions

**`NotebookTextModel`** (`common/model/notebookTextModel.ts`):
- Purpose: The authoritative, serializable notebook document — ordered list of `NotebookCellTextModel`, document metadata, transient-options config (which metadata/output fields are NOT persisted).
- Pattern: Central mutation point (`applyEdits`) + rich change-event stream; implements `INotebookTextModel`.

**`NotebookCellTextModel`** (`common/model/notebookCellTextModel.ts`):
- Purpose: One cell's persisted state — cell kind (code/markup), source text (backed by an `ITextModel`), language, outputs (`ICellOutput`/`NotebookCellOutputTextModel`), metadata/internal metadata (execution order, run state).

**`NotebookViewModel`** (`INotebookViewModel`, impl in `browser/viewModel/notebookViewModelImpl.ts`):
- Purpose: UI-facing wrapper around `NotebookTextModel`: selection state (`NotebookCellSelectionCollection`), folding (`FoldingModel`), find matches, per-cell view models array, layout invalidation events.
- Examples: one instance per open `NotebookEditorWidget`.

**`ICellViewModel` / `CodeCellViewModel` / `MarkupCellViewModel`** (`browser/viewModel/`):
- Purpose: Per-cell UI state — editing vs. preview mode, layout metrics (`CodeCellLayoutInfo`/`MarkupCellLayoutInfo`), output view models (`ICellOutputViewModel`).
- Pattern: One-to-one wrapper of `NotebookCellTextModel`, created/disposed by `NotebookViewModel` as cells are added/removed.

**`INotebookEditor` / `NotebookEditorWidget`** (`browser/notebookBrowser.ts` interface, `browser/notebookEditorWidget.ts` impl):
- Purpose: The full-featured programmatic surface for a notebook editor instance — cell CRUD, layout, decorations, view zones, output creation (`createOutput`), contribution access (`getContribution`).
- Pattern: Very large "God interface" (500+ lines) implemented by one very large class (`NotebookEditorWidget`, ~3350 lines) that composes many smaller collaborators (list, webview, toolbar, kernel picker, sticky scroll).

**`INotebookKernel`** (`common/notebookKernelService.ts`):
- Purpose: Abstraction over "something that can execute cells" — id, supported languages, `executeNotebookCellsRequest`/`cancelNotebookCellExecution`, optional variable provider.
- Pattern: Core never implements this itself; the only implementation in this codebase is `MainThreadKernel` (`api/browser/mainThreadNotebookKernels.ts`), an adapter that forwards every call over RPC to an ext-host-registered `vscode.NotebookController`.

**`INotebookKernelService`** (`common/notebookKernelService.ts`, impl `browser/services/notebookKernelServiceImpl.ts`):
- Purpose: Registry of all available kernels across notebooks; kernel-to-notebook affinity/selection; kernel-source-action providers (the "Select Kernel" quick pick data source).

**`INotebookExecutionStateService`** (`common/notebookExecutionStateService.ts`, impl `browser/services/notebookExecutionStateServiceImpl.ts`):
- Purpose: Tracks in-flight cell/notebook executions (`INotebookCellExecution`, `INotebookExecution`) as first-class disposable objects with `confirm()/update()/complete()`; this is what actually calls `NotebookTextModel.applyEdits` to write execution order, run state, and outputs back into the model as an execution progresses.

**`INotebookService`** (`common/notebookService.ts`, impl `browser/services/notebookServiceImpl.ts`):
- Purpose: Top-level registry: notebook-type (`viewType`) registration, serializer registration, renderer/mimetype registry, and the canonical map of open `NotebookTextModel`s by URI (so multiple editors on the same file share one model).

**`BackLayerWebView<T>`** (`browser/view/renderers/backLayerWebView.ts`):
- Purpose: One per notebook editor; owns the single `<webview>` element that hosts *all* cell outputs, tracks output "insets" by id/cell, and exchanges structured messages (`webviewMessages.ts`) with the in-webview runtime.

## Entry Points

**`notebook.contribution.ts`** (`browser/notebook.contribution.ts`):
- Location: `vscode-repo/src/vs/workbench/contrib/notebook/browser/notebook.contribution.ts`
- Triggers: Loaded at workbench startup as part of contrib registration (imported for side effects from the workbench's contribution index).
- Responsibilities: Registers the `NotebookEditor` editor pane (and diff/output-editor panes) with `IEditorPaneRegistry`, registers every notebook singleton service (`registerSingleton(INotebookService, ...)` etc.), registers workbench-level contributions (`NotebookContribution`, `NotebookEditorManager`, schema/content providers), and wires up configuration/menus/commands. This is the composition root for the whole subsystem.

**`NotebookEditor` / `NotebookEditorWidget`** (`browser/notebookEditor.ts`, `browser/notebookEditorWidget.ts`):
- Triggers: Instantiated by the workbench editor service whenever a `NotebookEditorInput` resolves for a `.ipynb`-associated (or other registered `viewType`) resource.
- Responsibilities: Owns the `NotebookViewModel`, `NotebookCellList`, `BackLayerWebView`, toolbar, kernel picker, and all editor-pane lifecycle (layout, focus, dispose).

**`MainThreadNotebooks` / `ExtHostNotebookController`** (`api/browser/mainThreadNotebook.ts`, `api/common/extHostNotebook.ts`):
- Triggers: Extension host activation of any extension importing `vscode.workspace.registerNotebookSerializer`, `vscode.notebooks.createNotebookController`, etc.
- Responsibilities: The RPC entry points that let extensions plug in serializers, kernels (controllers), renderers, and cell-status-bar providers.

**`extensions/ipynb/src/ipynbMain.ts`**:
- Triggers: Activated when a `.ipynb` file/workspace is opened (or `onNotebook:jupyter-notebook` activation event).
- Responsibilities: Registers the `jupyter-notebook` `NotebookSerializer`, cell-attachment cleanup, notebook-metadata store sync, image paste handling.

**`extensions/notebook-renderers/src/index.ts`**:
- Triggers: Loaded by `BackLayerWebView` inside the output webview whenever a cell output needs a mimetype this renderer declares (`image/png`, `text/plain` w/ ANSI, `application/vnd.code.notebook.error`, etc.), per its `package.json` `contributes.notebookRenderer` entries.
- Responsibilities: `activate()` returns `{ renderOutputItem, disposeOutputItem }` implementing the actual DOM rendering of each output mimetype inside the sandboxed webview.

## Error Handling

**Strategy:** Execution errors are captured as structured data, not thrown across the RPC boundary. `ICellExecutionError` (`common/notebookExecutionStateService.ts`) carries `name`/`message`/`stack` (optionally as parsed `ICellErrorStackFrame[]`)/`location`, set via `execution.complete({ error, lastRunSuccess: false })`. The `error` mimetype output (`application/vnd.code.notebook.error`) is then rendered by `notebook-renderers`'s `stackTraceHelper.ts` inside the webview.

**Patterns:**
- RPC handlers in `mainThreadNotebookKernels.ts` wrap state-service calls in `try { } catch (e) { onUnexpectedError(e); }` so a misbehaving extension kernel can't crash the main thread.
- `$executeCells`/`$cancelCells` on the ext-host side (`extHostNotebookKernels.ts`) similarly catch/log exceptions thrown by extension-provided `executeHandler`/`interruptHandler` callbacks rather than propagating them.
- Model-level invariants (e.g. kernel mismatch, missing notebook) are enforced with thrown `Error`s at the RPC boundary (e.g. `$createExecution` throws if `kernel.selected.id !== controllerId`), caught by the generic RPC dispatch layer.

## Cross-Cutting Concerns

**Logging:** `INotebookLoggingService` (`common/notebookLoggingService.ts`, impl `browser/services/notebookLoggingServiceImpl.ts`) plus direct `ILogService`/`this._logService.trace(...)` calls at RPC boundaries (e.g. `NotebookController[${handle}] EXECUTE cells` traces in `extHostNotebookKernels.ts`) for diagnosing kernel/execution issues. `notebookPerformance.ts` records timing marks (e.g. serializer round-trip `StopWatch`).

**Validation:** Cell edits are validated inside `NotebookTextModel.applyEdits` before being applied/turned into events; kernel/controller compatibility (`supportedLanguages`, selected-kernel identity) is checked in `NotebookExecutionService.executeNotebookCells` and again at the RPC layer (`$createExecution`) as a defense-in-depth check.

**Authentication/Trust:** Cell execution is gated by workspace trust — `NotebookExecutionService.executeNotebookCells` calls `IWorkspaceTrustRequestService.requestWorkspaceTrust(...)` before creating any cell execution, refusing to run code in untrusted workspaces.

## Cell Execution Round-Trip (ASCII Data-Flow Diagram)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ WORKBENCH (main thread / renderer process)                                  │
│                                                                               │
│  User clicks ▶ Run  (browser/controller/executeActions.ts)                  │
│        │                                                                     │
│        ▼                                                                     │
│  NotebookExecutionService.executeNotebookCells(notebook, cells, ctxKeySvc)   │
│  (browser/services/notebookExecutionServiceImpl.ts)                         │
│        │  1. requestWorkspaceTrust()                                        │
│        │  2. INotebookExecutionStateService.createCellExecution(uri, hnd)   │
│        │       → INotebookCellExecution  (state = Pending/Unconfirmed)      │
│        │  3. KernelPickerMRUStrategy.resolveKernel(...)                     │
│        │       → INotebookKernel  (== MainThreadKernel adapter)             │
│        ▼                                                                     │
│  kernel.executeNotebookCellsRequest(notebook.uri, [cellHandle, ...])        │
│  (api/browser/mainThreadNotebookKernels.ts: MainThreadKernel subclass)      │
│        │                                                                     │
│        ▼  RPC call:  _proxy.$executeCells(handle, uri, handles)             │
╞═══════════════════════════════ RPC boundary ════════════════════════════════╡
│ EXTENSION HOST (separate process)                                           │
│                                                                               │
│  ExtHostNotebookKernels.$executeCells(handle, uri, handles)                  │
│  (api/common/extHostNotebookKernels.ts)                                     │
│        │  resolves ExtHostNotebookDocument + vscode.NotebookCell[]          │
│        ▼                                                                     │
│  controller.executeHandler(cells, notebook, controller)   ◄── extension code│
│  (e.g. Jupyter extension's NotebookController, talks to a real kernel/      │
│   Jupyter server/subprocess — entirely outside this repo)                   │
│        │                                                                     │
│        │  extension creates a vscode.NotebookCellExecution and calls:       │
│        │    execution.start() → replaceOutput()/appendOutput() (1..n times) │
│        │                       → execution.end(success, endTime)            │
│        ▼                                                                     │
│  ExtHostCellExecution (in extHostNotebookKernels.ts) translates each call:  │
│    start()          → _proxy.$createExecution(handle, controllerId, uri,    │
│                                                 cellHandle)                  │
│    replaceOutput()  → _proxy.$updateExecution(handle, [outputEdits...])     │
│    end()            → _proxy.$completeExecution(handle, {runEndTime,        │
│                                                  lastRunSuccess, error?})    │
╞═══════════════════════════════ RPC boundary ════════════════════════════════╡
│ WORKBENCH (main thread) — receiving side                                   │
│                                                                               │
│  MainThreadNotebookKernels.$createExecution / $updateExecution /            │
│  $completeExecution  (api/browser/mainThreadNotebookKernels.ts)             │
│        │  looks up INotebookCellExecution by handle, calls:                 │
│        │    execution.confirm() / execution.update(edits) /                │
│        │    execution.complete({...})                                      │
│        ▼                                                                     │
│  NotebookCellExecution impl (browser/services/notebookExecutionStateServiceImpl.ts) │
│        │  translates updates into ICellEditOperation[] (output/metadata)   │
│        ▼                                                                     │
│  NotebookTextModel.applyEdits(edits, ...)  (common/model/notebookTextModel.ts) │
│        │  mutates NotebookCellTextModel outputs + fires change events      │
│        ▼                                                                     │
│  NotebookViewModel (browser/viewModel/notebookViewModelImpl.ts)             │
│        │  relays model change → view-cell update event                     │
│        ▼                                                                     │
│  CellOutputContainer / CellOutputElement (browser/view/cellParts/cellOutput.ts) │
│        │  notebookEditor.createOutput(viewCell, renderResult, offset, ...)  │
│        ▼                                                                     │
│  BackLayerWebView.createOutput(...)  (browser/view/renderers/backLayerWebView.ts) │
│        │  postMessage({type:'html'/'preload', outputId, mimeType, data})   │
╞═════════════════════════════ webview boundary (postMessage) ════════════════╡
│ OUTPUT WEBVIEW (sandboxed iframe, separate context)                        │
│                                                                               │
│  webviewPreloads.ts: window.addEventListener('message', ...)                │
│        │  dispatches to the registered renderer for the output's mimetype  │
│        ▼                                                                     │
│  notebook-renderers extension: activate().renderOutputItem(item, element)  │
│  (extensions/notebook-renderers/src/index.ts)                              │
│        │  renders image / text+ANSI / error+stacktrace / HTML into DOM     │
│        ▼                                                                     │
│  Output pixels visible to the user inside the notebook cell's output area  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

*Architecture analysis: 2026-07-26*
