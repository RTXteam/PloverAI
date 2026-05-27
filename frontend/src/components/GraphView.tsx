"use client";

// renders the Stage 13 answer_graph_view as a clickable node-link
// diagram. layout is a fixed star: the pinned entity sits in the
// center, every answer node sits on a circle around it, and each edge
// is drawn as a straight line connecting them. clicking an edge opens
// a side panel with the full provenance (predicate, knowledge level,
// primary knowledge source, supporting PMIDs, supporting-text
// snippets, PubTator co-mention verification).
//
// the SVG is intentionally hand-rolled rather than a node-graph
// library (react-flow, cytoscape) — every PloverAI one-hop answer has
// star topology with one center and ≤ ~10 leaves, and the visual is
// part of the paper's research-grade presentation, so we want exact
// control over the rendering instead of a library default.

import { useMemo, useState } from "react";
import { MarkdownAnswer } from "./MarkdownAnswer";
import type { AnswerGraphView, AnswerGraphEdge, AnswerGraphNode } from "@/lib/api";

type Props = {
  view: AnswerGraphView;
  // optional prose summary from Stage 15 — folded under the graph as
  // a collapsible "Plain-language summary" so the graph card is the
  // single answer view rather than a separate card alongside the prose.
  explanation?: string | null;
};

// node placement constants — sized for the answer-card position which
// has the full width of the result column. enlarged from the original
// 720×480 so node labels + CURIE codes have room to breathe and so
// the graph reads as the PRIMARY answer display.
const SVG_W = 900;
const SVG_H = 640;
const CENTER_X = SVG_W / 2;
const CENTER_Y = SVG_H / 2;
const RADIUS = 230;   // distance from center to answer-node circle
const NODE_R = 32;    // node circle radius — fits one short word inside

export function GraphView({ view, explanation }: Props) {
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null);

  // place each answer node at an angle around the pinned node.
  // mapping curie → {x, y} so edges can look up endpoint coordinates
  // by referencing either source or target.
  const positions = useMemo(() => {
    const m = new Map<string, { x: number; y: number }>();
    m.set(view.pinned_node.curie, { x: CENTER_X, y: CENTER_Y });
    const n = Math.max(view.answer_nodes.length, 1);
    view.answer_nodes.forEach((node, i) => {
      // start from the top (-90°) and go clockwise. evenly distribute.
      const angle = (-Math.PI / 2) + (2 * Math.PI * i) / n;
      m.set(node.curie, {
        x: CENTER_X + RADIUS * Math.cos(angle),
        y: CENTER_Y + RADIUS * Math.sin(angle),
      });
    });
    return m;
  }, [view]);

  const selectedEdge = selectedEdgeId
    ? view.edges.find((e) => e.id === selectedEdgeId) ?? null
    : null;

  // pubtator-metrics summary line shown above the SVG. helpful at a
  // glance: "3 of 5 edges PubTator-verified" — the eval axis the paper
  // hangs on.
  const pmetrics = view.pubtator_metrics;
  const ratePct =
    pmetrics?.rate != null ? `${Math.round(pmetrics.rate * 100)}%` : "—";

  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 overflow-hidden">
      <div className="flex items-center justify-between gap-2 px-4 py-3 border-b border-zinc-200 dark:border-zinc-800">
        <div>
          <h3 className="text-sm font-semibold tracking-tight">Answer graph</h3>
          <p className="text-xs text-zinc-600 dark:text-zinc-400 mt-0.5">
            Pinned entity in the center; each picked answer is a node connected by one edge.
            Click an edge to see its predicate, knowledge source, PMIDs, and PubTator
            co-mention verification.
          </p>
        </div>
        {pmetrics && (
          <div className="text-[11px] font-mono text-zinc-600 dark:text-zinc-400 whitespace-nowrap">
            PubTator: {pmetrics.verified}/{pmetrics.verified + pmetrics.unverified} verified
            {pmetrics.not_applicable > 0 && (
              <span className="text-zinc-400"> ({pmetrics.not_applicable} n/a)</span>
            )}
            {pmetrics.rate != null && <span className="ml-2">· {ratePct}</span>}
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_380px]">
        <div className="border-b lg:border-b-0 lg:border-r border-zinc-200 dark:border-zinc-800 p-4 overflow-x-auto">
          <svg
            viewBox={`0 0 ${SVG_W} ${SVG_H}`}
            className="w-full max-w-full"
            style={{ maxHeight: 640 }}
            role="img"
            aria-label="Answer graph"
          >
            {/* edges UNDER nodes so node circles cover the line ends */}
            {view.edges.map((edge) => (
              <EdgeLine
                key={edge.id}
                edge={edge}
                positions={positions}
                hovered={hoveredEdgeId === edge.id}
                selected={selectedEdgeId === edge.id}
                onHover={setHoveredEdgeId}
                onSelect={setSelectedEdgeId}
              />
            ))}
            {/* nodes on top */}
            <NodeCircle node={view.pinned_node} pos={positions.get(view.pinned_node.curie)!} />
            {view.answer_nodes.map((n) => (
              <NodeCircle key={n.curie} node={n} pos={positions.get(n.curie) ?? { x: 0, y: 0 }} />
            ))}
          </svg>
          <Legend />
        </div>
        <EdgeDetails edge={selectedEdge} />
      </div>

      {/* prose summary folded under the graph as a collapsible. the
          graph card is the PRIMARY answer view; the prose is a fallback
          / natural-language paraphrase that older users may still want
          to read. closed by default so the visual hierarchy puts the
          graph first. */}
      {explanation && (
        <details className="border-t border-zinc-200 dark:border-zinc-800">
          <summary className="cursor-pointer px-4 py-3 text-sm font-medium text-zinc-700 dark:text-zinc-300 hover:bg-zinc-50 dark:hover:bg-zinc-900/60 select-none flex items-center gap-2">
            <Chevron />
            Plain-language summary
            <span className="text-xs font-normal text-zinc-500">
              (LLM-written natural-language paraphrase)
            </span>
          </summary>
          <div className="px-4 pb-4">
            <MarkdownAnswer text={explanation} />
          </div>
        </details>
      )}
    </div>
  );
}

