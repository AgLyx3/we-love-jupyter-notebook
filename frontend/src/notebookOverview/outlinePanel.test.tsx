import { configure, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type NotebookCellData, type NotebookSnapshot, type Overview, type OverviewBlock } from "../api/client";
import OutlinePanel from "./OutlinePanel";

configure({ asyncUtilTimeout: 5000 });

const cell = (index: number, over: Partial<NotebookCellData> = {}): NotebookCellData => ({
  cellId: `c${index}`, index, cellType: "code", source: `x = ${index}\n`,
  metadata: {}, outputs: [], executionCount: index + 1, ...over,
});

const snapshot = (count = 6): NotebookSnapshot => ({
  sessionId: "s1", filename: "notes.ipynb", notebookPath: "/w/notes.ipynb", workspaceRoot: "/w",
  revision: 3, dirty: false, metadata: {}, nbformat: 4, nbformatMinor: 5,
  cells: Array.from({ length: count }, (_, index) => cell(index)),
});

const block = (over: Partial<OverviewBlock> = {}): OverviewBlock => ({
  start: 0, end: 2, name: "Revenue by region", state: "ok",
  produces: [], defines: [], marks: [], ...over,
});

const overview = (over: Partial<Overview> = {}): Overview => ({
  blocks: [block(), block({ start: 3, end: 5, name: "Weekly revenue trend" })],
  generated: true, stale: false, cellCount: 6, error: null, ...over,
});

function mount(over: Partial<Overview> = {}, props: { onJump?: (id: string) => void; onHoverBlock?: (ids: string[] | null) => void } = {}) {
  vi.spyOn(api, "overview").mockResolvedValue(overview(over));
  return render(<OutlinePanel notebook={snapshot()} onJump={props.onJump ?? (() => undefined)} onHoverBlock={props.onHoverBlock} />);
}

afterEach(() => { vi.restoreAllMocks(); });

describe("OutlinePanel", () => {
  it("renders one row per block", async () => {
    mount();
    expect(await screen.findByText("Revenue by region")).toBeInTheDocument();
    expect(screen.getByText("Weekly revenue trend")).toBeInTheDocument();
  });

  it("never calls the model on mount", async () => {
    const generate = vi.spyOn(api, "generateOverview");
    mount();
    await screen.findByText("Revenue by region");
    // Opening a notebook must not spend anything (spec §3.2). The panel loads
    // through the free route only.
    expect(generate).not.toHaveBeenCalled();
  });

  it("jumps to the block's first cell", async () => {
    const onJump = vi.fn();
    mount({}, { onJump });
    await userEvent.click(await screen.findByText("Weekly revenue trend"));
    // Block 3–5, so the start cell is index 3.
    expect(onJump).toHaveBeenCalledWith("c3");
  });

  it("marks the generated name as generated and leaves computed facts plain", async () => {
    mount({ blocks: [block({ produces: ["df"] })] });
    const name = await screen.findByText("Revenue by region");
    // Provenance is visible (spec §7.3): the one generated field is the only
    // thing carrying the marker.
    expect(name).toHaveClass("generated");
    await userEvent.click(screen.getByRole("button", { name: /Show block details/ }));
    expect(await screen.findByText(/df/)).not.toHaveClass("generated");
  });

  it("cites the cell range on every block so a wrong name is checkable", async () => {
    mount();
    expect(await screen.findByText("Cells 1–3")).toBeInTheDocument();
    expect(screen.getByText("Cells 4–6")).toBeInTheDocument();
  });

  it("renders a markdown heading inside whichever block contains it", async () => {
    // Spec §7.3: a heading is never silently dropped, even where the model's
    // boundaries disagree with where the author put it.
    mount({ blocks: [block({ marks: ["## Clean the data", "### Outliers"] })] });
    await userEvent.click(await screen.findByRole("button", { name: /Show block details/ }));
    expect(await screen.findByText("Clean the data")).toBeInTheDocument();
    expect(screen.getByText("Outliers")).toBeInTheDocument();
  });

  it("shows a function's call sites, and says so when there are none", async () => {
    mount({ blocks: [block({ defines: [
      { name: "sir", callSites: [4, 14] }, { name: "sensitivity", callSites: [] },
    ] })] });
    await userEvent.click(await screen.findByRole("button", { name: /Show block details/ }));
    expect(await screen.findByText(/used in cell 5, cell 15/)).toBeInTheDocument();
    expect(screen.getByText(/never used/)).toBeInTheDocument();
  });

  it("keeps a stale map visible and says it is stale", async () => {
    mount({ stale: true });
    expect(await screen.findByText(/Cells changed since this map was built/)).toBeInTheDocument();
    // Still shown, rather than blanked or silently regenerated.
    expect(screen.getByText("Revenue by region")).toBeInTheDocument();
  });

  it("says names are unavailable in the deterministic fallback", async () => {
    mount({ generated: false, blocks: [block({ name: "" }), block({ start: 3, end: 5, name: "" })] });
    expect(await screen.findByText(/Names need a model/)).toBeInTheDocument();
    // The range stands in for the name rather than something invented.
    expect(screen.getAllByText("Cells 1–3")[0]).toBeInTheDocument();
  });

  it("surfaces why a generation failed", async () => {
    mount({ generated: false, error: "uncovered cells: [7, 8]" });
    expect(await screen.findByText(/uncovered cells/)).toBeInTheDocument();
  });

  it("generates only when asked, and shows the result", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview({ generated: false, blocks: [block({ name: "" })] }));
    const generate = vi.spyOn(api, "generateOverview").mockResolvedValue(overview());
    render(<OutlinePanel notebook={snapshot()} onJump={() => undefined} />);

    const button = await screen.findByRole("button", { name: /Build map/ });
    expect(generate).not.toHaveBeenCalled();
    await userEvent.click(button);
    await waitFor(() => expect(generate).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Revenue by region")).toBeInTheDocument();
  });

  // The never-run / out-of-order / risky chips were dropped by stitch-diff D10
  // (accepted in §H), so the state is no longer written out in words. It is
  // still on the row as a class, which is what a future marker would hang off
  // and what keeps the drop a render decision rather than a data one.
  it("classes a block by its state without spelling it out", async () => {
    const { container } = mount({ blocks: [
      block({ state: "never-run" }),
      block({ start: 3, end: 5, name: "Later", state: "out-of-order" }),
    ] });
    await screen.findByText("Revenue by region");
    expect(container.querySelector(".outline-block.state-never-run")).not.toBeNull();
    expect(container.querySelector(".outline-block.state-out-of-order")).not.toBeNull();
    expect(screen.queryByText("never run")).not.toBeInTheDocument();
    expect(screen.queryByText("ran out of order")).not.toBeInTheDocument();
  });

  it("reports the cells a hovered block covers", async () => {
    const onHoverBlock = vi.fn();
    mount({}, { onHoverBlock });
    await userEvent.hover(await screen.findByText("Revenue by region"));
    expect(onHoverBlock).toHaveBeenCalledWith(["c0", "c1", "c2"]);
    await userEvent.unhover(screen.getByText("Revenue by region"));
    expect(onHoverBlock).toHaveBeenLastCalledWith(null);
  });

  it("does not offer an expander for a block with nothing to expand", async () => {
    mount({ blocks: [block()] });
    await screen.findByText("Revenue by region");
    expect(screen.queryByRole("button", { name: /Show block details/ })).not.toBeInTheDocument();
  });
});
