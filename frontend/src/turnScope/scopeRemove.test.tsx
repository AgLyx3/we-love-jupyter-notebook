import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import TurnScopePanel from "./TurnScopePanel";
import type { NotebookSnapshot, TurnScope } from "../api/client";

const notebook: NotebookSnapshot = {
  sessionId: "s1", filename: "n.ipynb", revision: 1, dirty: false, metadata: {}, nbformat: 4, nbformatMinor: 5,
  cells: [
    { cellId: "code-1", index: 0, cellType: "code", source: "value = 1", metadata: {}, outputs: [], executionCount: null },
    { cellId: "md-1", index: 1, cellType: "markdown", source: "# Title", metadata: {}, outputs: [], executionCount: null },
  ],
};
const scope: TurnScope = { editableCellIds: ["code-1"], contextCellIds: ["md-1"], sessionId: "s1", notebookRevision: 1 };

describe("TurnScopePanel per-cell removal", () => {
  it("removes an individual cell without clearing the rest", async () => {
    const onRemoveCell = vi.fn();
    render(<TurnScopePanel notebook={notebook} scope={scope} disabled={false} onClear={vi.fn()} onFocusCell={vi.fn()} onDropCell={vi.fn()} onRemoveCell={onRemoveCell} />);
    await userEvent.click(screen.getByRole("button", { name: "Remove cell 1 from turn scope" }));
    expect(onRemoveCell).toHaveBeenCalledWith("code-1");
  });

  it("still focuses the cell when its label is clicked", async () => {
    const onFocusCell = vi.fn();
    render(<TurnScopePanel notebook={notebook} scope={scope} disabled={false} onClear={vi.fn()} onFocusCell={onFocusCell} onDropCell={vi.fn()} onRemoveCell={vi.fn()} />);
    await userEvent.click(screen.getByTitle("Cell ID: md-1"));
    expect(onFocusCell).toHaveBeenCalledWith("md-1");
  });

  it("keeps the clear-all eraser alongside per-cell removal", () => {
    render(<TurnScopePanel notebook={notebook} scope={scope} disabled={false} onClear={vi.fn()} onFocusCell={vi.fn()} onDropCell={vi.fn()} onRemoveCell={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Clear turn scope" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Remove cell \d+ from turn scope/ })).toHaveLength(2);
  });
});
