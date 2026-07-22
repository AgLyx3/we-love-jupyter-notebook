type DiffLine = { kind: "same" | "added" | "removed"; text: string };

function lineDiff(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
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
