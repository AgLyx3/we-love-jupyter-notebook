/** The kernel chip says which environment the cells run in (#52).
 *
 *  A `ModuleNotFoundError` has two opposite fixes — install the package, or
 *  point the kernel somewhere else — and nothing on the page distinguished
 *  them. These hold the answer to being reachable from the chip, and to being
 *  absent rather than wrong when the server does not report it.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import KernelControls from "./KernelControls";
import type { KernelStatus } from "../api/client";

const base: KernelStatus = { kernelSessionId: "k1", state: "idle", executionAttemptId: null };

function renderChip(status: KernelStatus) {
  render(
    <KernelControls
      status={status}
      mutationDisabled={false}
      onRunAll={vi.fn()}
      onInterrupt={vi.fn()}
      onRestart={vi.fn()}
    />,
  );
  return screen.getByText(/^Kernel/).closest(".kernel-state") as HTMLElement;
}

describe("the kernel chip", () => {
  it("names the interpreter the cells run in", () => {
    const chip = renderChip({ ...base, interpreter: "/proj/.venv/bin/python", interpreterSource: "kernelspec" });
    expect(chip).toHaveAttribute("title", "Cells run in /proj/.venv/bin/python");
  });

  it("says when the interpreter was chosen rather than resolved", () => {
    // The case worth distinguishing: passing --kernel-python and still landing
    // in the wrong environment looks identical to never having passed it,
    // unless the page says which happened.
    const chip = renderChip({ ...base, interpreter: "/proj/.venv/bin/python", interpreterSource: "kernel-python" });
    expect(chip).toHaveAttribute("title", "Cells run in /proj/.venv/bin/python (--kernel-python)");
  });

  it("says nothing at all when the server does not report one", () => {
    // Rather than an empty or invented tooltip. A server predating the field,
    // or an environment with no kernelspec to read, are both real.
    expect(renderChip(base)).not.toHaveAttribute("title");
  });
});
