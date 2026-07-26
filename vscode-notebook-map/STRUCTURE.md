# Structure — Jupyter Notebook Subsystem (VS Code)

**Analysis Date:** 2026-07-26
**Scope:** `vscode-repo/src/vs/workbench/contrib/notebook/`, `vscode-repo/src/vs/workbench/api/**/*notebook*`, `vscode-repo/extensions/ipynb/`, `vscode-repo/extensions/notebook-renderers/`

## Directory Layout

```
vscode-repo/
├── src/vs/workbench/contrib/notebook/
│   ├── common/                          # Model + service interfaces (platform-agnostic)
│   │   ├── model/                       # NotebookTextModel, NotebookCellTextModel
│   │   ├── services/                    # Web-worker-backed diff/matching service
│   │   ├── notebookCommon.ts            # Shared types/enums/events (huge, ~40KB)
│   │   ├── notebookService.ts           # INotebookService interface
│   │   ├── notebookKernelService.ts     # INotebookKernel(Service) interfaces
│   │   ├── notebookExecutionService.ts  # INotebookExecutionService interface
│   │   ├── notebookExecutionStateService.ts # INotebookExecutionStateService interface
│   │   ├── notebookEditorInput.ts       # NotebookEditorInput (editor input type)
│   │   ├── notebookEditorModel.ts       # Working-copy model classes
│   │   ├── notebookEditorModelResolverService(Impl).ts
│   │   ├── notebookDiff*.ts             # Notebook diff editor input/types
│   │   ├── notebookOutputRenderer.ts    # Renderer registration types
│   │   ├── notebookProvider.ts          # NotebookProviderInfo (viewType metadata)
│   │   ├── notebookRange.ts             # ICellRange helpers
│   │   └── notebookContextKeys.ts       # Context-key definitions
│   ├── browser/                         # UI layer (viewModel + view + controllers)
│   │   ├── viewModel/                   # NotebookViewModel + per-kind cell view models
│   │   ├── view/                        # Virtualized list + webview output host
│   │   │   ├── renderers/               # cellRenderer (list row templates), backLayerWebView, webviewPreloads
│   │   │   └── cellParts/               # Composable per-cell UI features
│   │   ├── viewParts/                   # Editor-level chrome (toolbar, sticky scroll, kernel picker)
│   │   ├── controller/                  # Action/command implementations
│   │   │   └── chat/                    # Notebook inline-chat actions
│   │   ├── contrib/                     # Feature-scoped editor contributions (find, outline, debug, ...)
│   │   ├── diff/                        # Notebook diff editor (view + viewModel for diffing)
│   │   ├── services/                    # Impl classes for common/ service interfaces
│   │   ├── outputEditor/                # Standalone "output" editor pane
│   │   ├── media/                       # CSS
│   │   ├── notebookEditor.ts            # EditorPane registered with the workbench
│   │   ├── notebookEditorWidget.ts      # Main editor widget implementation (~3350 lines)
│   │   ├── notebookBrowser.ts           # Core browser-layer interfaces (INotebookEditor, ICellViewModel, ...)
│   │   ├── notebookOptions.ts           # Layout/appearance configuration object
│   │   ├── notebookExtensionPoint.ts    # `notebooks`/`notebookRenderer` contribution-point parsing
│   │   └── notebook.contribution.ts     # Composition root: registers panes, singletons, contributions
│   └── test/browser/                    # ~26 unit test files + test helpers (testNotebookEditor.ts)
│
├── src/vs/workbench/api/
│   ├── common/
│   │   ├── extHostNotebook.ts               # ExtHostNotebookController (serializer registration, viewType data provider)
│   │   ├── extHostNotebookDocument.ts        # ExtHostNotebookDocument / ExtHostCell (ext-host mirror of the model)
│   │   ├── extHostNotebookDocuments.ts       # Applies main→ext document change events
│   │   ├── extHostNotebookDocumentSaveParticipant.ts
│   │   ├── extHostNotebookEditor.ts          # ExtHostNotebookEditor (vscode.NotebookEditor impl)
│   │   ├── extHostNotebookEditors.ts         # Editor-visible-ranges/selections sync
│   │   ├── extHostNotebookKernels.ts         # ExtHostNotebookKernels (vscode.NotebookController impl + execution API)
│   │   ├── extHostNotebookRenderers.ts       # Renderer messaging (postMessage to/from renderer scripts)
│   │   └── extHostTypes/notebooks.ts         # Public API type impls (NotebookCellOutput, NotebookCellData, etc.)
│   └── browser/
│       ├── mainThreadNotebook.ts              # MainThreadNotebooks (serializer + cell-status-bar registration)
│       ├── mainThreadNotebookDocuments.ts     # MainThreadNotebookDocuments (open/save/apply-edits bridge)
│       ├── mainThreadNotebookDocumentsAndEditors.ts # Combined add/remove event dispatcher
│       ├── mainThreadNotebookDto.ts           # NotebookDto: DTO <-> internal model conversion helpers
│       ├── mainThreadNotebookEditors.ts       # MainThreadNotebookEditors (editor open/reveal/decorations)
│       ├── mainThreadNotebookKernels.ts       # MainThreadNotebookKernels (kernel registration + execution RPC)
│       ├── mainThreadNotebookRenderers.ts     # MainThreadNotebookRenderers (renderer messaging bridge)
│       └── mainThreadNotebookSaveParticipant.ts
│
├── extensions/ipynb/                     # Built-in extension: .ipynb <-> NotebookData serializer
│   ├── src/
│   │   ├── ipynbMain.ts / .node.ts / .browser.ts  # Extension activation entry points (per target)
│   │   ├── notebookSerializer.ts / .node.ts / .web.ts  # NotebookSerializerBase + platform variants
│   │   ├── notebookSerializerWorker.ts / .web.ts  # Off-main-thread (de)serialization worker
│   │   ├── serializers.ts / deserializers.ts      # Actual JSON <-> NotebookData mapping logic
│   │   ├── helper.ts / common.ts / constants.ts
│   │   ├── notebookAttachmentCleaner.ts   # Removes unused cell attachments (embedded images) on save
│   │   ├── notebookImagePaste.ts          # Paste-image-as-attachment feature
│   │   └── notebookModelStoreSync.ts      # Keeps in-memory model + metadata store in sync
│   └── notebook-src/cellAttachmentRenderer.ts  # Renders `attachment:` image references (runs in webview)
│
└── extensions/notebook-renderers/        # Built-in extension: output renderer scripts (run inside webview)
    └── src/
        ├── index.ts              # activate(): renderOutputItem/disposeOutputItem entry point
        ├── textHelper.ts         # Plain-text + ANSI-aware output rendering, scroll/truncation
        ├── ansi.ts / color.ts / colorMap.ts  # ANSI escape-code → HTML/CSS conversion
        ├── stackTraceHelper.ts   # Error/traceback formatting
        ├── linkify.ts            # URL/file-path link detection in text output
        └── htmlHelper.ts         # Trusted-types policy + HTML output helpers
```

