import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import AgentChatPanel from "./AgentChatPanel";
import type { NotebookSnapshot, TurnScope } from "../api/client";
import { makeAttachment, makeErrorAttachment, type SelectionAttachment } from "../notebook/selectionEdit";

const notebook: NotebookSnapshot = {
  sessionId: "s1", filename: "n.ipynb", revision: 1, dirty: false, metadata: {},
  nbformat: 4, nbformatMinor: 5,
  cells: [{ cellId: "cell-a", index: 2, cellType: "code", source: "print(x)", metadata: {}, outputs: [], executionCount: null }],
};
const scope: TurnScope = { editableCellIds: ["cell-a"], contextCellIds: [], sessionId: "s1", notebookRevision: 1 };
const attachment: SelectionAttachment = makeAttachment({ cellId: "cell-a", text: "print(x)", startLine: 3, endLine: 4 }, 2, "code");

function panel(over: { attachments?: SelectionAttachment[]; onRemoveAttachment?: (id: string) => void; onFocusCell?: (id: string) => void; onSubmit?: (prompt: string, options: unknown) => void } = {}) {
  return <AgentChatPanel notebook={notebook} scope={scope} turn={null} activeTurn={null} history={[]} operation={null} busy={false} mutationsDisabled={false}
    attachments={over.attachments ?? []} onRemoveAttachment={over.onRemoveAttachment ?? vi.fn()}
    onSubmit={over.onSubmit ?? vi.fn()} onCancel={vi.fn()} onUndo={vi.fn()} onClearScope={vi.fn()} onDecision={vi.fn()} onSelectTurn={vi.fn()} onFocusCell={over.onFocusCell ?? vi.fn()} onDropCell={vi.fn()} />;
}

const errorAttachment: SelectionAttachment = makeErrorAttachment("cell-a", "NameError: name 'x' is not defined", 2, "code");

describe("AgentChatPanel selection attachments", () => {
  it("shows a cell/line reference chip and keeps the input empty (no raw code pasted in)", () => {
    render(panel({ attachments: [attachment] }));
    const chip = screen.getByRole("button", { name: "Cell 3 · lines 3–4" });
    expect(chip).toHaveClass("attachment-focus");
    expect(screen.getByLabelText("Agent instruction")).toHaveValue("");
  });

  it("removes an attachment when its remove button is clicked", async () => {
    const onRemoveAttachment = vi.fn();
    render(panel({ attachments: [attachment], onRemoveAttachment }));
    await userEvent.click(screen.getByRole("button", { name: "Remove Cell 3 · lines 3–4" }));
    expect(onRemoveAttachment).toHaveBeenCalledWith("cell-a:3:4");
  });

  it("focuses the cell when the chip label is clicked", async () => {
    const onFocusCell = vi.fn();
    render(panel({ attachments: [attachment], onFocusCell }));
    await userEvent.click(screen.getByRole("button", { name: "Cell 3 · lines 3–4" }));
    expect(onFocusCell).toHaveBeenCalledWith("cell-a");
  });

  it("keeps Send disabled when only a non-error selection is attached and no text is typed", () => {
    render(panel({ attachments: [attachment] }));
    expect(screen.getByRole("button", { name: /Send/ })).toBeDisabled();
  });

  it("enables Send when an error output is attached, even with no typed text", () => {
    render(panel({ attachments: [errorAttachment] }));
    expect(screen.getByRole("button", { name: /Send/ })).toBeEnabled();
  });

  it("submits with an empty prompt when an error attachment is present", async () => {
    const onSubmit = vi.fn();
    render(panel({ attachments: [errorAttachment], onSubmit }));
    await userEvent.click(screen.getByRole("button", { name: /Send/ }));
    expect(onSubmit).toHaveBeenCalledWith("", { model: "default", mode: "edit" });
  });
});
