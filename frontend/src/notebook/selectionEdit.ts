export interface CellSelection {
  cellId: string;
  text: string;
  startLine: number;
  endLine: number;
  // "markdown" marks a selection taken from rendered markdown, where the text
  // has no reliable source line range; omitted/undefined means editor code.
  kind?: "code" | "markdown";
}

// A selection attached to the chat as a reference chip (Cursor-style). The code
// itself is not shown in the input box — only the cell/line label — and is sent
// to the agent as context when the turn is submitted.
export interface SelectionAttachment extends Omit<CellSelection, "kind"> {
  id: string;
  cellIndex: number;
  cellType: string;
  kind: "code" | "error" | "markdown";
}

// Choose a code fence longer than any run of backticks inside the snippet so
// the selected source is quoted verbatim without prematurely closing the block.
function fence(text: string): string {
  const longest = (text.match(/`+/g) ?? []).reduce((max, run) => Math.max(max, run.length), 0);
  return "`".repeat(Math.max(3, longest + 1));
}

function lineRange(selection: { startLine: number; endLine: number }): string {
  return selection.startLine === selection.endLine
    ? `line ${selection.startLine}`
    : `lines ${selection.startLine}–${selection.endLine}`;
}

export function makeAttachment(selection: CellSelection, cellIndex: number, cellType: string): SelectionAttachment {
  const kind = selection.kind === "markdown" ? "markdown" : "code";
  const id = kind === "markdown" ? `${selection.cellId}:selection` : `${selection.cellId}:${selection.startLine}:${selection.endLine}`;
  return { ...selection, id, cellIndex, cellType, kind };
}

// A selection taken from a cell's error output (traceback). It has no source
// line range, so it is labelled and quoted as an error rather than by lines.
export function makeErrorAttachment(cellId: string, text: string, cellIndex: number, cellType: string): SelectionAttachment {
  return { cellId, text, startLine: 0, endLine: 0, id: `${cellId}:error`, cellIndex, cellType, kind: "error" };
}

// Compact chip label, e.g. "Cell 3 · lines 3–4", "Cell 3 · error", "Cell 3 · selection".
export function attachmentLabel(attachment: SelectionAttachment): string {
  const suffix = attachment.kind === "error" ? "error" : attachment.kind === "markdown" ? "selection" : lineRange(attachment);
  return `Cell ${attachment.cellIndex + 1} · ${suffix}`;
}

function attachmentQuote(attachment: SelectionAttachment): string {
  const marker = fence(attachment.text);
  const heading = attachment.kind === "error"
    ? `Cell ${attachment.cellIndex + 1} (error output)`
    : attachment.kind === "markdown"
      ? `Cell ${attachment.cellIndex + 1} (markdown selection)`
      : `Cell ${attachment.cellIndex + 1} (${lineRange(attachment)})`;
  return `${heading}:\n${marker}\n${attachment.text}\n${marker}`;
}

// A default instruction used when the user sends attachments without typing
// anything — e.g. adding a cell's error output and hitting Send directly.
export function defaultAttachmentInstruction(attachments: SelectionAttachment[]): string {
  return attachments.some((attachment) => attachment.kind === "error")
    ? "Fix the error shown below."
    : "Address the referenced selection below.";
}

// Builds the prompt actually sent to the agent: the user's typed instruction
// (or a default when it is empty but selections are attached) followed by the
// quoted source of each attached selection.
export function composeChatPrompt(instruction: string, attachments: SelectionAttachment[]): string {
  const trimmed = instruction.trim();
  if (!attachments.length) return trimmed;
  const lead = trimmed || defaultAttachmentInstruction(attachments);
  return [lead, "", "Referenced selections:", ...attachments.map(attachmentQuote)].join("\n");
}

function selectionQuote(selection: CellSelection): string {
  const marker = fence(selection.text);
  const header = selection.startLine ? `Selected region (${lineRange(selection)})` : "Selected text";
  return `${header}:\n${marker}\n${selection.text}\n${marker}`;
}

// Full agent prompt for an inline (Copilot-style) edit. The editable boundary
// is still the whole containing cell; this text focuses the agent on the
// selected region without changing the enforced boundary.
export function composeInlineEditPrompt(instruction: string, selection: CellSelection): string {
  return [
    instruction.trim(),
    "",
    "Apply this change to the selected region of the cell you may edit. Keep edits minimal and focused on this region; leave the rest of the cell unchanged unless the instruction requires it.",
    "",
    selectionQuote(selection),
  ].join("\n");
}
