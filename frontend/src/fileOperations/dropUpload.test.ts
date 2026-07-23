import { describe, expect, it } from "vitest";
import { extractNotebookFiles, type FsEntry } from "./dropUpload";

const file = (name: string) => new File(["{}"], name, { type: "application/json" });

const fileEntry = (name: string): FsEntry => ({
  isFile: true, isDirectory: false, name,
  file: (onSuccess) => onSuccess(file(name)),
});

const dirEntry = (name: string, children: FsEntry[]): FsEntry => ({
  isFile: false, isDirectory: true, name,
  createReader: () => {
    let done = false;
    return { readEntries: (onSuccess) => { const batch = done ? [] : children; done = true; onSuccess(batch); } };
  },
});

const asItems = (entries: FsEntry[]) => entries.map((entry) => ({ webkitGetAsEntry: () => entry }));

describe("extractNotebookFiles", () => {
  it("falls back to the plain file list and keeps only .ipynb", async () => {
    const files = await extractNotebookFiles({ files: [file("a.ipynb"), file("notes.txt"), file("b.ipynb")] });
    expect(files.map((f) => f.name)).toEqual(["a.ipynb", "b.ipynb"]);
  });

  it("pulls a single dropped .ipynb file", async () => {
    const files = await extractNotebookFiles({ items: asItems([fileEntry("solo.ipynb")]) });
    expect(files.map((f) => f.name)).toEqual(["solo.ipynb"]);
  });

  it("recurses into dropped folders and returns notebooks sorted by name", async () => {
    const tree = dirEntry("project", [
      fileEntry("z.ipynb"),
      fileEntry("readme.md"),
      dirEntry("sub", [fileEntry("a.ipynb"), fileEntry(".hidden.ipynb")]),
    ]);
    const files = await extractNotebookFiles({ items: asItems([tree]) });
    expect(files.map((f) => f.name)).toEqual(["a.ipynb", "z.ipynb"]);
  });

  it("returns nothing when no notebook is present", async () => {
    const files = await extractNotebookFiles({ items: asItems([dirEntry("empty", [fileEntry("data.csv")])]) });
    expect(files).toEqual([]);
  });
});