// little chevron used by the collapsible summary
function Chevron() {
  return (
    <svg
      className="text-zinc-400 shrink-0 transition-transform [details[open]_&]:rotate-90"
      width="14"
      height="14"
      viewBox="0 0 20 20"
      fill="currentColor"
      aria-hidden
    >
      <path
        fillRule="evenodd"
        d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z"
        clipRule="evenodd"
      />
    </svg>
  );
}

// ---- SVG primitives ----

function EdgeLine({
  edge,
  positions,
  hovered,
  selected,
  onHover,
  onSelect,
}: {
  edge: AnswerGraphEdge;
  positions: Map<string, { x: number; y: number }>;
  hovered: boolean;
  selected: boolean;
  onHover: (id: string | null) => void;
  onSelect: (id: string | null) => void;
}) {
  const s = positions.get(edge.source);
  const t = positions.get(edge.target);
  if (!s || !t) return null;

  // shorten the line by NODE_R at each end so the line meets the
  // circle's edge, not the center.
  const dx = t.x - s.x;
  const dy = t.y - s.y;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  const x1 = s.x + ux * NODE_R;
  const y1 = s.y + uy * NODE_R;
  const x2 = t.x - ux * NODE_R;
  const y2 = t.y - uy * NODE_R;

  // colour by PubTator verification status:
  //   verified  → emerald
  //   checked-but-unverified → amber
  //   not applicable (no PMIDs) → zinc
  const v = edge.pubtator_verified;
  let strokeBase = "#a1a1aa"; // zinc-400
  let strokeBaseDark = "#52525b";
  if (v?.verified) {
    strokeBase = "#10b981";  // emerald-500
    strokeBaseDark = "#059669";
  } else if (v && !v.verified) {
    strokeBase = "#f59e0b";  // amber-500
    strokeBaseDark = "#d97706";
  }

  const strokeW = selected ? 3.5 : hovered ? 2.5 : 1.75;
  const opacity = hovered || selected || !v ? 1 : 0.75;

  // a wider transparent line catches mouse events so the user doesn't
  // have to land on a 2px-thick stroke. then the visible line on top.
  return (
    <g>
      <line
        x1={x1}
        y1={y1}
        x2={x2}
        y2={y2}
        stroke="transparent"
        strokeWidth={14}
        cursor="pointer"
        onMouseEnter={() => onHover(edge.id)}
        onMouseLeave={() => onHover(null)}
        onClick={() => onSelect(edge.id === selectedEdge_idEq(selected, edge.id) ? null : edge.id)}
      />
      <line
        x1={x1}
        y1={y1}
        x2={x2}
        y2={y2}
        stroke={strokeBase}
        className="dark:[stroke:var(--dk)]"
        style={{ ["--dk" as never]: strokeBaseDark }}
        strokeWidth={strokeW}
        opacity={opacity}
        pointerEvents="none"
      />
      {/* predicate label on hover/selection. on hover-or-select we show
          predicate + edge id so the user can identify the PloverDB
          edge without clicking. */}
      {(hovered || selected) && edge.predicate && (
        <PredicateLabel
          midX={(x1 + x2) / 2}
          midY={(y1 + y2) / 2}
          text={shortPredicate(edge.predicate)}
        />
      )}
      {/* persistent small edge-id chip slightly offset from midpoint.
          shown always (low-emphasis) so the user can see which PloverDB
          edge id maps to which line without hovering. */}
      <EdgeIdChip
        x={(x1 + x2) / 2}
        y={(y1 + y2) / 2 + 14}
        text={`#${edge.id}`}
      />
    </g>
  );
}

