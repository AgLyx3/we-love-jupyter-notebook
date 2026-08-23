import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type NotebookSnapshot } from "../api/client";
import WorkspaceSidebar, { readRailTab, writeRailTab } from "./WorkspaceSidebar";

const snapshot = (path: string | null = "/w/notes.ipynb"): NotebookSnapshot => ({
  sessionId: "s1", filename: "notes.ipynb", notebookPath: path, workspaceRoot: "/w",
  revision: 1, dirty: false, metadata: {}, nbformat: 4, nbformatMinor: 5,
  cells: [{ cellId: "c0", index: 0, cellType: "code", source: "x = 1\n", metadata: {}, outputs: [], executionCount: 1 }],
});

function mount(over: Partial<Parameters<typeof WorkspaceSidebar>[0]> = {}) {
  const props = {
    root: "/w" as string | null,
    activePath: null,
    notebook: snapshot(),
    tab: "files" as const,
    onTabChange: vi.fn(),
    onOpenNotebook: vi.fn(),
    onCollapse: vi.fn(),
    onJumpToCell: vi.fn(),
    ...over,
  };
  return { props, ...render(<WorkspaceSidebar {...props} />) };
}

beforeEach(() => {
  vi.spyOn(api, "listFiles").mockResolvedValue({ path: "/w", parent: null, entries: [] });
  vi.spyOn(api, "overview").mockResolvedValue({
    blocks: [{ start: 0, end: 0, name: "Setup", state: "ok", produces: [], defines: [], marks: [] }],
    generated: true, stale: false, cellCount: 1, error: null,
  });
  window.localStorage.clear();
});
afterEach(() => { vi.restoreAllMocks(); window.localStorage.clear(); });

describe("WorkspaceSidebar tabs", () => {
  it("shows both tabs when there is a folder and a notebook", () => {
    mount();
    expect(screen.getByRole("tab", { name: "Files" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Outline" })).toBeInTheDocument();
  });

  it("renders no tabs and gives the rail to the outline with no folder open", async () => {
    // Spec §7.1: the file tree is workspace-scoped, the outline notebook-scoped.
    mount({ root: null });
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(await screen.findByText("Setup")).toBeInTheDocument();
  });

  it("renders no tabs and shows only files with no notebook open", () => {
    mount({ notebook: null });
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(screen.getByLabelText("File tree")).toBeInTheDocument();
  });

  it("switches to the outline when the Outline tab is selected", async () => {
    const onTabChange = vi.fn();
    const { rerender, props } = mount({ onTabChange });
    await userEvent.click(screen.getByRole("tab", { name: "Outline" }));
    expect(onTabChange).toHaveBeenCalledWith("outline");
    rerender(<WorkspaceSidebar {...props} tab="outline" />);
    expect(await screen.findByText("Setup")).toBeInTheDocument();
  });

  it("does not fetch the outline while the Files tab is showing", async () => {
    mount({ tab: "files" });
    await waitFor(() => expect(api.listFiles).toHaveBeenCalled());
    expect(api.overview).not.toHaveBeenCalled();
  });
});

describe("rail tab memory", () => {
  it("remembers the tab per notebook path", () => {
    writeRailTab("/w/a.ipynb", "outline");
    writeRailTab("/w/b.ipynb", "files");
    expect(readRailTab("/w/a.ipynb", "files")).toBe("outline");
    expect(readRailTab("/w/b.ipynb", "outline")).toBe("files");
  });

  it("falls back to the default for a notebook with no path", () => {
    // The upload path has no path, so it must not inherit another file's tab.
    writeRailTab(null, "outline");
    expect(readRailTab(null, "files")).toBe("files");
  });

  it("falls back to the default for a path never seen before", () => {
    // Save As changes the path, which is treated as a new notebook.
    writeRailTab("/w/a.ipynb", "outline");
    expect(readRailTab("/w/copy.ipynb", "files")).toBe("files");
  });

  it("ignores a corrupted stored value", () => {
    window.localStorage.setItem("notebook-rail-tab:/w/a.ipynb", "nonsense");
    expect(readRailTab("/w/a.ipynb", "files")).toBe("files");
  });
});
