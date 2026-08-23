import { configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { CellOutput, NotebookSnapshot, TuningKnob } from "./api/client";

// The whole chain — probe, scan, warm, preview, apply, refresh — is several
// awaited fetches deep, and vitest runs these files in parallel.
configure({ asyncUtilTimeout: 5000 });

vi.mock("@uiw/react-codemirror", () => ({
  default: ({ value, onChange, "aria-label": label }: { value: string; onChange: (value: string) => void; "aria-label": string }) =>
    <textarea aria-label={label} value={value} onChange={(event) => onChange(event.target.value)} />,
}));

const plot: CellOutput = { output_type: "display_data", data: { "image/png": "PLOT" }, metadata: {} };
const printed: CellOutput = { output_type: "stream", name: "stdout", text: "ok\n" };
const bins: TuningKnob = { name: "BINS", cellId: "plot-a", cellIndex: 1, kind: "int", value: 30, bounds: { minimum: 6, maximum: 150, step: 1 } };

const baseNotebook: NotebookSnapshot = {
  sessionId: "session-1", filename: "plots.ipynb", revision: 4, dirty: false, metadata: {}, nbformat: 4, nbformatMinor: 5,
  cells: [
    { cellId: "md-1", index: 0, cellType: "markdown", source: "# Plots", metadata: {}, outputs: [], executionCount: null },
    { cellId: "plot-a", index: 1, cellType: "code", source: "plt.hist(data, bins=30)", metadata: {}, outputs: [plot], executionCount: 1 },
    { cellId: "plot-b", index: 2, cellType: "code", source: "plt.plot(df['x'])", metadata: {}, outputs: [plot], executionCount: 2 },
    { cellId: "text-c", index: 3, cellType: "code", source: "print('ok')", metadata: {}, outputs: [printed], executionCount: 3 },
  ],
};

function response(value: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }));
}

type Call = { path: string; body: Record<string, unknown> };

/** The whole backend the panel talks to, with every request body recorded. */
function server(over: { notebook?: NotebookSnapshot } = {}) {
  const calls: Call[] = [];
  let current = over.notebook ?? baseNotebook;
  const record = (path: string, init?: RequestInit) => {
    calls.push({ path, body: init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {} });
  };
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const path = String(input);
    if (path.endsWith("/notebooks/current")) return response(current);
    if (path.endsWith("/turn-scope")) return response({ editableCellIds: [], contextCellIds: [], sessionId: current.sessionId, notebookRevision: current.revision });
    if (path.endsWith("/kernel/status")) return response({ state: "idle", kernelSessionId: "kernel-1", executionAttemptId: null });
    if (path.endsWith("/session/status")) return response({ sessionId: current.sessionId, documentRevision: current.revision, activeTurn: null, activeExecution: null, turnHistory: [], turnHistoryTruncated: false });
    if (path.endsWith("/tuning/open")) {
      record(path, init);
      const cellId = String((JSON.parse(String(init?.body)) as { cellId: string }).cellId);
      // plot-b is a picture with nothing tunable behind it — the exact case the
      // Tune button must not appear on.
      return response({
        cellId, revision: current.revision, blocked: null, risky: [],
        knobs: cellId === "plot-a" ? [bins] : [],
        rejections: cellId === "plot-a" ? [] : [{ name: "df", reason: "mutated", detail: "`df` is modified in place before this cell." }],
      });
    }
    if (path.endsWith("/tuning/warm")) {
      record(path, init);
      return response({ shadowId: "shadow-1", knobs: [bins], rejections: [], outputs: [plot], timings: [], totalSeconds: 0.3, failed: false, failedCellId: null });
    }
    if (path.endsWith("/preview")) {
      record(path, init);
      return response({ outputs: [{ output_type: "display_data", data: { "image/png": "PREVIEW" }, metadata: {} }], values: { BINS: 40 }, executedCellIds: ["plot-a"], seconds: 0.2, cached: false, failed: false, failedCellId: null });
    }
    if (path.endsWith("/apply")) {
      record(path, init);
      current = {
        ...current, revision: 5,
        cells: current.cells.map((cell) => cell.cellId === "plot-a" ? { ...cell, source: "plt.hist(data, bins=40)" } : cell),
      };
      return response({
        recordId: "rec-1", origin: "tune", sessionId: current.sessionId, state: "applied", knobs: ["BINS"],
        baseRevision: 4, appliedRevision: 5, executionOperationId: "exec-1", executionStartIndex: 1, reRanFromTop: false,
        changes: [{ cellId: "plot-a", previousSource: "plt.hist(data, bins=30)", nextSource: "plt.hist(data, bins=40)" }],
        operations: [{ operationId: "op-1", cellId: "plot-a", ordinal: 0, kind: "source_hunk", state: "pending" }],
        error: null, createdAt: "2026-08-14T00:00:00Z",
      }, 202);
    }
    return response({});
  });
  return { calls, fetchMock };
}

afterEach(() => vi.restoreAllMocks());

describe("the Tune entry point, end to end", () => {
  it("offers Tune only where a picture and knobs meet", async () => {
    const { calls } = server();
    render(<App />);

    expect(await screen.findByRole("button", { name: "Tune code cell 2" })).toBeInTheDocument();
    // plot-b has a picture but nothing tunable; text-c and the markdown cell
    // are never even asked about.
    await waitFor(() => expect(calls.filter((call) => call.path.endsWith("/tuning/open"))).toHaveLength(2));
    expect(calls.map((call) => call.body.cellId)).toEqual(["plot-a", "plot-b"]);
    expect(screen.queryByRole("button", { name: "Tune code cell 3" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Tune code cell 4" })).not.toBeInTheDocument();
  });

  it("applies the values the user dialled in, and says the user did it", async () => {
    const { calls } = server();
    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Tune code cell 2" }));
    const field = await screen.findByRole("spinbutton", { name: "BINS" });

    // One change event carrying the whole field: `<input type="number">` has no
    // text selection for userEvent.clear() to work with.
    fireEvent.change(field, { target: { value: "40" } });
    // The preview lands first (after the panel's debounce) — Apply is what the
    // user presses once they like what they see.
    await waitFor(() => expect(calls.find((call) => call.path.endsWith("/preview"))).toBeDefined());
    await userEvent.click(await screen.findByRole("button", { name: "Apply 1 change and re-run" }));

    const applyCall = await waitFor(() => {
      const found = calls.find((call) => call.path.endsWith("/apply"));
      expect(found).toBeDefined();
      return found as Call;
    });
    // Apply is a document mutation and carries the guard every other one does.
    expect(applyCall.body).toEqual({ sessionId: "session-1", expectedDocumentRevision: 4, values: { BINS: 40 } });
    // Preview, by contrast, mutates nothing and carries no revision to conflict on.
    expect(calls.find((call) => call.path.endsWith("/preview"))?.body).toEqual({ values: { BINS: 40 } });

    expect(await screen.findByText("You tuned this cell")).toBeInTheDocument();
    expect(screen.queryByText("Agent changed this cell")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Undo tuned change to code cell 2")).toBeInTheDocument();
  });
});
