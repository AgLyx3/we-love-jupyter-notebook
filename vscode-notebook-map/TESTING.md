# Testing Patterns — Notebook Subsystem

**Analysis Date:** 2026-07-26
**Scope:** `vscode-repo/src/vs/workbench/contrib/notebook/test/`, `vscode-repo/extensions/ipynb/src/test/`, `vscode-repo/extensions/notebook-renderers/src/test/`

Canonical reference: `vscode-repo/.github/instructions/writing-tests.instructions.md` (https://github.com/microsoft/vscode/wiki/Writing-Tests).

## Test Framework

**Runner:** Mocha, BDD-style `suite`/`test` interface (TDD `ui: 'tdd'`).

**Assertion library:** Node's built-in `assert` module (`import assert from 'assert';` or `import * as assert from 'assert';`).

**Mocking/spies:** `sinon` for spies/stubs (e.g. `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/notebookExecutionService.test.ts:7`, `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/notebookRendererMessagingService.test.ts:7`). Always call `sinon.restore()` in `teardown` when sinon is used, to avoid leaking stubs/spies across tests.

**Config:** No dedicated per-subsystem test config — notebook tests run inside VS Code's standard unit-test harness (`vscode-repo/test/unit/electron/index.js` for Electron, `vscode-repo/test/unit/browser/index.js` for browser-layer tests, `vscode-repo/test/unit/node/index.js` for Node-layer tests). Which harness a test runs in is determined by its path (`browser/` vs `common/` subfolder), not by an explicit per-file config.

## Test Types

| Type | File suffix | Location | Runs in |
|------|-------------|----------|---------|
| Unit tests | `.test.ts` | `src/vs/workbench/contrib/notebook/test/browser/` (and would-be `test/common/` if platform-agnostic code needed dedicated tests) | Browser, Electron, or Node (per layer) |
| Integration tests | `.integrationTest.ts` | `src/vs/**/test/` (none currently present under the notebook contrib folder) | Real external APIs |
| Extension tests | Standard `vscode-test`/Mocha extension test harness | `extensions/ipynb/src/test/`, `extensions/notebook-renderers/src/test/` | Extension host |

All notebook unit tests currently live under `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/` — there is no `test/common/` subfolder despite there being a `common/` implementation folder; common-layer logic (models, services contracts) is exercised through the same browser-layer test harness (e.g. `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/notebookTextModel.test.ts`, `notebookCommon.test.ts`).

## Test File Layout and Naming

- Location: `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/`, mirroring the shape of `browser/` (subfolders `contrib/`, `diff/`, `view/` mirror `browser/contrib/`, `browser/diff/`, `browser/view/`).
- Naming: `<subjectUnderTest>.test.ts`, matching the filename of the implementation it covers — e.g. `notebookCellList.test.ts` tests `browser/view/notebookCellList.ts`; `notebookKernelService.test.ts` tests `browser/services/notebookKernelServiceImpl.ts`.
- Shared test utilities/fixtures are NOT suffixed `.test.ts` so Mocha doesn't try to run them as a suite — e.g. `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/testNotebookEditor.ts`.
- Snapshot files live in a sibling `__snapshots__/` directory, one `.snap` file per test case, named `<SuiteName>_test<N>__<slugified-test-title>.0.snap` — e.g. `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/__snapshots__/NotebookEditorStickyScroll_test0__should_render_empty___scrollTop_at_0.0.snap`.

Representative test files:
- `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/notebookCellList.test.ts` — viewport/scroll behavior
- `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/notebookTextModel.test.ts` — model edits/undo
- `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/notebookExecutionStateService.test.ts` — execution state tracking
- `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/contrib/find.test.ts` — find-in-notebook
- `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/diff/notebookDiff.test.ts` — notebook diffing
- `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/notebookStickyScroll.test.ts` — sticky scroll headers (uses snapshots)

## Running Tests

- **Unit tests:** `scripts/test.sh` (macOS/Linux) or `scripts\test.bat` (Windows), from repo root `vscode-repo/`.
  - Filter to notebook-only tests by grep on suite/test titles: `scripts/test.sh --grep Notebook`
  - Glob a single file: `scripts/test.sh --runGlob **/notebookCellList.test.js` (compiled `.js`, not `.ts`)
- **Integration tests:** `scripts/test-integration.sh` / `scripts\test-integration.bat`.
- **Extension tests** (`extensions/ipynb`, `extensions/notebook-renderers`): run via the extension's own test harness — `notebook-renderers` uses `vscode-repo/extensions/notebook-renderers/src/test/index.ts` (Mocha `tdd` UI via `vscode-repo/test/integration/electron/testrunner`), invoked as part of the extension integration test suite (`npm run test-extension` at repo root uses `vscode-test`).
- **Selfhost UI runner:** the Selfhost Test Provider VS Code extension can run/debug any individual `.test.ts` from the editor.
- npm root-level scripts (`vscode-repo/package.json`): `test-browser` (Playwright + `test/unit/browser/index.js`), `test-node` (`mocha test/unit/node/index.js --delay --ui=tdd --timeout=5000 --exit`), `test-extension` (`vscode-test`).

## Test Structure

Standard shape: module-level `suite(...)`, a shared `ensureNoDisposablesAreLeakedInTestSuite()` call, `setup`/`teardown` hooks for shared instantiation-service wiring, then `test(...)` blocks:

```typescript
// vscode-repo/src/vs/workbench/contrib/notebook/test/browser/notebookCellList.test.ts
suite('NotebookCellList', () => {
	let testDisposables: DisposableStore;
	let instantiationService: TestInstantiationService;

	teardown(() => {
		testDisposables.dispose();
	});

	ensureNoDisposablesAreLeakedInTestSuite();

	let config: TestConfigurationService;
	setup(() => {
		testDisposables = new DisposableStore();
		instantiationService = setupInstantiationService(testDisposables);
		config = new TestConfigurationService();
		instantiationService.stub(IConfigurationService, config);
	});

	test('revealElementsInView: reveal fully visible cell should not scroll', async function () {
		await withTestNotebook([...cells...], async (editor, viewModel, disposables) => {
			// ... assertions via assert.deepStrictEqual ...
		});
	});
});
```

An alternative pattern assigns the disposable store directly from `ensureNoDisposablesAreLeakedInTestSuite()`'s return value and reuses it as the notebook-scoped `DisposableStore` for the whole suite:

```typescript
// vscode-repo/src/vs/workbench/contrib/notebook/test/browser/contrib/find.test.ts
suite('Notebook Find', () => {
	const disposables = ensureNoDisposablesAreLeakedInTestSuite();
	...
});
```

**Clean teardown rule:** every suite must call `ensureNoDisposablesAreLeakedInTestSuite()` (from `vs/base/test/common/utils.js`) so disposal leaks fail the test. This is also enforced statically by the `code-ensure-no-disposables-leak-in-test` local ESLint rule (`vscode-repo/.eslint-plugin-local/code-ensure-no-disposables-leak-in-test.ts`).

## Fixtures, Mocks, and Test Utils

The central fixture/harness module for notebook browser tests is `vscode-repo/src/vs/workbench/contrib/notebook/test/browser/testNotebookEditor.ts`. Key exports:

- **`setupInstantiationService(disposables)`** — builds a `TestInstantiationService` and stubs every service the notebook browser layer needs (`ILanguageService`, `IUndoRedoService`, `IConfigurationService`, `IThemeService`, `IModelService`, `IContextKeyService`, `INotebookExecutionStateService`, `INotebookCellStatusBarService`, `ICodeEditorService`, etc.). Call this once per test/suite via `setup()`, then `instantiationService.stub(...)` to override individual services per test.

  ```typescript
  // vscode-repo/src/vs/workbench/contrib/notebook/test/browser/testNotebookEditor.ts:214-252
  export function setupInstantiationService(disposables: Pick<DisposableStore, 'add'>) {
  	const instantiationService = disposables.add(new TestInstantiationService());
  	instantiationService.stub(ILanguageService, disposables.add(new LanguageService()));
  	instantiationService.stub(INotebookExecutionStateService, new TestNotebookExecutionStateService());
  	...
  	return instantiationService;
  }
  ```

- **`withTestNotebook(cells, callback, accessor?)`** — the primary fixture entry point. Builds a real `NotebookTextModel`/`NotebookViewModel`/`NotebookCellList` from a plain array of `MockNotebookCell` tuples, runs `callback(editor, viewModel, disposables, accessor)` inside `runWithFakedTimers`, and disposes everything afterward (even on async rejection via `.finally`).

  ```typescript
  // vscode-repo/src/vs/workbench/contrib/notebook/test/browser/testNotebookEditor.ts:485-507
  export async function withTestNotebook<R = any>(
  	cells: MockNotebookCell[],
  	callback: (editor, viewModel, disposables, accessor) => Promise<R> | R,
  	accessor?: TestInstantiationService
  ): Promise<R> { ... }
  ```

  `MockNotebookCell` is a tuple: `[source: string, lang: string, kind: CellKind, output?: IOutputDto[], metadata?: NotebookCellMetadata]`.

- **`withTestNotebookDiffModel(originalCells, modifiedCells, callback)`** — same idea for diff-editor tests, builds two notebooks and an `INotebookDiffEditorModel` wrapper.

- **`createNotebookCellList(instantiationService, disposables, viewContext?)`** — builds a standalone `NotebookCellList` with a minimal mock renderer/delegate, for tests that need direct list control (scroll, reveal) outside a full editor.

- **`createTestNotebookEditor` / `TestCell` / `NotebookEditorTestModel`** — lower-level building blocks used internally by `withTestNotebook`; also usable directly when a test needs a bare `NotebookViewModel` without the full editor mock.

- **`TestNotebookExecutionStateService`** — an in-memory fake of `INotebookExecutionStateService` used as the default stub in `setupInstantiationService`.

**Mocking library:** `vs/base/test/common/mock.js`'s `mock<T>()` helper builds partial interface implementations (`new class extends mock<IActiveNotebookEditorDelegate>() { override someMethod() {...} }`), used extensively in `testNotebookEditor.ts` to stub interfaces without implementing every member.

**Fixture location convention:** fixtures/mocks live beside the tests in the `test/` folder, not in a separate `__fixtures__` or `__mocks__` directory. Files that export test utilities (not test suites) omit the `.test.ts` suffix (e.g. `testNotebookEditor.ts`) so Mocha's test discovery skips them.

## Snapshot Testing

Uses `assertSnapshot` from `vs/base/test/common/snapshot.js` (Jest-like snapshot assertions). On first run, the snapshot is written to a `__snapshots__` directory beside the test file; subsequent runs diff against it and fail on mismatch — verify manually the first output is correct before committing the `.snap` file.

```typescript
// vscode-repo/src/vs/workbench/contrib/notebook/test/browser/notebookStickyScroll.test.ts
import { assertSnapshot } from '../../../../../base/test/common/snapshot.js';
...
await assertSnapshot(resultingMap);
```

## Extension-Level Tests (`extensions/ipynb`, `extensions/notebook-renderers`)

These use plain Mocha `suite`/`test` + Node's `assert`, running in the extension host rather than the notebook browser test harness — no `TestInstantiationService`/`withTestNotebook` involved, since these packages test pure serialization/rendering logic against the `vscode` extension API surface.

```typescript
// vscode-repo/extensions/ipynb/src/test/serializers.test.ts
suite(`ipynb serializer`, () => {
	let disposables: vscode.Disposable[] = [];
	setup(() => { disposables = []; });
	teardown(async () => {
		disposables.forEach(d => d.dispose());
		disposables = [];
		sinon.restore();
	});

	test('Deserialize', async () => {
		const notebook = jupyterNotebookModelToNotebookData({ cells }, 'python');
		assert.ok(notebook);
		...
	});
});
```

Files:
- `vscode-repo/extensions/ipynb/src/test/serializers.test.ts` — `.ipynb` ⇄ `NotebookData` (de)serialization
- `vscode-repo/extensions/ipynb/src/test/deserializers.test.ts`-adjacent logic (see `jupyterCellOutputToCellOutput`)
- `vscode-repo/extensions/ipynb/src/test/clearOutputs.test.ts` — output-clearing command behavior
- `vscode-repo/extensions/ipynb/src/test/notebookModelStoreSync.test.ts` — model/store synchronization
- `vscode-repo/extensions/notebook-renderers/src/test/notebookRenderer.test.ts`, `linkify.test.ts`, `stackTraceHelper.test.ts` — output renderer logic (ANSI, links, stack traces)
- Both extensions' `src/test/index.ts` configure the Mocha `tdd` test runner (`vscode-repo/extensions/notebook-renderers/src/test/index.ts` wires `mocha-junit-reporter` output when running in CI).

## What Is Covered

- Model correctness: cell edits, undo/redo, text buffer sync (`notebookTextModel.test.ts`, `contrib/notebookUndoRedo.test.ts`)
- View/viewport behavior: scroll/reveal, sticky scroll, cell layout (`notebookCellList.test.ts`, `notebookStickyScroll.test.ts`, `notebookCellLayoutManager.test.ts`)
- Execution state machine (`notebookExecutionService.test.ts`, `notebookExecutionStateService.test.ts`, `notebookKernelService.test.ts`, `notebookKernelHistory.test.ts`)
- Diffing between notebook versions (`diff/notebookDiff.test.ts`, `diff/notebookDiffService.test.ts`, `diff/editorHeightCalculator.test.ts`)
- Find-in-notebook, including hybrid text/output search (`contrib/find.test.ts`)
- Outline/symbols, status bar contributions, clipboard, diagnostics (`contrib/notebookOutline.test.ts`, `contrib/executionStatusBarItem.test.ts`, `contrib/notebookClipboard.test.ts`, `contrib/notebookCellDiagnostics.test.ts`)
- `.ipynb` serialization round-tripping and output rendering (extension-level tests above)

## Common Patterns

**Async test with fixture teardown:**
```typescript
test('some behavior', async function () {
	await withTestNotebook(
		[['print(1)', 'python', CellKind.Code, [], {}]],
		async (editor, viewModel, disposables, accessor) => {
			// exercise editor/viewModel
			assert.deepStrictEqual(viewModel.length, 1);
		}
	);
});
```

**Faked timers:** `withTestNotebook` wraps callbacks in `runWithFakedTimers({ useFakeTimers: true }, ...)` (`vs/base/test/common/timeTravelScheduler.js`) so debounced/scheduled notebook logic (e.g. viewport updates) can be tested deterministically without real delays.

**Assertion style:** prefer a single `assert.deepStrictEqual` capturing a full expected state/snapshot over many fine-grained assertions per test, per `writing-tests.instructions.md`.

---

*Testing analysis: 2026-07-26*