function EdgeIdChip({ x, y, text }: { x: number; y: number; text: string }) {
  return (
    <text
      x={x}
      y={y}
      textAnchor="middle"
      fontSize="10"
      fontFamily="ui-monospace, SFMono-Regular, monospace"
      className="fill-zinc-500 dark:fill-zinc-400 select-none"
      pointerEvents="none"
    >
      {text}
    </text>
  );
}

// tiny helper to keep onClick logic readable
function selectedEdge_idEq(currentlySelected: boolean, id: string) {
  return currentlySelected ? id : "__none__";
}

function PredicateLabel({ midX, midY, text }: { midX: number; midY: number; text: string }) {
  // approximate width by chars × 7.5. centre the box around the
  // midpoint so the text reads cleanly. larger than before to match
  // the upscaled node fonts.
  const padX = 8;
  const w = text.length * 7.5 + padX * 2;
  const h = 20;
  return (
    <g pointerEvents="none">
      <rect
        x={midX - w / 2}
        y={midY - h / 2}
        width={w}
        height={h}
        rx={4}
        fill="white"
        stroke="#a1a1aa"
        strokeWidth={1.25}
        className="dark:fill-zinc-900 dark:stroke-zinc-600"
      />
      <text
        x={midX}
        y={midY + 4.5}
        textAnchor="middle"
        fontSize="12"
        fontFamily="ui-monospace, SFMono-Regular, monospace"
        className="fill-zinc-700 dark:fill-zinc-200"
      >
        {text}
      </text>
    </g>
  );
}

function shortPredicate(p: string): string {
  // strip the "biolink:" prefix for compactness — it's redundant on
  // an edge between two clearly-typed nodes.
  return p.replace(/^biolink:/, "");
}

