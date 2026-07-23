import { describe, expect, it } from "vitest";
import { applyMention, detectMention, mentionKey } from "./fileMention";

describe("detectMention", () => {
  it("triggers on @ at the start of the text", () => {
    expect(detectMention("@dat", 4)).toEqual({ start: 0, query: "dat" });
  });

  it("triggers on @ after whitespace and reads up to the caret", () => {
    expect(detectMention("load @train", 11)).toEqual({ start: 5, query: "train" });
  });

  it("returns an empty query right after a bare @", () => {
    expect(detectMention("see @", 5)).toEqual({ start: 4, query: "" });
  });

  it("does not trigger inside an email (@ not preceded by whitespace)", () => {
    expect(detectMention("mail a@b.com", 12)).toBeNull();
  });

  it("closes once whitespace follows the @ token", () => {
    expect(detectMention("@data now", 9)).toBeNull();
  });

  it("uses the @ nearest the caret", () => {
    expect(detectMention("@one @two", 9)).toEqual({ start: 5, query: "two" });
  });

  it("does not trigger when the caret sits before the @", () => {
    expect(detectMention("@foo", 0)).toBeNull();
  });
});

describe("applyMention", () => {
  it("replaces the token with a backticked path and a trailing space", () => {
    const { text, caret } = applyMention("load @tr", 5, 8, "data/train.csv");
    expect(text).toBe("load `data/train.csv` ");
    expect(caret).toBe(text.length);
  });

  it("keeps text after the caret intact", () => {
    const { text, caret } = applyMention("use @cfg here", 4, 8, "config.yaml");
    expect(text).toBe("use `config.yaml`  here");
    expect(text.slice(caret)).toBe(" here");
  });
});

describe("mentionKey", () => {
  it("changes when the query changes", () => {
    expect(mentionKey({ start: 0, query: "a" })).not.toBe(mentionKey({ start: 0, query: "ab" }));
  });
});
