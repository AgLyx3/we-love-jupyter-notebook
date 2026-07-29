import { describe, expect, it } from "vitest";
import { cellDiffRanges, hunkOverlays } from "./cellDiff";

describe("cellDiffRanges", () => {
  it("returns no ranges when source is unchanged", () => {
    expect(cellDiffRanges("a = 1\nprint(a)", "a = 1\nprint(a)")).toEqual({ added: [], removed: [] });
  });

  it("maps a single changed line to its added index and removed content", () => {
    const ranges = cellDiffRanges("a = 1\nprint(a)", "a = 2\nprint(a)");
    expect(ranges.added).toEqual([0]);
    expect(ranges.removed).toEqual([{ line: 1, text: "a = 1" }]);
  });

  it("reports added line indices for a multi-line insertion", () => {
    const ranges = cellDiffRanges("a = 1", "a = 1\nb = 2\nc = 3");
    expect(ranges.added).toEqual([1, 2]);
    expect(ranges.removed).toEqual([]);
  });

  it("anchors trailing removed lines past the after document", () => {
    const ranges = cellDiffRanges("a = 1\nb = 2\nc = 3", "a = 1");
    expect(ranges.added).toEqual([]);
    expect(ranges.removed).toEqual([{ line: 1, text: "b = 2" }, { line: 1, text: "c = 3" }]);
  });

  it("handles simultaneous multi-line add and remove", () => {
    const ranges = cellDiffRanges("x\nold1\nold2\nz", "x\nnew1\nnew2\nnew3\nz");
    expect(ranges.added).toEqual([1, 2, 3]);
    expect(ranges.removed).toEqual([{ line: 4, text: "old1" }, { line: 4, text: "old2" }]);
  });
});

describe("hunkOverlays", () => {
  // previous: a b c d e   next: a B c d E — two independent single-line hunks.
  const PREVIOUS = "a\nb\nc\nd\ne";
  const operation = (ordinal: number, state: string, previousRange: [number, number], nextRange: [number, number]) =>
    ({ operationId: `t:c:${ordinal}`, ordinal, state, previousRange, nextRange });

  it("projects pending hunks at their next-document positions when nothing is rejected", () => {
    const overlays = hunkOverlays(PREVIOUS, [
      operation(0, "pending", [1, 2], [1, 2]),
      operation(1, "pending", [4, 5], [4, 5]),
    ]);
    expect(overlays).toEqual([
      { operationId: "t:c:0", line: 1, added: 1, removed: ["b"] },
      { operationId: "t:c:1", line: 4, added: 1, removed: ["e"] },
    ]);
  });

  it("shifts later hunks by the line delta of an earlier rejected hunk", () => {
    // previous: a b c d e → next: a X Y Z c d E. Hunk 0 replaced "b" with
    // three lines; rejecting it puts the single original line back, so the
    // composed document is a b c d E and hunk 1 ("e"→"E") sits at index 4,
    // two lines above its next-document position 6. This is the misalignment
    // the legacy previous→next overlay cannot represent.
    const overlays = hunkOverlays(PREVIOUS, [
      operation(0, "rejected", [1, 2], [1, 4]),
      operation(1, "pending", [4, 5], [6, 7]),
    ]);
    expect(overlays).toEqual([
      { operationId: "t:c:1", line: 4, added: 1, removed: ["e"] },
    ]);
  });

  it("renders nothing for accepted hunks and applies no shift", () => {
    const overlays = hunkOverlays(PREVIOUS, [
      operation(0, "accepted", [1, 2], [1, 2]),
      operation(1, "pending", [4, 5], [4, 5]),
    ]);
    expect(overlays).toEqual([
      { operationId: "t:c:1", line: 4, added: 1, removed: ["e"] },
    ]);
  });

  it("handles pure insertions and pure deletions", () => {
    expect(hunkOverlays("a\nc", [operation(0, "pending", [1, 1], [1, 2])])).toEqual([
      { operationId: "t:c:0", line: 1, added: 1, removed: [] },
    ]);
    expect(hunkOverlays("a\nb\nc", [operation(0, "pending", [1, 2], [1, 1])])).toEqual([
      { operationId: "t:c:0", line: 1, added: 0, removed: ["b"] },
    ]);
  });

  it("skips structural operations with no ranges", () => {
    expect(hunkOverlays(PREVIOUS, [
      { operationId: "t:added:0", ordinal: 0, state: "pending", previousRange: null, nextRange: null },
    ])).toEqual([]);
  });

  it("sorts by ordinal regardless of input order", () => {
    // previous: a b c d e → next: a X Y c d E (hunk 0 grew "b" by one line).
    const overlays = hunkOverlays(PREVIOUS, [
      operation(1, "pending", [4, 5], [5, 6]),
      operation(0, "rejected", [1, 2], [1, 3]),
    ]);
    expect(overlays).toEqual([
      { operationId: "t:c:1", line: 4, added: 1, removed: ["e"] },
    ]);
  });
});
