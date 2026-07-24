import { describe, expect, it } from "vitest";
import { attachmentLabel, composeChatPrompt, composeInlineEditPrompt, defaultAttachmentInstruction, makeAttachment, makeErrorAttachment, type CellSelection } from "./selectionEdit";

const selection = (over: Partial<CellSelection> = {}): CellSelection => ({
  cellId: "cell-a", text: "print(x)", startLine: 3, endLine: 3, ...over,
});

describe("selectionEdit", () => {
  it("labels an attachment with 1-based cell number and line range", () => {
    expect(attachmentLabel(makeAttachment(selection(), 2, "code"))).toBe("Cell 3 · line 3");
    expect(attachmentLabel(makeAttachment(selection({ startLine: 3, endLine: 5 }), 0, "code"))).toBe("Cell 1 · lines 3–5");
  });

  it("derives a stable attachment id from cell and line range", () => {
    expect(makeAttachment(selection({ startLine: 3, endLine: 4 }), 2, "code").id).toBe("cell-a:3:4");
  });

  it("returns just the trimmed instruction when there are no attachments", () => {
    expect(composeChatPrompt("  do the thing  ", [])).toBe("do the thing");
  });

  it("appends quoted cell/line snippets when attachments are present", () => {
    const prompt = composeChatPrompt("explain this", [makeAttachment(selection(), 2, "code")]);
    expect(prompt).toContain("explain this");
    expect(prompt).toContain("Referenced selections:");
    expect(prompt).toContain("Cell 3 (line 3):");
    expect(prompt).toContain("```\nprint(x)\n```");
  });

  it("labels and quotes an error-output attachment as an error, not by lines", () => {
    const attachment = makeErrorAttachment("cell-a", "NameError: name 'x' is not defined", 2, "code");
    expect(attachment.id).toBe("cell-a:error");
    expect(attachmentLabel(attachment)).toBe("Cell 3 · error");
    const prompt = composeChatPrompt("fix this", [attachment]);
    expect(prompt).toContain("Cell 3 (error output):");
    expect(prompt).toContain("NameError: name 'x' is not defined");
  });

  it("falls back to a fix-the-error instruction when an error is sent with no typed text", () => {
    const attachment = makeErrorAttachment("cell-a", "NameError: name 'x' is not defined", 2, "code");
    expect(defaultAttachmentInstruction([attachment])).toBe("Fix the error shown below.");
    const prompt = composeChatPrompt("", [attachment]);
    // No leading blank line, and the default instruction leads the prompt.
    expect(prompt.startsWith("Fix the error shown below.")).toBe(true);
    expect(prompt).toContain("Cell 3 (error output):");
  });

  it("uses a generic default instruction for a non-error attachment sent with no text", () => {
    expect(defaultAttachmentInstruction([makeAttachment(selection(), 2, "code")])).toBe("Address the referenced selection below.");
  });

  it("labels and quotes a rendered-markdown selection without line numbers", () => {
    const attachment = makeAttachment({ cellId: "cell-a", text: "Summary statistics", startLine: 0, endLine: 0, kind: "markdown" }, 1, "markdown");
    expect(attachment.kind).toBe("markdown");
    expect(attachment.id).toBe("cell-a:selection");
    expect(attachmentLabel(attachment)).toBe("Cell 2 · selection");
    const prompt = composeChatPrompt("rephrase this", [attachment]);
    expect(prompt).toContain("Cell 2 (markdown selection):");
    expect(prompt).toContain("Summary statistics");
  });

  it("uses a longer fence when the selection contains a triple backtick", () => {
    const prompt = composeChatPrompt("x", [makeAttachment(selection({ text: "```py\ncode\n```" }), 0, "code")]);
    expect(prompt).toContain("````\n```py\ncode\n```\n````");
  });

  it("builds an inline-edit prompt that leads with the instruction and includes the snippet", () => {
    const prompt = composeInlineEditPrompt("  rename x to total  ", selection());
    expect(prompt.startsWith("rename x to total\n")).toBe(true);
    expect(prompt).toContain("Keep edits minimal and focused on this region");
    expect(prompt).toContain("Selected region (line 3):");
  });
});