## Directory Purposes

**`common/model/`:**
- Purpose: The persisted notebook document model.
- Contains: `notebookTextModel.ts` (1397 lines — the document), `notebookCellTextModel.ts` (cell), `notebookCellOutputTextModel.ts` (output), `notebookMetadataTextModel.ts` (doc/cell metadata as `ITextModel` for editing metadata as JSON), `cellEdit.ts` (undo/redo elements for cell moves/inserts).
- Key files: `common/model/notebookTextModel.ts`, `common/model/notebookCellTextModel.ts`.

**`common/services/`:**
- Purpose: A web-worker-hosted service for expensive/background notebook operations (not the same as `browser/services/`).
- Contains: `notebookWebWorker.ts` (worker-side implementation), `notebookWebWorkerMain.ts` (worker entry), `notebookCellMatching.ts` (diff cell-matching algorithm run off the main thread), `notebookWorkerService.ts` (interface).

**`browser/viewModel/`:**
- Purpose: UI-facing per-notebook and per-cell state layered over the common model.
- Key files: `notebookViewModelImpl.ts` (1089 lines, the `NotebookViewModel`), `baseCellViewModel.ts`, `codeCellViewModel.ts`, `markupCellViewModel.ts`, `cellSelectionCollection.ts`, `foldingModel.ts`, `OutlineEntry.ts`/`notebookOutlineDataSource.ts`/`notebookOutlineEntryFactory.ts` (outline/breadcrumbs support).