function NodeCircle({ node, pos }: { node: AnswerGraphNode; pos: { x: number; y: number } }) {
  const isPinned = node.role === "pinned";
  // category → colour. pinned uses a distinct deeper hue so the focal
  // entity reads at a glance.
  const palette = categoryPalette(node.category);
  const fill = isPinned ? palette.pinned : palette.fill;
  const stroke = isPinned ? palette.pinnedStroke : palette.stroke;

  // we deliberately keep the INSIDE of the circle empty (just a single
  // role glyph) — earlier versions stuffed "ANSWER" + "SmallMolecul"
  // inside which clipped letters at the circle boundary. now the
  // category is conveyed by COLOR (see Legend) and the full label sits
  // BELOW the circle where it has all the horizontal room it needs.
  //
  // labels vary widely: "metformin" (9 chars) up to
  // "congenital bilateral aplasia of vas deferens from CFTR mutation"
  // (63 chars). we word-wrap to UP TO 3 lines × 22 chars (66 chars
  // displayed), truncate past that with an ellipsis, and provide the
  // full label as a native SVG <title> element so hovering surfaces
  // the complete text in the browser's tooltip. avoid de-duplication
  // when label IS the curie (the unresolved-curie fallback case).
  const rawLabel = node.label || node.curie;
  const lines = wrapLabel(rawLabel, 22, 3);
  const labelIsTruncated = lines.length > 0 && lines.at(-1)?.endsWith("…") === true;
  const showCurieLine = (node.label ?? "") !== node.curie;

  return (
    <g>
      {/* native browser tooltip — surfaces the FULL untruncated label,
          curie, and category on hover. helpful when labels get
          ellipsis-truncated in the visible layout. */}
      <title>
        {rawLabel}
        {node.category ? `\n${node.category}` : ""}
        {`\n${node.curie}`}
      </title>
      <circle
        cx={pos.x}
        cy={pos.y}
        r={NODE_R}
        fill={fill}
        stroke={stroke}
        strokeWidth={isPinned ? 3 : 2}
      />
      {/* single small glyph inside the circle: "P" for pinned, blank
          for answers. it's just a hint so the user can tell which
          node is the focal entity at a glance without depending on
          colour-vision. */}
      {isPinned && (
        <text
          x={pos.x}
          y={pos.y + 5}
          textAnchor="middle"
          fontSize="16"
          fontWeight="bold"
          fontFamily="ui-sans-serif, system-ui"
          className="fill-zinc-900"
        >
          P
        </text>
      )}
      {/* label BELOW the circle — up to 3 wrapped lines, ellipsis past
          that. when truncated, the full label is still available via
          the <title> tooltip above. */}
      {lines.map((line, i) => (
        <text
          key={i}
          x={pos.x}
          y={pos.y + NODE_R + 18 + i * 15}
          textAnchor="middle"
          fontSize="13"
          fontFamily="ui-sans-serif, system-ui"
          className="fill-zinc-900 dark:fill-zinc-100 font-semibold"
        >
          {line}
        </text>
      ))}
      {/* tiny "(hover for full name)" hint ONLY when the label was
          truncated, so the user knows there's more text behind the
          ellipsis. */}
      {labelIsTruncated && (
        <text
          x={pos.x}
          y={pos.y + NODE_R + 18 + lines.length * 15 - 3}
          textAnchor="middle"
          fontSize="9"
          fontFamily="ui-sans-serif, system-ui"
          className="fill-zinc-500 dark:fill-zinc-400 italic"
        >
          (hover for full name)
        </text>
      )}
      {/* CURIE in mono just under the label, low-emphasis. only shown
          when label is different from the curie (i.e., the entity
          actually resolved to a human-readable name). */}
      {showCurieLine && (
        <text
          x={pos.x}
          y={pos.y + NODE_R + 18 + lines.length * 15 + (labelIsTruncated ? 8 : 4)}
          textAnchor="middle"
          fontSize="11"
          fontFamily="ui-monospace, SFMono-Regular, monospace"
          className="fill-zinc-500 dark:fill-zinc-400"
        >
          {node.curie}
        </text>
      )}
      {/* category as a tiny line UNDER the curie. shown small so it
          doesn't compete with the label, but visible for users who
          aren't memorising the colour legend. */}
      {node.category && (
        <text
          x={pos.x}
          y={pos.y + NODE_R + 18 + lines.length * 15 + (labelIsTruncated ? 21 : 17)}
          textAnchor="middle"
          fontSize="9.5"
          fontFamily="ui-monospace, SFMono-Regular, monospace"
          className="fill-zinc-500 dark:fill-zinc-400 uppercase tracking-wider"
        >
          {node.category.replace(/^biolink:/, "")}
        </text>
      )}
    </g>
  );
}

