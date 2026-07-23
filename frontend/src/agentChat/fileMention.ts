// Pure helpers for the chat "@"-mention: detecting an active mention token from
// the textarea value + caret, and inserting a chosen file path in its place.
// Kept free of React so the trigger/replacement rules can be unit-tested.

export interface MentionToken {
  /** Index of the triggering "@" in the text. */
  start: number;
  /** Text typed after "@" up to the caret (the search query). */
  query: string;
}

/**
 * Return the active mention at `caret`, or null. A mention triggers on an "@"
 * that starts the text or follows whitespace (so emails like `a@b` do not
 * trigger), with no whitespace between it and the caret.
 */
export function detectMention(text: string, caret: number): MentionToken | null {
  const at = text.lastIndexOf("@", caret - 1);
  if (at < 0 || caret <= at) return null;
  if (at > 0 && !/\s/.test(text[at - 1])) return null;
  const query = text.slice(at + 1, caret);
  if (/\s/.test(query)) return null;
  return { start: at, query };
}

/** Serializable identity of a mention token, for tracking a dismissed menu. */
export function mentionKey(token: MentionToken): string {
  return `${token.start}:${token.query}`;
}

export interface MentionInsert {
  text: string;
  caret: number;
}

/**
 * Replace the `@query` token spanning [start, caret) with a backtick-wrapped
 * relative path plus a trailing space, returning the new text and caret. The
 * backticks let the agent recognize the referenced path unambiguously.
 */
export function applyMention(text: string, start: number, caret: number, relativePath: string): MentionInsert {
  const token = `\`${relativePath}\` `;
  return {
    text: text.slice(0, start) + token + text.slice(caret),
    caret: start + token.length,
  };
}
