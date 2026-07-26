# Coding Conventions — Notebook Subsystem

**Analysis Date:** 2026-07-26
**Scope:** `vscode-repo/src/vs/workbench/contrib/notebook/`, `vscode-repo/extensions/ipynb/`, `vscode-repo/extensions/notebook-renderers/`

These conventions are VS Code's global TypeScript conventions as applied to the notebook subsystem. Canonical source: `vscode-repo/.github/instructions/coding-guidelines.instructions.md`, `vscode-repo/.github/instructions/notebook.instructions.md`, `vscode-repo/.github/instructions/disposable.instructions.md`, `vscode-repo/.github/copilot-instructions.md`. The wiki (https://github.com/microsoft/vscode/wiki/Coding-Guidelines) is the canonical upstream reference.

## Naming Patterns

**Indentation:** Tabs, not spaces (`vscode-repo/.editorconfig`).

**Interfaces:** Prefixed with `I`, PascalCase — e.g. `INotebookService`, `INotebookEditorDelegate`, `ICellViewModel` (`vscode-repo/src/vs/workbench/contrib/notebook/common/notebookService.ts:54`, `vscode-repo/src/vs/workbench/contrib/notebook/browser/notebookBrowser.ts`).

**Types/enums:** PascalCase — e.g. `CellKind`, `NotebookCellExecutionState`, `NotebookSetting` (`vscode-repo/src/vs/workbench/contrib/notebook/common/notebookCommon.ts`).

**Functions/methods/properties/locals:** camelCase, whole words preferred over abbreviations.

**Private class members:** Prefixed with `_` — e.g. `_onDidChangeOutputRenderers`, `_notebook`, `_handled`, `_editorResolverService` (`vscode-repo/src/vs/workbench/contrib/notebook/browser/services/notebookServiceImpl.ts:549-570`, `vscode-repo/src/vs/workbench/contrib/notebook/browser/notebook.contribution.ts:65-77`).

**Constants:** SCREAMING_SNAKE_CASE for command/context-key IDs — e.g. `MOVE_CELL_UP_COMMAND_ID`, `NOTEBOOK_EDITOR_FOCUSED` (`vscode-repo/src/vs/workbench/contrib/notebook/browser/contrib/cellCommands/cellCommands.ts:26-29`).

**File naming:** camelCase filenames matching the primary export — e.g. `notebookServiceImpl.ts` implements `NotebookService`, `notebookCellList.test.ts` tests `notebookCellList.ts`.

## Types

- Do not export types/functions unless shared across multiple components.
- Do not introduce new types/values to the global namespace.
- Avoid `any`/`unknown` unless absolutely necessary (enforced partly by `code-no-any-casts` local ESLint rule, `vscode-repo/.eslint-plugin-local/code-no-any-casts.ts`).

## Dependency Injection

Services are declared as constructor parameters decorated with `@IServiceName`, never resolved lazily via `IInstantiationService.invokeFunction`. Non-service constructor parameters come first, service parameters after:

```typescript
// vscode-repo/src/vs/workbench/contrib/notebook/browser/notebook.contribution.ts:68-78
constructor(
	@IStorageService storageService: IStorageService,
	@IExtensionService extensionService: IExtensionService,
	@IEditorResolverService private readonly _editorResolverService: IEditorResolverService,
	@IConfigurationService private readonly _configurationService: IConfigurationService,
	@IAccessibilityService private readonly _accessibilityService: IAccessibilityService,
	@IInstantiationService private readonly _instantiationService: IInstantiationService,
	@IFileService private readonly _fileService: IFileService,
	@INotebookEditorModelResolverService private readonly _notebookEditorModelResolverService: INotebookEditorModelResolverService,
	@IUriIdentityService private readonly uriIdentService: IUriIdentityService,
) {
	super();
	...
}
```

Rule (from `coding-guidelines.instructions.md`): if a constructor cycle prevents direct injection, break the cycle (e.g. pass the dependency into an `init()` wiring method) rather than reaching through `accessor.get`/`invokeFunction`.

## Service Registration

Services are registered once, at the bottom of a `*.contribution.ts` file, via `registerSingleton`:

```typescript
// vscode-repo/src/vs/workbench/contrib/notebook/browser/notebook.contribution.ts:908-921
registerSingleton(INotebookService, NotebookService, InstantiationType.Delayed);
registerSingleton(INotebookEditorWorkerService, NotebookEditorWorkerServiceImpl, InstantiationType.Delayed);
registerSingleton(INotebookEditorModelResolverService, NotebookModelResolverServiceImpl, InstantiationType.Delayed);
...
```