// greedy word-wrap into up to `maxLines` lines of `maxLen` chars each.
// per-line truncation if a single word exceeds maxLen; ellipsis on the
// last line if the text doesn't fit. examples (maxLen=22, maxLines=3):
//   "metformin"                           → ["metformin"]
//   "type 2 diabetes mellitus"            → ["type 2 diabetes",
//                                            "mellitus"]
//   "congenital bilateral aplasia of vas
//    deferens from CFTR mutation"         → ["congenital bilateral",
//                                            "aplasia of vas",
//                                            "deferens from CFTR…"]
// the full untruncated label is preserved in the SVG <title> tooltip
// rendered alongside the node, so truncation here is purely a visual
// fit problem, not data loss.
function wrapLabel(text: string, maxLen: number, maxLines: number): string[] {
  if (text.length <= maxLen) return [text];
  const words = text.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let current = "";
  for (const w of words) {
    // word longer than maxLen? hard-break it (only once, into the
    // current line). avoids infinite loops on weird inputs like
    // "supercalifragilisticexpialidocious-protein-name".
    const wordToUse = w.length > maxLen ? w.slice(0, maxLen - 1) + "…" : w;
    const candidate = current ? `${current} ${wordToUse}` : wordToUse;
    if (candidate.length <= maxLen) {
      current = candidate;
    } else {
      if (current) lines.push(current);
      current = wordToUse;
      if (lines.length >= maxLines) {
        // already at the line limit and we still have content to
        // place — terminate the last line with an ellipsis.
        const last = lines.pop() ?? "";
        const truncated = last.length + 1 > maxLen ? last.slice(0, maxLen - 1) + "…" : last + "…";
        lines.push(truncated);
        return lines;
      }
    }
  }
  if (current) lines.push(current);
  if (lines.length > maxLines) {
    // we accumulated more than maxLines; truncate to the limit and
    // mark the last line as truncated.
    const kept = lines.slice(0, maxLines);
    const last = kept[kept.length - 1];
    kept[kept.length - 1] =
      last.length + 1 > maxLen ? last.slice(0, maxLen - 1) + "…" : last + "…";
    return kept;
  }
  return lines;
}

function categoryPalette(category: string | null): {
  fill: string;
  stroke: string;
  pinned: string;
  pinnedStroke: string;
} {
  // Tailwind colour values, with both pinned (deeper) and answer
  // (lighter) variants per category.
  switch ((category || "").replace(/^biolink:/, "")) {
    case "Disease":
      return { fill: "#fecaca", stroke: "#ef4444", pinned: "#fca5a5", pinnedStroke: "#b91c1c" };
    case "Drug":
    case "ChemicalEntity":
      return { fill: "#bfdbfe", stroke: "#3b82f6", pinned: "#93c5fd", pinnedStroke: "#1d4ed8" };
    case "Gene":
    case "Protein":
      return { fill: "#bbf7d0", stroke: "#22c55e", pinned: "#86efac", pinnedStroke: "#15803d" };
    case "PhenotypicFeature":
      return { fill: "#fde68a", stroke: "#f59e0b", pinned: "#fcd34d", pinnedStroke: "#b45309" };
    case "Pathway":
    case "BiologicalProcess":
      return { fill: "#ddd6fe", stroke: "#8b5cf6", pinned: "#c4b5fd", pinnedStroke: "#6d28d9" };
    default:
      return { fill: "#e4e4e7", stroke: "#71717a", pinned: "#d4d4d8", pinnedStroke: "#3f3f46" };
  }
}

function Legend() {
  return (
    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5 text-[11px] text-zinc-600 dark:text-zinc-400 px-1">
      <span className="font-medium text-zinc-700 dark:text-zinc-300">Edge color:</span>
      <LegendDot color="#10b981" /> verified by PubTator (≥ 1 co-mention PMID)
      <LegendDot color="#f59e0b" /> checked — no co-mention
      <LegendDot color="#a1a1aa" /> not checked / no PMIDs
    </div>
  );
}

function LegendDot({ color }: { color: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span
        className="inline-block w-3 h-3 rounded-full"
        style={{ backgroundColor: color }}
        aria-hidden
      />
    </span>
  );
}

// ---- edge details side panel ----

