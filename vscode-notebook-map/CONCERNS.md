# Codebase Concerns — Jupyter Notebook Subsystem (VS Code)

**Analysis Date:** 2026-07-26
**Scope:** `vscode-repo/src/vs/workbench/contrib/notebook/`, `vscode-repo/src/vs/workbench/api/{browser,common}/*Notebook*.ts`, `vscode-repo/extensions/ipynb/`, `vscode-repo/extensions/notebook-renderers/`

This document is written for someone building an AI adapter that reads/edits notebook cells and reasons about cell outputs/errors, layered on top of this code (either as a VS Code extension using the notebook API, or by directly touching the workbench internals).

## Tech Debt

**God-object notebook editor widget:**
- Issue: `notebookEditorWidget.ts` is 3351 lines with 104 `import` statements and owns webview lifecycle, cell list, kernel selection, toolbars, view-zone layout, find/replace wiring, drag/drop, and viewport warmup orchestration in one class.
- Files: `src/vs/workbench/contrib/notebook/browser/notebookEditorWidget.ts`
- Impact: Any adapter that needs to hook into "when did the editor really finish loading/rendering" or "how do I get a stable reference to the currently visible cells" has to reverse-engineer a very large surface. High risk of depending on incidental behavior that changes between VS Code versions.
- Fix approach: Not something an adapter author can fix; treat this file as a black box and prefer the public `vscode.NotebookEditor` / `vscode.NotebookDocument` extension API (`extHostNotebookDocument.ts`, `extHostNotebookEditor.ts`) rather than reaching into workbench internals.

**Webview renderer bundle is a second monolith:**
- Issue: `webviewPreloads.ts` (3186 lines) and `backLayerWebView.ts` (2012 lines) implement the entire in-webview runtime (output DOM creation, renderer script loading/activation, markdown preview, resize/ARIA/scroll observers, message routing) as effectively one large closure-based module with minimal internal typing boundaries.
- Files: `src/vs/workbench/contrib/notebook/browser/view/renderers/webviewPreloads.ts`, `src/vs/workbench/contrib/notebook/browser/view/renderers/backLayerWebView.ts`
- Impact: Output rendering behavior (how a given mimetype becomes DOM, how resize is reported back) is deeply intertwined with rendering performance hacks; adapters cannot easily intercept "raw output before DOM rendering" from inside the webview — they must go through the extension host's `NotebookDocument.cellAt(i).outputs` instead (see RPC boundary section below).
- Fix approach: N/A for adapter authors; avoid trying to inject code into the webview context directly. Use `NotebookRendererScript`/`onDidReceiveMessage` (see `vscode.proposed.notebookMessaging.d.ts`) if you must exchange data with the webview.

**Diff/compare view is a large parallel implementation:**
- Files: `src/vs/workbench/contrib/notebook/browser/diff/diffComponents.ts` (2191 lines), `.../diff/notebookDiffEditor.ts` (1023 lines), `.../diff/diffElementViewModel.ts` (1103 lines), `.../diff/notebookDiffActions.ts` (751 lines)
- Impact: If an adapter wants to show "AI proposed diff" for notebook edits, this is the only built-in diff renderer, but it is a separate view/viewModel hierarchy from the live editor — proposed edits are not simply "preview cells in the live model."
- Note: `notebookCellDiffDecorator.ts:26` has an explicit TODO: `// TODO: allow client to set read-only - chateditsession should set read-only while making changes` — the diff/decorator layer already anticipates chat/agent-driven edits but the read-only-during-edit story is unfinished.

