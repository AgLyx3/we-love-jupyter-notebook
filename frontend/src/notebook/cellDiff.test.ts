import { describe, expect, it } from "vitest";
import { cellDiffRanges } from "./cellDiff";

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
