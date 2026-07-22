type DiffLine = { kind: "same" | "added" | "removed"; text: string };
const MAX_DIFF_CHARS = 120_000;
const MAX_DIFF_LINES = 600;
const MAX_DIFF_WORK = 40_000;
const MAX_REGION_LINES = 200;

function boundedRegion(lines: string[], kind: DiffLine["kind"]): DiffLine[] {
  if (lines.length <= MAX_REGION_LINES) return lines.map((text) => ({ kind, text }));
  const edge = MAX_REGION_LINES / 2;
  return [
    ...lines.slice(0, edge).map((text): DiffLine => ({ kind, text })),
    { kind, text: `… ${lines.length - MAX_REGION_LINES} ${kind} lines omitted …` },
    ...lines.slice(-edge).map((text): DiffLine => ({ kind, text })),
  ];
}

function coarseDiff(a: string[], b: string[]): DiffLine[] {
  let prefix = 0;
  while (prefix < a.length && prefix < b.length && a[prefix] === b[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < a.length - prefix && suffix < b.length - prefix && a[a.length - 1 - suffix] === b[b.length - 1 - suffix]) suffix += 1;
  return [
    ...boundedRegion(a.slice(0, prefix), "same"),
    ...boundedRegion(a.slice(prefix, a.length - suffix), "removed"),
    ...boundedRegion(b.slice(prefix, b.length - suffix), "added"),
    ...boundedRegion(a.slice(a.length - suffix), "same"),
  ];
}

function lineDiff(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  if (before.length + after.length > MAX_DIFF_CHARS || a.length + b.length > MAX_DIFF_LINES || a.length * b.length > MAX_DIFF_WORK) return coarseDiff(a, b);
  const dp = Array.from({ length: a.length + 1 }, () => Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i -= 1) for (let j = b.length - 1; j >= 0; j -= 1) dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const lines: DiffLine[] = [];
  let i = 0; let j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) { lines.push({ kind: "same", text: a[i] }); i += 1; j += 1; }
    else if (j < b.length && (i === a.length || dp[i][j + 1] >= dp[i + 1][j])) { lines.push({ kind: "added", text: b[j] }); j += 1; }
    else { lines.push({ kind: "removed", text: a[i] }); i += 1; }
  }
  return lines;
}

export default function LineDiff({ before, after }: { before: string; after: string }) {
  return <pre className="line-diff" aria-label="Agent source diff">{lineDiff(before, after).map((line, index) => <span className={`diff-${line.kind}`} key={`${index}-${line.kind}`}><i>{line.kind === "added" ? "+" : line.kind === "removed" ? "−" : " "}</i>{line.text || " "}{"\n"}</span>)}</pre>;
}