**Metadata/model desync workaround (`ipynb` extension):**
- Issue: `notebookModelStoreSync.ts` exists specifically because the in-memory notebook model and the serialized `.ipynb` JSON diverge (e.g., new cells have `undefined` metadata in-memory but become `{}` after a save round-trip). The extension listens to `workspace.onDidChangeNotebookDocument` and asynchronously re-applies `WorkspaceEdit`s to normalize metadata, debounced via a merged-event timer (`mergedEvents`/`timer` module-level state).
- Files: `extensions/ipynb/src/notebookModelStoreSync.ts`, `extensions/ipynb/src/serializers.ts`
- Impact: **Directly relevant to an AI adapter that edits cells.** After the adapter applies a `WorkspaceEdit` to add/edit a cell, a second, asynchronous, debounced edit may land shortly after (normalizing metadata) that the adapter did not initiate. If the adapter reads back cell state immediately after its own edit (to confirm/quote back to the model), it may observe pre-normalization state; if it races a save (`onWillSaveNotebookDocument` waits via `pendingNotebookCellModelUpdates`), timing bugs are possible. Any code that also listens to `onDidChangeNotebookDocument` will see extra change events not caused by the user or the adapter.
- Fix approach: When building the adapter, do not assume a 1:1 mapping between "edit I applied" and "change events observed." Debounce/coalesce your own listener, and prefer diffing final cell content rather than trusting a single change event.

**CSP for the notebook output webview is opt-in, not default:**
- Issue: In `backLayerWebView.ts`, the `<meta http-equiv="Content-Security-Policy">` tag is only emitted `if (enableCsp)`, gated by the experimental setting `notebook.experimental.enableCsp` (default off in this build).
- File: `src/vs/workbench/contrib/notebook/browser/view/renderers/backLayerWebView.ts:311-329`
- Impact: By default, script/style/connect sources inside the output webview are unrestricted by CSP (though the webview `<iframe>`/host sandboxing still applies). Third-party renderer scripts and cell outputs (e.g., raw HTML/JS output) execute with fewer restrictions than a "secure by default" reading of the architecture would suggest. Relevant to any adapter that renders or forwards notebook output HTML into another surface (e.g., quoting an HTML output into a chat panel) — do not assume CSP has sanitized it.

## Known Bugs / Rough Edges

**Multi-cursor decoration leak acknowledged in-code:**
- Symptoms: Decorations can leak across cells during rapid selection updates.
- Files: `src/vs/workbench/contrib/notebook/browser/contrib/multicursor/notebookMulticursor.ts:257` (`this.cursorsDisposables.clear(); // TODO: dial this back for perf and just update the relevant controllers`), `:728` (`this.clearDecorations(trackedMatch); // need this to avoid leaking decorations -- TODO: just optimize the lazy decorations fn`)
- Trigger: Multi-cursor/multi-select editing across many cells.
- Relevance: Not directly on the read/output path, but signals that bulk cell-selection state is fragile; an adapter that programmatically selects/highlights many cells (e.g., to show "these are the cells I changed") should test against large selections.

**Markdown-cell rendering disabled in viewport warmup for accessibility, with commented-out code:**
- Files: `src/vs/workbench/contrib/notebook/browser/contrib/viewportWarmup/viewportWarmup.ts:53` (`// TODO@rebornix currently we disable markdown cell rendering in webview for accessibility` followed by a commented-out `this._notebookEditor.createMarkupPreview(cell);` call)
- Impact: Markdown cell preview warmup is effectively a no-op path today; adapters relying on markdown preview being pre-rendered before a cell scrolls into view should not assume it happens eagerly.

**Diff editor height calculation has a known gap:**
- File: `src/vs/workbench/contrib/notebook/browser/diff/editorHeightCalculator.ts:62` — `// TODO: When we have a horizontal scrollbar, we need to add 12 to the height.`
- Impact: Minor layout-only bug; relevant only if building on the diff view.

**Output diff rendering has no explicit limit yet:**
- File: `src/vs/workbench/contrib/notebook/browser/diff/diffElementOutputs.ts:290` — `// TODO, outputs to render (should have a limit)`
- Impact: Combined with the general output-count concerns below, diffing two notebooks that both have many/large outputs can render everything with no cap, unlike the live editor's `outputDisplayLimit`.

