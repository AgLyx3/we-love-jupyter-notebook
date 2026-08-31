/** Reading a `ModuleNotFoundError` well enough to say what to do about it (#52).
 *
 *  The traceback names the module and nothing else. The fix depends on a fact
 *  it does not carry — which interpreter looked — because the error means one
 *  of two opposite things: the environment running the cells is the right one
 *  and is missing a package, or it is the wrong environment entirely and
 *  installing the package would paper over that. This module supplies the
 *  first half (what is missing, and what to install for it); the interpreter
 *  comes from the kernel status.
 *
 *  Mirrored in Python at `backend/app/kernel_execution/missing_module.py` for
 *  the MCP side, which has the same problem with a less patient reader. The
 *  package table below is checked against that one by
 *  `backend/tests/test_missing_module.py`, so the two cannot drift.
 */

/** Import names that are not their own distribution name.
 *
 *  Deliberately short and deliberately not exhaustive. `pip install cv2` fails
 *  outright, which is worse than no command at all, so the handful of names a
 *  notebook actually trips over are worth knowing exactly; the long tail is
 *  not worth pretending to. Anything absent falls back to the module name,
 *  which is right for the large majority of packages.
 *
 *  Keep in sync with PACKAGE_NAMES in the Python module named above. */
export const PACKAGE_NAMES: Record<string, string> = {
  bs4: "beautifulsoup4",
  cv2: "opencv-python",
  dateutil: "python-dateutil",
  dotenv: "python-dotenv",
  PIL: "pillow",
  skimage: "scikit-image",
  sklearn: "scikit-learn",
  yaml: "PyYAML",
};

export interface MissingModule {
  /** The module the kernel could not find, exactly as the error named it. */
  module: string;
  /** The distribution that provides it, where those differ. Not `package`,
   *  which is a reserved word once destructured under strict mode. */
  packageName: string;
}

// `ModuleNotFoundError`'s message is built by CPython itself, so the quoting is
// stable enough to read. Anchored: a message that merely contains this phrase
// is somebody's own string, not the interpreter's.
const NAMED = /^No module named '([^']+)'/;

// A module the message could plausibly be about. Guards the install command
// against anything odd arriving in `evalue` and being printed as a command.
const IMPORT_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** What is missing, or null when this error is not one to give a command for.
 *
 *  Scoped tightly on purpose:
 *
 *  - Only `ModuleNotFoundError`. Its parent `ImportError` — a `from x import y`
 *    where `x` imports fine but has no `y` — is not fixed by installing
 *    anything, and offering `pip install x` for it would send a reader the
 *    wrong way.
 *  - Only a top-level name. CPython reports the *first* component it could not
 *    find, so a dotted `No module named 'a.b'` means `a` imported and `a.b` did
 *    not: the distribution is already installed and `pip install a` is a no-op.
 */
export function missingModule(ename: unknown, evalue: unknown): MissingModule | null {
  if (ename !== "ModuleNotFoundError" || typeof evalue !== "string") return null;
  const named = NAMED.exec(evalue);
  if (!named) return null;
  const module = named[1];
  if (!IMPORT_NAME.test(module)) return null;
  return { module, packageName: PACKAGE_NAMES[module] ?? module };
}

/** The command that installs into the interpreter the cells actually run in.
 *
 *  `<interpreter> -m pip install`, never a guessed sibling `bin/pip`. The
 *  interpreter path is known exactly; a `pip` executable next to it is not
 *  guaranteed to exist, and a command that fails is worse than none.
 */
export function installCommand(interpreter: string, missing: MissingModule): string {
  return `${interpreter} -m pip install ${missing.packageName}`;
}
