/** Saying what to do about `ModuleNotFoundError` in the cell that failed (#52).
 *
 *  The traceback is correct and useless on its own: the same error means the
 *  environment running the cells lacks a package, or that it is not the
 *  environment that was meant, and those have opposite fixes. What tells them
 *  apart is the interpreter — so these tests are mostly about the block naming
 *  it, and about the cases where naming it would be a guess.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Outputs } from "./NotebookCell";
import { installCommand, missingModule } from "./missingModule";
import type { KernelStatus } from "../api/client";

const kernel: KernelStatus = {
  kernelSessionId: "kernel-1", state: "idle", executionAttemptId: null,
  interpreter: "/proj/.venv/bin/python", interpreterSource: "kernelspec",
};

const error = (ename: string, evalue: string) =>
  ({ output_type: "error", ename, evalue, traceback: [`${ename}: ${evalue}`] });

const missingPandas = error("ModuleNotFoundError", "No module named 'pandas'");

describe("reading a ModuleNotFoundError", () => {
  it("names the module and the distribution that provides it", () => {
    expect(missingModule("ModuleNotFoundError", "No module named 'pandas'"))
      .toEqual({ module: "pandas", packageName: "pandas" });
    expect(missingModule("ModuleNotFoundError", "No module named 'sklearn'"))
      .toEqual({ module: "sklearn", packageName: "scikit-learn" });
  });

  it("leaves ImportError alone", () => {
    // `from x import y` where x imports fine is not fixed by installing x.
    expect(missingModule("ImportError", "cannot import name 'y' from 'x'")).toBeNull();
  });

  it("leaves a submodule alone", () => {
    // CPython names the first component it could not find, so a dotted name
    // means the distribution is already installed.
    expect(missingModule("ModuleNotFoundError", "No module named 'pandas.nonesuch'")).toBeNull();
  });

  it("runs pip through the interpreter rather than a guessed sibling pip", () => {
    const missing = missingModule("ModuleNotFoundError", "No module named 'pandas'")!;
    expect(installCommand("/proj/.venv/bin/python", missing))
      .toBe("/proj/.venv/bin/python -m pip install pandas");
  });
});

describe("the remediation under a failed cell", () => {
  it("names the environment that looked and the command that installs into it", () => {
    render(<Outputs outputs={[missingPandas]} kernel={kernel} />);
    const note = screen.getByRole("note", { name: "Missing module" });
    expect(note).toHaveTextContent("pandas is not installed in the environment your cells run in, /proj/.venv/bin/python");
    expect(note).toHaveTextContent("/proj/.venv/bin/python -m pip install pandas");
  });

  it("offers the other fix when nobody chose the interpreter", () => {
    // The case that goes wrong: the editor's own environment won the
    // kernelspec lookup, so installing may be the wrong move entirely.
    render(<Outputs outputs={[missingPandas]} kernel={kernel} />);
    expect(screen.getByRole("note", { name: "Missing module" }))
      .toHaveTextContent("restart the editor with --kernel-python");
  });

  it("does not offer that fix when --kernel-python was already used", () => {
    render(<Outputs outputs={[missingPandas]} kernel={{ ...kernel, interpreterSource: "kernel-python" }} />);
    const note = screen.getByRole("note", { name: "Missing module" });
    expect(note).toHaveTextContent("chosen with --kernel-python");
    expect(note).not.toHaveTextContent("restart the editor");
  });

  it("says which package a mismatched module comes from", () => {
    render(<Outputs outputs={[error("ModuleNotFoundError", "No module named 'sklearn'")]} kernel={kernel} />);
    const note = screen.getByRole("note", { name: "Missing module" });
    expect(note).toHaveTextContent("It comes from the scikit-learn package");
    expect(note).toHaveTextContent("-m pip install scikit-learn");
  });

  it("stays out of the way of every other error", () => {
    render(<Outputs outputs={[error("NameError", "name 'x' is not defined")]} kernel={kernel} />);
    expect(screen.queryByRole("note", { name: "Missing module" })).not.toBeInTheDocument();
  });

  it("says nothing when the interpreter is unknown", () => {
    // With no path there is no environment to name and no command to give:
    // the block would only restate the traceback above it.
    render(<Outputs outputs={[missingPandas]} kernel={{ ...kernel, interpreter: null }} />);
    expect(screen.queryByRole("note", { name: "Missing module" })).not.toBeInTheDocument();
  });

  it("stays visible while mutations are disabled", () => {
    // It is something to read, not something to press. A turn in flight hides
    // the Add-to-chat button and does not make the environment less true.
    render(<Outputs outputs={[missingPandas]} kernel={kernel} disabled onAddErrorToChat={vi.fn()} />);
    expect(screen.getByRole("note", { name: "Missing module" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add error to agent chat" })).not.toBeInTheDocument();
  });

  it("hands the agent the same environment fact when the error is added to chat", async () => {
    // Otherwise the agent gets the bare traceback and has to guess an
    // interpreter — the failure this whole thing is about.
    const onAddErrorToChat = vi.fn();
    render(<Outputs outputs={[missingPandas]} kernel={kernel} onAddErrorToChat={onAddErrorToChat} />);
    await userEvent.click(screen.getByRole("button", { name: "Add error to agent chat" }));
    const text = onAddErrorToChat.mock.calls[0][0] as string;
    expect(text).toContain("No module named 'pandas'");
    expect(text).toContain("/proj/.venv/bin/python -m pip install pandas");
  });
});