## Security Considerations

**Arbitrary output HTML/JS executes in the notebook webview:**
- Risk: Cell outputs (`text/html`, `application/javascript`, custom mimetypes via renderer extensions) are rendered inside a dedicated webview (`backLayerWebView.ts` builds the HTML document, `webviewPreloads.ts` is the in-page runtime that creates DOM per output). This is standard Jupyter behavior (kernels can emit arbitrary HTML/JS), but it means the "output" your adapter reads may itself be live, executable content, not inert text.
- Files: `src/vs/workbench/contrib/notebook/browser/view/renderers/backLayerWebView.ts`, `src/vs/workbench/contrib/notebook/browser/view/renderers/webviewPreloads.ts`
- Current mitigation: Standard VS Code webview isolation (separate origin, `asWebviewUri` remapping, `webviewGenericCspSource`), and CSP is available but **off by default** (see above).
- Recommendation for the adapter: When "reading" an output to feed an LLM, read the underlying `NotebookCellOutput`/`NotebookCellOutputItem` bytes/mime from the extension-host model (`extHostNotebookDocument.ts`), not by scraping the webview DOM. Treat `text/html` and `application/javascript` output items as untrusted content — strip or escape before re-displaying, and never eval/execute them in your own tooling.

**Kernel trust / workspace trust gates renderer/preload execution:**
- File: `backLayerWebView.ts` passes `this.workspaceTrustManagementService.isWorkspaceTrusted()` into `preloadsScriptStr(...)`, meaning renderer preload scripts and static preloads behave differently in untrusted workspaces.
- Impact: An adapter that expects consistent output rendering behavior (e.g., custom renderer contributed by a third-party kernel/extension) must account for workspace-trust state — behavior in an untrusted workspace can silently differ (fewer preloads run).

**`ipynb` extension declares `untrustedWorkspaces: { supported: true }` and `virtualWorkspaces: true`:**
- File: `extensions/ipynb/package.json`
- Impact: The serializer itself is expected to work without full trust/workspace access; if your adapter piggybacks on the `ipynb` extension's activation events (`onNotebook:jupyter-notebook`, `onNotebookSerializer:interactive`, `onNotebookSerializer:repl`) it should not assume elevated trust either.

## Performance Considerations

**Large notebooks / many outputs:**
- The live editor caps rendered outputs per cell at `outputDisplayLimit = 500` (`src/vs/workbench/contrib/notebook/browser/viewModel/codeCellViewModel.ts:28`), and `cellOutput.ts` shows a "show more" link once exceeded (`src/vs/workbench/contrib/notebook/browser/view/cellParts/cellOutput.ts:571,730,767`, message: `There are more than ${this.options.limit} outputs, [show more...]`).
- Text output line count is capped by the `notebook.output.textLineLimit` setting (default 30), implemented via `NotebookSetting.textOutputLineLimit` (`src/vs/workbench/contrib/notebook/common/notebookCommon.ts:1029`) and consumed in `notebookOptions.ts:188`. Truncated text output in the renderer is handled by `extensions/notebook-renderers/src/textHelper.ts` (`truncatedArrayOfString`), which shows a "View as a scrollable element" affordance rather than the full text.
- Backup of large outputs is separately capped by `notebook.backup.sizeLimit` (`NotebookSetting.outputBackupSizeLimit`, `notebookCommon.ts:1057`; description at `notebook.contribution.ts:1298`) — beyond this size, hot-reload backups are skipped.
- **Relevance for an AI adapter:** what you see rendered in the UI (or scrape from a webview) is **already truncated** at 30 lines / 500 outputs by default. To get the *full* output text/data for reasoning, you must read output items via the extension API (`NotebookCellOutputItem.data`) rather than the rendered DOM — the DOM view is intentionally lossy for performance. Also budget for genuinely large outputs (e.g., base64 images, big DataFrames) since there is no hard cap at the model level, only at the render/backup level.

