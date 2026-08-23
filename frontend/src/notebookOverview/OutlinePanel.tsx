import { AlertTriangle, ChevronDown, ChevronRight, CircleDashed, Hash, Shuffle, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError, type NotebookSnapshot, type Overview, type OverviewBlock } from "../api/client";

// One generated field, five computed ones, and the panel has to say which is
// which (spec §7.3). The name carries a dotted underline and a title that names
// it as generated; everything else is plain text. That is what keeps a wrong
// name a correctable annoyance rather than a reason to distrust the panel — and
// every block cites its cell range, so it is checkable in one click.

const stateLabels: Record<string, { text: string; icon: typeof CircleDashed }> = {
  "never-run": { text: "never run", icon: CircleDashed },
  "out-of-order": { text: "ran out of order", icon: Shuffle },
  risky: { text: "touches files, network or the shell", icon: AlertTriangle },
};

const range = (block: OverviewBlock): string =>
  block.start === block.end ? `Cell ${block.start + 1}` : `Cells ${block.start + 1}–${block.end + 1}`;

function BlockRow({ block, expanded, onToggle, onJump, onHover }: {
  block: OverviewBlock;
  expanded: boolean;
  onToggle: () => void;
  onJump: () => void;
  onHover: (hovering: boolean) => void;
}) {
  const state = stateLabels[block.state];
  const StateIcon = state?.icon;
  // Progressive disclosure: blocks by default, details on expand, one level at
  // a time. A block with nothing computed to show gets no expander at all
  // rather than an expander onto an empty box.
  const hasDetail = block.produces.length > 0 || block.defines.length > 0 || block.marks.length > 0;
  return (
    <li
      className={`outline-block state-${block.state}`}
      // The range as data, not as text to be re-parsed: the visible label says
      // "Cells 2–45" when named and "44 cells" when not.
      data-start={block.start}
      data-end={block.end}
      onMouseEnter={() => onHover(true)}
      onMouseLeave={() => onHover(false)}
    >
      <div className="outline-block-head">
        {hasDetail ? (
          <button
            className="outline-expander"
            aria-expanded={expanded}
            aria-label={expanded ? "Hide block details" : "Show block details"}
            onClick={onToggle}
          >
            {expanded ? <ChevronDown /> : <ChevronRight />}
          </button>
        ) : (
          <span className="outline-expander outline-expander-empty" aria-hidden="true" />
        )}
        <button className="outline-jump" onClick={onJump} title={`Go to ${range(block).toLowerCase()}`}>
          {block.name ? (
            <span className="outline-name generated" title="Generated name — the cells below are what it describes">
              {block.name}
            </span>
          ) : (
            // The deterministic fallback has no names. Rendering the range in
            // their place is honest; inventing a label would look generated.
            <span className="outline-name unnamed">{range(block)}</span>
          )}
          <span className="outline-range">
            {/* The range stands in for the name in the fallback, so repeating it
                here would just print it twice. */}
            {block.name ? range(block) : `${block.end - block.start + 1} cell${block.end === block.start ? "" : "s"}`}
            {state && (
              <span className={`outline-state ${block.state}`} title={state.text}>
                {StateIcon && <StateIcon />}
                {state.text}
              </span>
            )}
          </span>
        </button>
      </div>
      {expanded && (
        <div className="outline-detail">
          {block.produces.length > 0 && (
            <p>
              <span className="outline-label">produces</span>
              {block.produces.join(", ")}
            </p>
          )}
          {block.defines.map((def) => (
            <p key={def.name}>
              <span className="outline-label">defines</span>
              <code>{def.name}()</code>
              {def.callSites.length
                ? ` used in ${def.callSites.map((index) => `cell ${index + 1}`).join(", ")}`
                : " never used"}
            </p>
          ))}
          {/* A heading the author wrote always surfaces, even where the model's
              boundaries disagree with it. Losing one reads as broken. */}
          {block.marks.map((mark, index) => (
            <p key={`${mark}-${index}`} className="outline-mark">
              <Hash />
              {mark.replace(/^#+\s*/, "")}
            </p>
          ))}
        </div>
      )}
    </li>
  );
}

export default function OutlinePanel({ notebook, onJump, onHoverBlock }: {
  notebook: NotebookSnapshot;
  onJump: (cellId: string) => void;
  onHoverBlock?: (cellIds: string[] | null) => void;
}) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());

  // Refetch whenever the document moves. This is the free route — it is an AST
  // parse on the backend and never a model call — so following every revision
  // is fine, and it means running a cell updates each block's state without
  // touching the generated map. App drives revisions off the /events stream, so
  // nothing here polls.
  useEffect(() => {
    let live = true;
    setLoading(true);
    api
      .overview(notebook)
      .then((next) => { if (live) { setOverview(next); setFailure(null); } })
      .catch((error) => {
        // A revision conflict just means this snapshot is already behind; the
        // next render carries the newer one, so it is not worth reporting.
        if (live && !(error instanceof ApiError && error.isConflict)) {
          setFailure(error instanceof Error ? error.message : "Outline unavailable");
        }
      })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [notebook.sessionId, notebook.revision]);

  // Collapse every expander when the notebook itself changes — indices from the
  // previous file mean nothing here.
  useEffect(() => { setExpanded(new Set()); }, [notebook.sessionId]);

  const generate = async () => {
    setGenerating(true);
    setFailure(null);
    try {
      setOverview(await api.generateOverview(notebook));
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "Could not build the outline");
    } finally {
      setGenerating(false);
    }
  };

  const cellIdsFor = (block: OverviewBlock): string[] =>
    notebook.cells.slice(block.start, block.end + 1).map((cell) => cell.cellId);

  const toggle = (index: number) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index); else next.add(index);
      return next;
    });

  if (loading && !overview) return <div className="tree-empty">Reading the notebook…</div>;

  const blocks = overview?.blocks ?? [];
  return (
    <div className="outline-panel">
      <div className="outline-actions">
        <button
          className="outline-generate"
          onClick={() => void generate()}
          disabled={generating || !notebook.cells.length}
          title={overview?.generated ? "Build the map again from the current cells" : "Name and group these cells with a model"}
        >
          <Sparkles />
          {generating ? "Building…" : overview?.generated ? "Rebuild map" : "Build map"}
        </button>
      </div>

      {/* Three things the panel must be honest about, in the order they matter. */}
      {overview?.stale && (
        <p className="outline-note stale">
          Cells changed since this map was built. It is still shown as-is — press Rebuild map to update it.
        </p>
      )}
      {overview && !overview.generated && blocks.length > 0 && (
        <p className="outline-note">
          Grouped by headings and milestone calls only. Names need a model — press Build map.
        </p>
      )}
      {(failure ?? overview?.error) && (
        <p className="outline-note error">
          <AlertTriangle />
          {failure ?? overview?.error}
        </p>
      )}

      {blocks.length === 0 ? (
        <div className="tree-empty">This notebook has no cells to map.</div>
      ) : (
        <ul className="outline-list">
          {blocks.map((block, index) => (
            <BlockRow
              key={`${block.start}-${block.end}`}
              block={block}
              expanded={expanded.has(index)}
              onToggle={() => toggle(index)}
              // The existing focus path: App turns this into a focusRequest and
              // NotebookView scrolls it into view. No new scroll machinery.
              onJump={() => { const cell = notebook.cells[block.start]; if (cell) onJump(cell.cellId); }}
              onHover={(hovering) => onHoverBlock?.(hovering ? cellIdsFor(block) : null)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
