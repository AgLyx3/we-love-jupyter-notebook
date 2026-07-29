import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import NotebookView from "./NotebookView";
import type { NotebookSnapshot, TurnScope } from "../api/client";

const notebook: NotebookSnapshot = {
  sessionId: "s1", filename: "n.ipynb", revision: 1, dirty: false, metadata: {},
  nbformat: 4, nbformatMinor: 5,
  cells: [{ cellId: "cell-a", index: 0, cellType: "code", source: "x = 1", metadata: {}, outputs: [], executionCount: null }],
};
const scope: TurnScope = { editableCellIds: [], contextCellIds: [], sessionId: "s1", notebookRevision: 1 };

function view(trusted: boolean) {
  return <NotebookView notebook={notebook} scope={scope} turn={null} trusted={trusted}
    disabled={false} sourceActionsDisabled={false} autoSave={false} focusRequest={null}
    onDirtyChange={vi.fn()} onSave={vi.fn()} onRun={vi.fn()} onScope={vi.fn()} onScopeMany={vi.fn()} onRevert={vi.fn()} />;
}

describe("NotebookView gutter under Trusted", () => {
  it("shows the 'Allow agent edit' button in Blocking", () => {
    render(view(false));
    expect(screen.getByRole("button", { name: /Allow agent edit/ })).toBeInTheDocument();
  });

  it("hides the 'Allow agent edit' button in Trusted; keeps 'Add as focus'", () => {
    render(view(true));
    expect(screen.queryByRole("button", { name: /Allow agent edit/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Add .* as focus/ })).toBeInTheDocument();
  });
});