function EdgeDetails({ edge }: { edge: AnswerGraphEdge | null }) {
  if (!edge) {
    return (
      <div className="p-4 text-xs text-zinc-500 italic">
        Click an edge to inspect its predicate, knowledge source, supporting publications,
        and PubTator verification.
      </div>
    );
  }
  const v = edge.pubtator_verified;
  return (
    <div className="p-4 flex flex-col gap-3 text-xs">
      <div>
        <div className="font-mono text-zinc-500">edge</div>
        <div className="font-mono text-zinc-700 dark:text-zinc-200">{edge.id}</div>
      </div>
      <div>
        <div className="font-mono text-zinc-500">{shortPredicate(edge.predicate || "?")}</div>
        <div className="text-zinc-700 dark:text-zinc-200 mt-0.5">
          <span className="font-mono">{edge.source}</span>
          <span className="text-zinc-400 mx-1.5">→</span>
          <span className="font-mono">{edge.target}</span>
        </div>
      </div>

      {edge.knowledge_level && (
        <div>
          <div className="text-zinc-500">Knowledge level</div>
          <span className={`inline-flex items-center px-1.5 py-0.5 rounded font-mono ${
            edge.knowledge_level === "knowledge_assertion"
              ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300"
              : "bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200"
          }`}>
            {edge.knowledge_level}
          </span>
        </div>
      )}

      {edge.agent_type && (
        <div>
          <div className="text-zinc-500">Agent type</div>
          <span className={`inline-flex items-center px-1.5 py-0.5 rounded font-mono ${
            edge.agent_type === "manual_agent"
              ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300"
              : "bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200"
          }`}>
            {edge.agent_type}
          </span>
        </div>
      )}

      {edge.primary_knowledge_source && (
        <div>
          <div className="text-zinc-500">Primary knowledge source</div>
          <span className="font-mono text-zinc-700 dark:text-zinc-200">
            {edge.primary_knowledge_source}
          </span>
        </div>
      )}

      {v && (
        <div className="rounded border border-zinc-200 dark:border-zinc-700 p-2">
          <div className="font-medium text-zinc-700 dark:text-zinc-200 mb-1">
            PubTator co-mention check
          </div>
          {v.verified ? (
            <p className="text-emerald-700 dark:text-emerald-400 text-[11px]">
              ✓ {v.co_mention_pmids.length} of {edge.supporting_publications.length}{" "}
              PMID{edge.supporting_publications.length === 1 ? "" : "s"} mention both endpoints.
            </p>
          ) : (
            <p className="text-amber-700 dark:text-amber-400 text-[11px]">
              ✗ No supporting PMID co-mentions both endpoints
              {v.missing_pmids.length > 0 && (
                <> (PubTator did not index {v.missing_pmids.length}{" "}
                  of {edge.supporting_publications.length})</>
              )}.
            </p>
          )}
          <PmidList label="Co-mentioned" pmids={v.co_mention_pmids} />
          <PmidList label="Subject only" pmids={v.subject_only_pmids} />
          <PmidList label="Object only" pmids={v.object_only_pmids} />
          <PmidList label="Not indexed" pmids={v.missing_pmids} />
        </div>
      )}

      {edge.supporting_text_snippets.length > 0 && (
        <div>
          <div className="text-zinc-500 mb-1">Supporting text</div>
          <ul className="flex flex-col gap-1.5">
            {edge.supporting_text_snippets.map((s, i) => (
              <li key={i} className="rounded border border-zinc-200 dark:border-zinc-700 p-2">
                <a
                  href={`https://pubmed.ncbi.nlm.nih.gov/${s.pmid.replace(/^PMID:/, "")}/`}
                  target="_blank"
                  rel="noreferrer"
                  className="font-mono text-[11px] text-blue-600 dark:text-blue-400 hover:underline"
                >
                  {s.pmid}
                </a>
                {s.date && <span className="text-zinc-400 text-[11px] ml-1.5">{s.date}</span>}
                {s.sentence && (
                  <p className="mt-1 text-zinc-700 dark:text-zinc-300 leading-relaxed italic">
                    “{s.sentence}”
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function PmidList({ label, pmids }: { label: string; pmids: string[] }) {
  if (pmids.length === 0) return null;
  return (
    <div className="mt-1.5">
      <span className="text-zinc-500 text-[11px]">{label}: </span>
      {pmids.map((p, i) => (
        <span key={p}>
          <a
            href={`https://pubmed.ncbi.nlm.nih.gov/${p.replace(/^PMID:/, "")}/`}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-[11px] text-blue-600 dark:text-blue-400 hover:underline"
          >
            {p}
          </a>
          {i < pmids.length - 1 && <span className="text-zinc-400">, </span>}
        </span>
      ))}
    </div>
  );
}
