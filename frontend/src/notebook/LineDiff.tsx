import { lineDiff } from "./cellDiff";

export default function LineDiff({ before, after }: { before: string; after: string }) {
  return <pre className="line-diff" aria-label="Agent source diff">{lineDiff(before, after).map((line, index) => <span className={`diff-${line.kind}`} key={`${index}-${line.kind}`}><i>{line.kind === "added" ? "+" : line.kind === "removed" ? "−" : " "}</i>{line.text || " "}{"\n"}</span>)}</pre>;
}