**Virtualized cell list:**
- `NotebookCellList` (`src/vs/workbench/contrib/notebook/browser/view/notebookCellList.ts`, 1546 lines) extends `WorkbenchList` — cells are virtualized like a normal list widget; only visible cells have realized DOM/webview insets. `viewportWarmup.ts` proactively warms up code cells near the viewport (200ms `RunOnceScheduler`) to mask virtualization latency.
- Relevance: "Visible in the UI" and "exists in the model" are different — an adapter should always query the `NotebookDocument`/`NotebookTextModel` (always fully materialized) rather than assuming DOM-based access reflects the whole notebook. Programmatically revealing/scrolling to a cell (`revealInView`) may be needed before certain webview-dependent operations complete.

**RPC/serialization boundary cost:**
- All extension-host ↔ main-thread notebook communication goes through the standard proxy-identifier RPC (`MainThreadNotebook*`/`ExtHostNotebook*` in `src/vs/workbench/api/common/extHost.protocol.ts:1290-1400,3502-3580`, registered at `:4094-4098` and `:4171-4176`). Cell/output payloads are converted through `NotebookDto` (`src/vs/workbench/api/browser/mainThreadNotebookDto.ts`), which maps every output item 1:1 (`toNotebookOutputItemDto`/`fromNotebookOutputItemDto`) including raw bytes (`valueBytes`).
- Impact: Reading full outputs for a large notebook (many cells × many outputs × large payloads, e.g., embedded images or big JSON) means marshaling all of that data across the RPC boundary as part of the document snapshot/change events, and — for a real (out-of-process) extension host — across an OS pipe with JSON+binary framing. This is not a big-O concern for typical notebooks but becomes an actual latency/memory concern for output-heavy notebooks (image-generating ML pipelines, wide DataFrame reprs, etc.) — exactly the kind of workload an "AI reads outputs" adapter is likely to target.
- Practical guidance: avoid re-reading the *entire* notebook's outputs on every keystroke/edit; use the granular change-event kinds (`NotebookCellsChangeType.Output`, `.OutputItem`, `.ChangeCellContent`, etc., enumerated in `notebookCommon.ts:337-420`) to only fetch what changed, since `mainThreadNotebookDocuments.ts`/`extHostNotebookDocuments.ts` already deliver deltas rather than full-document snapshots per edit.

**Webview creation/comm cost is measured and logged:**
- `NotebookPerfMarks` (`src/vs/workbench/contrib/notebook/common/notebookPerformance.js`/`.ts`) instruments `extensionActivation`, `inputLoad`, `webviewComm`, `customMarkdownLoad`, `editorLoad` phases; consumed in `notebookEditor.ts:266-552` and telemetry-logged as `notebook/editorOpenPerf`.
- Relevance: Confirms that webview handshake ("webviewComm") is a distinct, measured latency phase — an adapter that needs the webview ready (e.g., to wait for outputs to be visually rendered before screenshotting) should expect a non-trivial async handshake, not instant availability after `notebook.setModel`.

## TODO/FIXME/HACK Density