Every service interface declares `readonly _serviceBrand: undefined;` as its first member (`vscode-repo/src/vs/workbench/contrib/notebook/common/notebookService.ts:55`), enforced by the `code-declare-service-brand` local ESLint rule (`vscode-repo/.eslint-plugin-local/code-declare-service-brand.ts`).

## Workbench Contributions

Feature-level (non-service) singletons that need to run at startup implement `IWorkbenchContribution` and are registered with `registerWorkbenchContribution2`, specifying a `WorkbenchPhase`:

```typescript
// vscode-repo/src/vs/workbench/contrib/notebook/browser/notebook.contribution.ts:894-902
const workbenchContributionsRegistry = Registry.as<IWorkbenchContributionsRegistry>(WorkbenchExtensions.Workbench);
registerWorkbenchContribution2(NotebookContribution.ID, NotebookContribution, WorkbenchPhase.BlockStartup);
registerWorkbenchContribution2(CellContentProvider.ID, CellContentProvider, WorkbenchPhase.BlockStartup);
registerWorkbenchContribution2(NotebookEditorManager.ID, NotebookEditorManager, WorkbenchPhase.BlockRestore);
```

Contribution classes declare a `static readonly ID` and typically `extends Disposable implements IWorkbenchContribution` — e.g. `NotebookContribution` at `vscode-repo/src/vs/workbench/contrib/notebook/browser/notebook.contribution.ts:296`.

## Commands and Actions

New commands/keybindings/menu entries are registered via `registerAction2`, subclassing a domain-specific action base class (`NotebookCellAction`, `NotebookMultiCellAction` from `vscode-repo/src/vs/workbench/contrib/notebook/browser/controller/coreActions.ts`) rather than the generic `Action2` directly:

```typescript
// vscode-repo/src/vs/workbench/contrib/notebook/browser/contrib/cellCommands/cellCommands.ts:32-56
registerAction2(class extends NotebookCellAction {
	constructor() {
		super({
			id: MOVE_CELL_UP_COMMAND_ID,
			title: localize2('notebookActions.moveCellUp', "Move Cell Up"),
			icon: icons.moveUpIcon,
			keybinding: {
				primary: KeyMod.Alt | KeyCode.UpArrow,
				when: ContextKeyExpr.and(NOTEBOOK_EDITOR_FOCUSED, InputFocusedContext.toNegated()),
				weight: KeybindingWeight.WorkbenchContrib
			},
			menu: {
				id: MenuId.NotebookCellTitle,
				when: ContextKeyExpr.equals('config.notebook.dragAndDropEnabled', false),
				group: CellOverflowToolbarGroups.Edit,
				order: 14
			}
		});
	}

	async runWithContext(accessor: ServicesAccessor, context: INotebookCellActionContext) {
		return moveCellRange(context, 'up');
	}
});
```

Command/context-key/menu constants are declared just above the registration block, not inline.

## Disposables / Lifecycle

Core symbols (`vscode-repo/.github/instructions/disposable.instructions.md`): `IDisposable`, `Disposable` (base class exposing `this._register<T extends IDisposable>(t: T): T`), `DisposableStore` (`add`, `clear`), `MutableDisposable` (`value`, `clear`), `toDisposable(fn)`.

Rules:
- Register disposables immediately upon creation: `const x = this._register(new SomeDisposable())`.
- Classes that own disposable children extend `Disposable` (e.g. `NotebookProviderInfoStore extends Disposable`, `vscode-repo/src/vs/workbench/contrib/notebook/browser/notebook.contribution.ts:57`).
- Store collections of disposables in a `DisposableStore` when the class itself isn't `Disposable`: `private readonly _contributedEditorDisposables = this._register(new DisposableStore());` (`vscode-repo/src/vs/workbench/contrib/notebook/browser/notebook.contribution.ts:66`).
- Use `MutableDisposable` for a single disposable slot that gets replaced over time.
- Never leak disposables in tests — see TESTING.md (`ensureNoDisposablesAreLeakedInTestSuite`), enforced by the local ESLint rule `code-ensure-no-disposables-leak-in-test` (`vscode-repo/.eslint-plugin-local/code-ensure-no-disposables-leak-in-test.ts`).

## Event Emitters

Public events are exposed as `Event<T>` backed by a private `Emitter<T>`, registered for disposal:

```typescript
// vscode-repo/src/vs/workbench/contrib/notebook/browser/services/notebookServiceImpl.ts:549-598
private readonly _onDidChangeOutputRenderers;
readonly onDidChangeOutputRenderers;
...
this._onDidChangeOutputRenderers = this._register(new Emitter<void>());
this._onWillAddNotebookDocument = this._register(new Emitter<NotebookTextModel>());
```