**`browser/view/`:**
- Purpose: Virtualized rendering of cells and the shared output webview.
- Key files: `notebookCellList.ts` (55KB — the `WorkbenchList`-based virtualized list, `INotebookCellList`), `notebookCellListView.ts` (`NotebookCellsLayout`/`NotebookCellListView`, custom variable-height virtualization), `notebookCellEditorPool.ts` (recycles Monaco editor instances across virtualized rows), `notebookCellAnchor.ts`, `notebookRenderingCommon.ts`.

**`browser/view/renderers/`:**
- Purpose: The output webview host and its in-webview counterpart script.
- Key files: `backLayerWebView.ts` (2012 lines — `BackLayerWebView<T>`, owns the `<webview>` element, output-inset lifecycle, message dispatch), `webviewPreloads.ts` (3186 lines — the actual JS injected *into* the webview iframe; runs in the sandboxed context, handles DOM events, output resize, scroll sync, renderer loading), `webviewMessages.ts` (shared TS types for every main↔webview postMessage payload), `cellRenderer.ts` (`NotebookCellListDelegate`, `MarkupCellRenderer`, `CodeCellRenderer` — the list's row template renderers), `webviewThemeMapping.ts` (maps editor theme colors into CSS variables sent to the webview).

**`browser/view/cellParts/`:**
- Purpose: Composable, per-cell UI "parts" attached to each rendered cell row (mirrors editor contribution composition pattern at cell granularity).
- Key files: `cellOutput.ts` (`CellOutputContainer`/`CellOutputElement` — drives `BackLayerWebView.createOutput`), `codeCell.ts`/`markupCell.ts` (top-level per-kind part orchestrators), `cellFocus.ts`, `cellToolbars.ts`, `cellDnd.ts` (drag/drop reordering), `cellExecution.ts` (run-state UI), `chat/` (inline-chat cell part).
- Base class: `browser/view/cellPart.ts` (`CellPart`/`CellContentPart`).

**`browser/viewParts/`:**
- Purpose: Editor-widget-level (not per-cell) chrome.
- Key files: `notebookEditorToolbar.ts`, `notebookKernelView.ts` + `notebookKernelQuickPickStrategy.ts` (kernel picker UI/logic, `KernelPickerMRUStrategy`), `notebookOverviewRuler.ts`, `notebookEditorStickyScroll.ts`, `notebookViewZones.ts`, `notebookEditorWidgetContextKeys.ts`.

**`browser/controller/`:**
- Purpose: `Action2`/command registrations for all user-facing notebook commands.
- Key files: `executeActions.ts` (run/run-all/run-above/below, interrupt), `insertCellActions.ts`, `editActions.ts`, `cellOperations.ts` (shared helper functions used by multiple actions — move/split/join/change-kind), `apiActions.ts` (commands invoked via the extension `vscode.commands.executeCommand` surface), `foldingController.ts`, `sectionActions.ts`, `variablesActions.ts`, `chat/` (inline-chat command actions).

**`browser/contrib/`:**
- Purpose: One subfolder per optional editor feature, each following the `INotebookEditorContribution` pattern (parallel to `vs/editor/contrib`).
- Subfolders: `find/`, `outline/`, `debug/`, `execute/`, `layout/`, `kernelDetection/`, `notebookVariables/`, `saveParticipants/`, `undoRedo/`, `chat/`, `cellCommands/`, `cellDiagnostics/`, `cellStatusBar/`, `clipboard/`, `editorHint/`, `editorStatusBar/`, `format/`, `gettingStarted/`, `marker/`, `multicursor/`, `navigation/`, `profile/`, `troubleshoot/`, `viewportWarmup/`.

**`browser/services/`:**
- Purpose: Browser-side implementations of the interfaces declared in `common/`.
- Key files: `notebookServiceImpl.ts` (40KB — `INotebookService` impl, the registry of open documents/viewTypes/renderers), `notebookKernelServiceImpl.ts`, `notebookExecutionServiceImpl.ts` (the "run cells" orchestration described in ARCHITECTURE.md), `notebookExecutionStateServiceImpl.ts` (22KB — tracks in-flight executions, applies output/state edits to the model), `notebookEditorServiceImpl.ts`, `notebookKernelHistoryServiceImpl.ts`, `notebookWorkerServiceImpl.ts` (main-thread proxy to `common/services/notebookWebWorker.ts`).

**`browser/diff/`:**
- Purpose: The notebook diff editor (git diff / compare view for `.ipynb` files), a parallel view/viewModel stack specific to diffing.
- Key files: `notebookDiffEditor.ts`, `notebookDiffViewModel.ts`, `diffElementViewModel.ts`, `notebookMultiDiffEditor.ts` (multi-file diff variant), `inlineDiff/` subfolder.

**`browser/outputEditor/`:**
- Purpose: A standalone editor pane for viewing/editing a single cell's raw output (opened via "Open Output in New Editor" style commands).
- Key files: `notebookOutputEditor.ts`, `notebookOutputEditorInput.ts`.

**`api/browser/mainThread*.ts` and `api/common/extHost*.ts`:**
- Purpose: The RPC boundary — every extension-visible notebook capability has a matching `mainThreadNotebookX.ts` (runs with full main-thread service access, `@extHostNamedCustomer`) and `extHostNotebookX.ts` (runs in the extension host, implements the `vscode.*` public API surface).
- Key files: see full list under Naming Conventions below.

**`extensions/ipynb/`:**
- Purpose: The only place in the codebase that understands the physical `.ipynb` JSON format.
- Key files: `src/serializers.ts`/`src/deserializers.ts` (JSON ⟷ `NotebookData` mapping), `src/notebookSerializer.ts` (`NotebookSerializerBase`, wires the above into `vscode.NotebookSerializer`), `src/notebookSerializerWorker.ts` (heavy parsing off the main extension-host thread).

**`extensions/notebook-renderers/`:**
- Purpose: The default output renderers, executed inside the sandboxed webview (never in the extension host or main thread).
- Key files: `src/index.ts` (renderer entry point), `src/textHelper.ts` + `src/ansi.ts` (text/ANSI output), `src/stackTraceHelper.ts` (error output).

## Key File Locations

**Entry Points:**
- `vscode-repo/src/vs/workbench/contrib/notebook/browser/notebook.contribution.ts`: composition root — editor pane registration, singleton service registration, workbench contributions.
- `vscode-repo/src/vs/workbench/contrib/notebook/browser/notebookEditor.ts`: the `EditorPane` class registered with the workbench editor registry.
- `vscode-repo/extensions/ipynb/src/ipynbMain.ts` (+ `.node.ts`/`.browser.ts`): extension activation entry points for the `.ipynb` serializer extension.
- `vscode-repo/extensions/notebook-renderers/src/index.ts`: webview-side renderer activation entry point.

**Configuration:**
- `vscode-repo/src/vs/workbench/contrib/notebook/browser/notebookOptions.ts` (33KB): all notebook layout/appearance configuration reads (cell padding, toolbar visibility, font, etc.), consumed by the view layer.
- `vscode-repo/src/vs/workbench/contrib/notebook/browser/notebookExtensionPoint.ts`: parses the `contributes.notebooks` and `contributes.notebookRenderer` `package.json` extension points.
- `vscode-repo/extensions/notebook-renderers/package.json`: declares built-in renderer mimetype ownership (`contributes.notebookRenderer[].mimeTypes`).

**Core Logic:**
- `vscode-repo/src/vs/workbench/contrib/notebook/common/model/notebookTextModel.ts`: document mutation (`applyEdits`) and change-event source of truth.
- `vscode-repo/src/vs/workbench/contrib/notebook/browser/services/notebookExecutionServiceImpl.ts`: "run cells" orchestration (trust check → create execution → resolve kernel → dispatch).
- `vscode-repo/src/vs/workbench/contrib/notebook/browser/services/notebookExecutionStateServiceImpl.ts`: in-flight execution tracking + writing kernel output/state updates back into the model.
- `vscode-repo/src/vs/workbench/api/browser/mainThreadNotebookKernels.ts` / `vscode-repo/src/vs/workbench/api/common/extHostNotebookKernels.ts`: the kernel RPC bridge (see ARCHITECTURE.md diagram).

**Testing:**
- `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/`: ~26 unit test files (e.g. `notebookTextModel.test.ts`, `notebookViewModel.test.ts`, `notebookExecutionService.test.ts`, `notebookKernelService.test.ts`).
- `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/testNotebookEditor.ts`: shared test harness/fixture builder for constructing a fake `NotebookEditor` + model in unit tests.
- `vscode-repo/src/vs/workbench/api/test/browser/extHostNotebook.test.ts`, `extHostNotebookKernel.test.ts`, `TestMainThreadNotebookKernels.ts`: RPC-layer tests with a fake main-thread kernel counterpart.
- `vscode-repo/extensions/ipynb/src/test/`: serializer round-trip tests (`serializers.test.ts`), `clearOutputs.test.ts`, `notebookModelStoreSync.test.ts`.
- `vscode-repo/extensions/notebook-renderers/src/test/`: `linkify.test.ts`, `notebookRenderer.test.ts`, `stackTraceHelper.test.ts`.

## Naming Conventions

**Files:**
- `*Impl.ts` in `browser/services/`: the concrete implementation class for an interface of the same base name declared in `common/` (e.g. `notebookKernelService.ts` interface ↔ `notebookKernelServiceImpl.ts` impl). This mirrors the standard VS Code service pattern (interface in `common`, platform-specific impl in `browser`/`node`/`electron-*`).
- `mainThreadNotebook*.ts` (in `api/browser/`): main-process side of an RPC pair; always implements a `MainThread*Shape` interface and is registered via `@extHostNamedCustomer(MainContext.MainThread*)`.
- `extHostNotebook*.ts` (in `api/common/`): extension-host side of the same RPC pair; implements an `ExtHost*Shape` interface and is instantiated per extension-host process.
- `*.node.ts` / `*.browser.ts` / `*.web.ts` suffixes (in `extensions/ipynb/src/`): platform-specific entry points/variants for the same logical module (desktop/Node vs. browser/web builds), selected at build/bundling time.
- `*.contribution.ts`: a file whose only job is side-effect registration (editor panes, singleton services, commands, menus) — always imported purely for its side effects, never for exported symbols.
- `*ViewModel.ts` / `*TextModel.ts`: naming directly encodes which architectural layer (view vs. model) a class belongs to.
- `I*` prefix: interface (VS Code-wide convention), e.g. `INotebookEditor`, `INotebookKernel`, `INotebookService` — paired with a `createDecorator<I...>('...')` call for DI.

**Directories:**
- `contrib/`: one subdirectory per optional feature, each self-registering — do not put core/required logic here.
- `viewModel/` vs `view/` vs `viewParts/`: `viewModel` = data/state only (no DOM), `view` = the cell list + per-cell DOM composition, `viewParts` = editor-chrome DOM pieces that aren't per-cell.
- `common/` vs `browser/`: `common` must stay environment-agnostic (no direct DOM/webview access); anything touching `document`/`window`/webview APIs belongs in `browser/`.

## Where to Add New Code

**New notebook document/model feature** (e.g. a new cell metadata field):
- Type/edit-op definitions: `vscode-repo/src/vs/workbench/contrib/notebook/common/notebookCommon.ts`
- Model logic: `vscode-repo/src/vs/workbench/contrib/notebook/common/model/notebookTextModel.ts` or `notebookCellTextModel.ts`
- Extension-facing exposure (if extensions need to read/write it): add DTO conversion in `vscode-repo/src/vs/workbench/api/browser/mainThreadNotebookDto.ts` and surface through `mainThreadNotebookDocuments.ts` / `extHostNotebookDocument.ts`.
- Tests: `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/notebookTextModel.test.ts`.

**New per-cell UI feature** (e.g. a new inline cell button or badge):
- Implementation: new file under `vscode-repo/src/vs/workbench/contrib/notebook/browser/view/cellParts/`, extending `CellContentPart`/`CellPart` (`browser/view/cellPart.ts`), then wire into `codeCell.ts`/`markupCell.ts` part composition.
- If it needs a command/keybinding: add an action in `vscode-repo/src/vs/workbench/contrib/notebook/browser/controller/`.

**New optional editor feature (contribution)** (e.g. a new panel/decoration that's not always active):
- New subfolder under `vscode-repo/src/vs/workbench/contrib/notebook/browser/contrib/`, following the pattern of an existing feature like `browser/contrib/outline/` or `browser/contrib/find/`; register via the contribution registry called from `notebook.contribution.ts`.

**New output renderer / mimetype support:**
- If it's a first-party built-in renderer: add to `vscode-repo/extensions/notebook-renderers/src/` and declare the mimetype in `vscode-repo/extensions/notebook-renderers/package.json` under `contributes.notebookRenderer`.
- If it's the generic renderer-hosting infra (main-thread side): `vscode-repo/src/vs/workbench/api/browser/mainThreadNotebookRenderers.ts` / `vscode-repo/src/vs/workbench/contrib/notebook/browser/view/renderers/backLayerWebView.ts`.

**New kernel-related capability** (e.g. new metadata a controller can report):
- Interface: `vscode-repo/src/vs/workbench/contrib/notebook/common/notebookKernelService.ts`
- Main-thread adapter: `vscode-repo/src/vs/workbench/api/browser/mainThreadNotebookKernels.ts` (the `MainThreadKernel` class)
- Ext-host public API surface: `vscode-repo/src/vs/workbench/api/common/extHostNotebookKernels.ts`
- Public `vscode.d.ts` typings live outside this scope (in `vscode-repo/src/vscode-dts/`).

**New `.ipynb` serialization behavior** (e.g. new metadata round-tripping):
- `vscode-repo/extensions/ipynb/src/serializers.ts` / `deserializers.ts`; add/extend a test in `vscode-repo/extensions/ipynb/src/test/serializers.test.ts`.

**Utilities:**
- Shared cell-mutation helpers: `vscode-repo/src/vs/workbench/contrib/notebook/browser/controller/cellOperations.ts`
- Shared range helpers: `vscode-repo/src/vs/workbench/contrib/notebook/common/notebookRange.ts`

## Special Directories

**`browser/media/`:**
- Purpose: CSS for the notebook editor DOM (not the webview — webview styling lives inline/in `webviewPreloads.ts` and the renderer extensions).
- Generated: No
- Committed: Yes

**`test/browser/`:**
- Purpose: Unit tests using the standard VS Code Mocha-based test runner and a hand-built fake editor harness (`testNotebookEditor.ts`) rather than a full DOM/Electron instance.
- Generated: No
- Committed: Yes

**`extensions/ipynb/notebook-src/`:**
- Purpose: A separate small TypeScript project (own `tsconfig.json`) compiled specifically to run inside the notebook/webview context (`cellAttachmentRenderer.ts`), distinct from the main extension-host `src/` sources.
- Generated: build output (`renderer-out`-style dirs, not shown here) is generated; source is committed.

---

*Structure analysis: 2026-07-26*