- 49 TODO/FIXME/HACK-style comments across ~37 files under `src/vs/workbench/contrib/notebook` (excluding `/test/`), concentrated in: view/viewModel layer (`webviewPreloads.ts`, `notebookEditorWidget.ts`, `cellOutput.ts`, `notebookCellList.ts`), diff view (`notebookDiffEditor.ts`, `diffElementOutputs.ts`, `editorHeightCalculator.ts`, `inlineDiff/notebookCellDiffDecorator.ts`), and controllers (`multicursor/notebookMulticursor.ts`, `editActions.ts`, `cellCommands.ts`).
- Representative examples with adapter relevance:
  - `src/vs/workbench/contrib/notebook/browser/contrib/cellDiagnostics/cellDiagnosticsActions.ts:115` — `// TODO: can we add special prompt instructions? e.g. use "%pip install"` inside the built-in "Explain Cell Error" chat action. This is the **existing** built-in analog to what this project's AI adapter does; worth reading in full (see Extensibility section) since it shows the sanctioned pattern for turning a cell error into a chat prompt.
  - `src/vs/workbench/contrib/notebook/browser/controller/editActions.ts:390,448` — `// TODO@rebornix: cells` / `// TODO: support multiple cells` — multi-cell editing operations have known single-cell-oriented gaps; an adapter that edits multiple cells atomically should verify behavior directly rather than assuming full multi-cell support everywhere.
  - `src/vs/workbench/contrib/notebook/browser/services/notebookServiceImpl.ts:316` — `// TODO @lramos15 find a better way to toggle handling diff editors than needing these listeners for every registration` — signals event-listener sprawl in the central `INotebookService` registration path.
  - `src/vs/workbench/contrib/notebook/common/services/notebookWebWorker.ts:100,233` — `// TODO@rebornix, but it might lead to interesting bugs in the future.` / `// TODO@DonJayamanne` — the notebook web worker (used for tokenization/search across notebooks) has acknowledged-fragile logic; relevant if the adapter does bulk text search across many notebooks.
- `eslint-disable` occurrences: 7, all localized (no blanket-disabled files found) — not a significant concern by itself.
- `@deprecated` markers: only 2 in this subsystem — `notebookBrowser.ts:54` (`NotebookKernel<Type>` "keyword" pattern instead of an older kernel API) and `webviewThemeMapping.ts:78`. Low deprecation churn, but the one kernel-related deprecation matters if the adapter enumerates/attaches to kernels.
- `as any` casts: none found via direct grep in `src/vs/workbench/contrib/notebook/**/*.ts` (this pattern is not how loose typing manifests here — loose typing instead shows up as `message: any` in postMessage-style APIs, see below).

## Stability / Extensibility Caveats for Extension Authors