Naming: private field `_on<Verb><Noun>` (e.g. `_onDidChangeContent`, `_onWillAddNotebookDocument`), public getter/property `on<Verb><Noun>` bound to `.event`. Verbs follow `will`/`did` conventions to distinguish pre- and post-change notifications. See `Event.None` for a no-op event stub in test doubles (`vscode-repo/src/vs/workbench/contrib/notebook/test/browser/testNotebookEditor.ts:127-129`).

Avoid using events for control flow between components — prefer direct method calls (coding-guidelines instruction).

## Error Handling

- Prefer `async`/`await` over `Promise.then()` chains.
- Unexpected/background errors are reported via `onUnexpectedError(e)` from `vs/base/common/errors.js`, typically wrapping a call in try/catch and returning a safe fallback:

```typescript
// vscode-repo/src/vs/workbench/contrib/notebook/browser/view/cellPart.ts:127-133
try {
	return func();
} catch (e) {
	onUnexpectedError(e);
	return null;
}
```
- User-actionable errors are surfaced via `INotificationService`/`createErrorWithActions` with localized messages and recovery actions (`vscode-repo/src/vs/workbench/contrib/notebook/browser/notebook.contribution.ts:7-8`, `createErrorWithActions`).
- Cancellation is modeled explicitly with `CancellationToken`/`CancellationError`, not by throwing generic errors (`vscode-repo/src/vs/workbench/contrib/notebook/browser/notebook.contribution.ts:49-50`).

## Comments and JSDoc

- Default to **no comment** — code should be self-explanatory through naming.
- JSDoc only for functions/interfaces/classes that need it, max 1–2 short sentences; do not restate the signature or enumerate every branch.
- Inline comments inside a method body: at most 1 line, only for genuine workarounds, non-obvious ordering constraints, or surprising side effects — never narrate the next statement.
- Multi-line block comments inside a method body are treated as a code smell (extract a function / rename instead).

## Strings

- `"double quotes"` for user-visible, localized strings; `'single quotes'` for everything else.
- All user-facing strings go through `localize()`/`localize2()` from `vs/nls`, using `{0}` placeholders — never string concatenation:

```typescript
title: localize2('notebookActions.moveCellUp', "Move Cell Up"),
```

## UI Labels

- Title case for command labels, buttons, menu items (each major word capitalized); short prepositions (≤4 letters) are lowercase unless first/last word.
- Sentence case for view titles/headings (only first word capitalized), no trailing period.

## Style

- Arrow functions over anonymous function expressions; only parenthesize a single arrow parameter when necessary (`x => x + x`, not `(x) => x + x`).
- Always brace loop/conditional bodies; open braces on the same line.
- Prefer `export function x(...) {...}` over `export const x = (...) => {...}` at top-level scope for stack-trace quality.
- Use `IEditorService` to open editors, never reach through `IEditorGroupsService.activeGroup.openEditor`.
- Avoid `bind()`/`call()`/`apply()` solely for `this` binding — use arrow functions instead.
- Every file starts with the Microsoft copyright header (see top of any file cited above).

## Module Organization / Layering

- Notebook code is split into `common/` (platform-agnostic: models, services interfaces, contracts — e.g. `vscode-repo/src/vs/workbench/contrib/notebook/common/notebookCommon.ts`, `common/model/notebookTextModel.ts`) and `browser/` (DOM/editor-widget specific — e.g. `browser/notebookEditorWidget.ts`, `browser/view/`, `browser/viewModel/`).
- Feature-scoped code that plugs into the editor via contribution points lives under `browser/contrib/<feature>/` (e.g. `browser/contrib/find/`, `browser/contrib/execute/`, `browser/contrib/cellCommands/`).
- Cross-file import boundaries between layers (`base` → `platform` → `editor` → `workbench`) are enforced by the local ESLint rule `code-import-patterns` (`vscode-repo/.eslint-plugin-local/code-import-patterns.ts`), configured per-path in `vscode-repo/eslint.config.js` (notebook-specific overrides appear around lines 321-325, 687-701).

## Architecture Reference

For a deeper description of notebook-specific subsystems (viewport virtualization, cell parts lifecycle, focus tracking, hybrid find), see `vscode-repo/.github/instructions/notebook.instructions.md`, which is the maintained architecture doc for this subsystem and is auto-attached by Copilot when editing files under `src/vs/workbench/contrib/notebook/`.

---

*Convention analysis: 2026-07-26*
