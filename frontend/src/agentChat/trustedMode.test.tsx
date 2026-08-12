import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AgentChatPanel from "./AgentChatPanel";
import type { AgentTurn, NotebookSnapshot, TurnScope } from "../api/client";

const notebook: NotebookSnapshot = {
  sessionId: "s1", filename: "n.ipynb", revision: 1, dirty: false, metadata: {},
  nbformat: 4, nbformatMinor: 5,
  cells: [{ cellId: "cell-a", index: 0, cellType: "code", source: "print(x)", metadata: {}, outputs: [], executionCount: null }],
};
const scope: TurnScope = { editableCellIds: ["cell-a"], contextCellIds: [], sessionId: "s1", notebookRevision: 1 };

function panel(over: { onSubmit?: (prompt: string, options: unknown) => void; turn?: AgentTurn | null } = {}) {
  return <AgentChatPanel notebook={notebook} scope={scope} turn={over.turn ?? null} activeTurn={null} history={[]} operation={null} busy={false} mutationsDisabled={false}
    onSubmit={over.onSubmit ?? vi.fn()} onCancel={vi.fn()} onUndo={vi.fn()} onClearScope={vi.fn()} onDecision={vi.fn()} onSelectTurn={vi.fn()} onFocusCell={vi.fn()} onDropCell={vi.fn()} />;
}

describe("AgentChatPanel write scope (Trusted mode)", () => {
  beforeEach(() => localStorage.clear());

  it("defaults to Blocking", () => {
    render(panel());
    expect(screen.getByLabelText("Write scope")).toHaveValue("blocking");
  });

  it("submits with writeScope trusted after selecting Trusted", async () => {
    const onSubmit = vi.fn();
    render(panel({ onSubmit }));
    await userEvent.selectOptions(screen.getByLabelText("Write scope"), "trusted");
    await userEvent.type(screen.getByLabelText("Agent instruction"), "restructure it");
    await userEvent.click(screen.getByRole("button", { name: /Send/ }));
    expect(onSubmit).toHaveBeenCalledWith("restructure it", { agent: "default", model: "default", mode: "edit", writeScope: "trusted" });
  });

  it("is sticky: a previously chosen Trusted scope is restored on mount", () => {
    localStorage.setItem("agent.writeScope", "trusted");
    render(panel());
    expect(screen.getByLabelText("Write scope")).toHaveValue("trusted");
    // Both the composer note and the scope-panel attention banner appear.
    expect(screen.getByText(/Review the diff before keeping/)).toBeInTheDocument();
    expect(screen.getByText(/attention hints only, not a permission fence/)).toBeInTheDocument();
  });

  it("hides the editable/context count chips in Trusted (both are attention-only)", async () => {
    render(panel());
    // Blocking: the count chips are shown.
    expect(screen.getByText("1 editable")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Write scope"), "trusted");
    // Trusted: the chips are gone; pinned cell rows remain.
    expect(screen.queryByText("1 editable")).not.toBeInTheDocument();
    expect(screen.queryByText("0 context")).not.toBeInTheDocument();
  });

  it("renders scoped cells as focus (green) in Trusted", async () => {
    render(panel());
    // Blocking: cell-a is an editable (blue) row.
    expect(screen.getByTitle("Cell ID: cell-a")).toHaveClass("editable");
    await userEvent.selectOptions(screen.getByLabelText("Write scope"), "trusted");
    // Trusted: editable collapses into context — the same cell renders green.
    expect(screen.getByTitle("Cell ID: cell-a")).toHaveClass("context");
  });

  it("disables the scope toggle in Plan mode (write scope has no effect)", async () => {
    render(panel());
    await userEvent.selectOptions(screen.getByLabelText("Agent mode"), "plan");
    expect(screen.getByLabelText("Write scope")).toBeDisabled();
  });

  it("summarizes structural changes for a Trusted turn", () => {
    const turn = {
      turnId: "t1", sessionId: "s1", baseRevision: 1, prompt: "p", writeScope: "trusted",
      editableCellIds: [], contextCellIds: [], undoEligible: true, state: "completed",
      attempts: 1, finalOutput: "", appliedRevision: 2, executionOperationId: null,
      changes: [], structuralOps: [
        { op: "add", cellId: null, detail: {} },
        { op: "delete", cellId: "old", detail: {} },
        { op: "add", cellId: null, detail: {} },
      ], error: null, createdAt: "", completedAt: null,
    } as unknown as AgentTurn;
    render(panel({ turn }));
    expect(screen.getByText(/Structural changes: 2 add · 1 delete/)).toBeInTheDocument();
  });
});