**The notebook messaging/rendering surfaces are still proposed APIs, not stable:**
- `vscode.proposed.notebookMessaging.d.ts` (`NotebookController.postMessage`, `onDidReceiveMessage`, `NotebookController.asWebviewUri`) is a **proposed API** — requires `enabledApiProposals: ["notebookMessaging"]` in the consuming extension's `package.json`, is only usable in specific extension hosts (not in arbitrary marketplace-published extensions without special allowlisting for some proposals), and its shape can change between VS Code releases without the normal deprecation cycle of stable API.
- Other proposed surfaces directly relevant to an AI adapter: `vscode.proposed.notebookCellExecution.d.ts` (structured `CellExecutionError` with `stack: string | CellErrorStackFrame[]`, used for building rich "why did this cell fail" context — exactly the error-reasoning use case), `vscode.proposed.notebookVariableProvider.d.ts` (`provideVariables` for kernel variable introspection — useful for grounding an AI adapter's answers in live kernel state), `vscode.proposed.notebookExecution.d.ts`, `vscode.proposed.notebookKernelSource.d.ts`, `vscode.proposed.notebookReplDocument.d.ts`, `vscode.proposed.contribNotebookStaticPreloads.d.ts`, `vscode.proposed.notebookDeprecated.d.ts` (marks `NotebookCellOutput.id` itself as `@deprecated`).
- File listing all proposals: `src/vscode-dts/vscode.proposed.notebook*.d.ts` (11 files); registered centrally in `src/vs/platform/extensions/common/extensionsApiProposals.ts:351-380`.
- Impact: Building a robust AI adapter on `CellExecutionError`/`notebookVariableProvider`/`notebookMessaging` means depending on unstable, opt-in API. These proposals can be finalized, renamed, or removed. If distributing as a normal (non-Microsoft-internal) extension, some proposals may not be grantable at all outside of VS Code Insiders or specific extension allowlists — verify current availability before depending on them for a production adapter.
- The `ipynb` built-in extension itself only enables one proposal (`diffContentOptions`, per `extensions/ipynb/package.json`), i.e., even Microsoft's own notebook serializer extension keeps its proposed-API footprint minimal — a signal that proposed APIs are treated as high-churn/risky even internally.

**Stable, safer surface to build on:**
- `vscode.NotebookDocument`, `vscode.NotebookCell`, `vscode.NotebookCellOutput`/`NotebookCellOutputItem`, `workspace.onDidChangeNotebookDocument`, `WorkspaceEdit`/`NotebookEdit` for cell mutation, and `NotebookController` (execution) are stable. Prefer these for the AI adapter's core read/edit/execute loop; treat anything from `vscode.proposed.*` as optional enhancement with a fallback path.
- Extension-host implementations to read for exact stable-surface semantics: `src/vs/workbench/api/common/extHostNotebookDocument.ts` (`ExtHostCell`, output/metadata accessors), `src/vs/workbench/api/common/extHostNotebookEditor.ts`, `src/vs/workbench/api/common/extHostNotebookKernels.ts` (883 lines — the largest file in the API layer, execution/controller lifecycle).

**Built-in "explain/fix cell error" actions are a template to imitate, not a hook to extend:**
- Files: `src/vs/workbench/contrib/notebook/browser/contrib/cellDiagnostics/cellDiagnosticsActions.ts` (defines `notebook.cell.chat.explainError` / `notebook.cell.chat.fixError` / `notebook.cell.openFailureActions`), `cellDiagnostics.ts`, `cellDiagnosticEditorContrib.ts`, `diagnosticCellStatusBarContrib.ts`.
- These commands read `context.cell.executionErrorDiagnostic.get()` (an internal `CodeCellViewModel` property, not part of the public extension API) and call directly into `IChatWidgetService`/`InlineChatController` — i.e., this is workbench-internal wiring, not something a third-party extension/adapter can call into or subclass. An external adapter must reconstruct equivalent error context itself from the stable API (`NotebookCellOutput` items with mime `application/vnd.code.notebook.error`, or the proposed `CellExecutionError`/`CellErrorStackFrame` shape) rather than reusing these commands.
- The stack-trace parsing/linkifying logic worth reusing conceptually (regexes for IPython `Cell In[N], line M` / ANSI stripping) lives in `extensions/notebook-renderers/src/stackTraceHelper.ts` — this is extension code (not workbench-internal) and is a reasonable reference implementation for turning a raw IPython traceback string into structured location info, since VS Code's own "Explain Cell Error" flow does the equivalent parsing before handing the message to chat.

## Test Coverage Gaps (relevant to adapter reliability)

**Diff service test file dwarfs its implementation:**
- `src/vs/workbench/contrib/notebook/test/browser/diff/notebookDiffService.test.ts` is 14742 lines (by far the largest file in the whole subsystem, ~10x the next largest), suggesting the diff algorithm has many edge cases historically been bug-fixed via snapshot/example tests rather than a simpler, provably-correct algorithm. Treat notebook diffing as inherently edge-case-prone if the adapter needs custom diff/merge behavior.
- File: `src/vs/workbench/contrib/notebook/test/browser/diff/notebookDiffService.test.ts`

**No test coverage located for `notebookModelStoreSync.ts`'s debounce/merge logic beyond `notebookModelStoreSync.test.ts` (538 lines) — verify edge cases (rapid successive edits from an automated agent) manually:**
- Files: `extensions/ipynb/src/test/notebookModelStoreSync.test.ts`, `extensions/ipynb/src/notebookModelStoreSync.ts`
- Risk: An AI adapter that issues edits in rapid succession (e.g., multi-cell batch edit in one turn) is exactly the kind of caller this debounce logic was not obviously designed/tested for (it targets interactive, human-paced editing). Priority: Medium — worth an integration test in the adapter itself that applies several edits back-to-back and asserts final on-disk/in-memory metadata converges.

---

*Concerns audit: 2026-07-26*
