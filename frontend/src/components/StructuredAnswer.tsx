"use client";

// renders the Stage 15 LLM summary as a 4-section card structure:
//   ## Answer       → plain prose
//   ## Evidence     → ONE evidence card per picked answer entity (replaces
//                     the LLM's flat bullet list). each card has a tiny
//                     pinned→edge→answer mini-graph showing the PloverDB
//                     edge ids, plus knowledge_level, primary KS, PMIDs,
//                     and the LLM's per-entity prose extracted from its
//                     original bullet for that entity.
//   ## Confidence   → plain prose
//   ## Limitations  → plain prose
//
// the rationale: the user wants the graph(s) to live INSIDE the
// answer/evidence structure, one per relation, instead of one big star
// graph as a separate card. each evidence card is a focused "this
// answer entity is connected to the query entity by these PloverDB
// edges" cell, with the per-edge provenance immediately visible.

import { useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AnswerGraphView, AnswerGraphEdge, AnswerGraphNode } from "@/lib/api";
import { linkifyCitations, linkifyCURIEs } from "@/lib/linkify";

type Props = {
  // markdown produced by Stage 15 — the four-section Answer / Evidence /
  // Confidence / Limitations template. we parse it into sections; the
  // Evidence section is REPLACED with our card stack.
  explanation: string;
  // Stage 13 structured view — pinned node, picked answer nodes, edges
  // with provenance + PubTator verification.
  view: AnswerGraphView;
};

export function StructuredAnswer({ explanation, view }: Props) {
  // split the markdown into sections keyed by `## Heading`. anything
  // before the first `##` is "preamble" (rare; usually empty). each
  // section value is the prose after the heading, trimmed.
  const sections = useMemo(() => parseSections(explanation), [explanation]);

  // pull the prose sentence the LLM wrote for each answer CURIE out of
  // the Evidence section's bullet list, so we can render it INSIDE the
  // corresponding evidence card alongside the mini-graph.
  const proseByCurie = useMemo(
    () => extractEvidenceProseByCurie(sections.Evidence ?? "", view.answer_nodes),
    [sections.Evidence, view.answer_nodes],
  );

  // group edges by their non-pinned endpoint. each answer node may have
  // 1-N edges connecting it to the pinned node (multiple supporting
  // edges from different sources). we want one card per answer node,
  // listing all of its edges inside.
  const edgesByAnswerCurie = useMemo(
    () => groupEdgesByAnswerCurie(view.edges, view.pinned_node.curie),
    [view.edges, view.pinned_node.curie],
  );

  return (
    <article className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 overflow-hidden">
      <div className="p-6 prose prose-zinc dark:prose-invert prose-sm sm:prose-base max-w-none">
        {/* ## Answer — plain prose */}
        {sections.Answer && (
          <section>
            <h2 className="!mt-0">Answer</h2>
            <Markdown text={sections.Answer} />
          </section>
        )}
      </div>

      {/* ## Evidence — per-entity cards (the meat of the change) */}
      <div className="border-t border-zinc-200 dark:border-zinc-800 bg-zinc-50/60 dark:bg-zinc-950/40">
        <div className="px-6 pt-5 pb-2">
          <h2 className="text-lg font-semibold tracking-tight">Evidence</h2>
          <p className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">
            {view.answer_nodes.length} answer{view.answer_nodes.length === 1 ? "" : "s"},
            each connected to the pinned entity by one or more PloverDB edges.
          </p>
          <PubTatorExplainer />
        </div>
        <ul className="flex flex-col gap-3 px-6 pb-6">
          {view.answer_nodes.map((answer) => (
            <li key={answer.curie}>
              <EvidenceCard
                pinned={view.pinned_node}
                answer={answer}
                edges={edgesByAnswerCurie.get(answer.curie) ?? []}
                prose={proseByCurie.get(answer.curie) ?? null}
              />
            </li>
          ))}
        </ul>
      </div>

      <div className="px-6 py-5 border-t border-zinc-200 dark:border-zinc-800 prose prose-zinc dark:prose-invert prose-sm sm:prose-base max-w-none">
        {sections.Confidence && (
          <section>
            <h2 className="!mt-0">Confidence</h2>
            <Markdown text={sections.Confidence} />
          </section>
        )}
        {sections.Limitations && (
          <section className="!mt-4">
            <h2>Limitations</h2>
            <Markdown text={sections.Limitations} />
          </section>
        )}
      </div>
    </article>
  );
}

// ---- per-entity evidence card ----

function EvidenceCard({
  pinned,
  answer,
  edges,
  prose,
}: {
  pinned: AnswerGraphNode;
  answer: AnswerGraphNode;
  edges: AnswerGraphEdge[];
  prose: string | null;
}) {
  // compute the "best" provenance line across the edges for this
  // answer — knowledge_level (highest tier wins) + KS list. small
  // ribbon at the card header so the user sees evidence quality at
  // a glance without opening the per-edge details.
  const headerProvenance = summarizeProvenance(edges);
  // PubTator verification summary: at least one edge verified?
  const pubtatorBest = bestPubTatorStatus(edges);

  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900">
      {/* card header: answer label + provenance ribbon */}
      <div className="flex items-start justify-between gap-3 px-4 py-3 border-b border-zinc-200 dark:border-zinc-800">
        <div className="min-w-0">
          <div className="text-base font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
            {answer.label || answer.curie}
          </div>
          <a
            href={`https://bioregistry.io/${encodeURIComponent(answer.curie)}`}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-xs text-blue-600 dark:text-blue-400 hover:underline"
          >
            {answer.curie}
          </a>
          {answer.category && (
            <span className="ml-2 text-[10px] uppercase tracking-wider font-mono text-zinc-500">
              {answer.category.replace(/^biolink:/, "")}
            </span>
          )}
        </div>
        <div className="flex flex-col items-end gap-1 shrink-0">
          {headerProvenance.knowledge_level && (
            <KnowledgeLevelBadge level={headerProvenance.knowledge_level} />
          )}
          <PubTatorBadge status={pubtatorBest} />
        </div>
      </div>

      {/* mini-graph: pinned → edges → answer. horizontal layout, three
          columns wide. clicking an edge highlights its provenance below. */}
      <div className="px-4 py-4">
        <MiniGraph pinned={pinned} answer={answer} edges={edges} />
      </div>

      {/* LLM's per-entity prose (extracted from the markdown bullet
          line for this answer) — the natural-language summary stays
          with its graph, not separated as a footnote. */}
      {prose && (
        <div className="px-4 pb-3 text-sm text-zinc-700 dark:text-zinc-300 leading-relaxed">
          <Markdown text={prose} />
        </div>
      )}

      {/* per-edge accordion: each edge in this card lists its
          predicate, edge id, PMIDs, supporting-text snippets, and
          per-edge PubTator co-mention check. */}
      <details className="border-t border-zinc-200 dark:border-zinc-800 group">
        <summary className="cursor-pointer px-4 py-2.5 text-xs font-medium text-zinc-600 dark:text-zinc-400 hover:bg-zinc-50 dark:hover:bg-zinc-900/60 select-none flex items-center gap-2">
          <ChevronRight />
          {edges.length} supporting edge{edges.length === 1 ? "" : "s"} · provenance
        </summary>
        <ul className="px-4 pb-4 pt-1 flex flex-col gap-3">
          {edges.map((e) => (
            <EdgeDetailRow key={e.id} edge={e} />
          ))}
        </ul>
      </details>
    </div>
  );
}

// ---- the inline mini-graph ----

function MiniGraph({
  pinned,
  answer,
  edges,
}: {
  pinned: AnswerGraphNode;
  answer: AnswerGraphNode;
  edges: AnswerGraphEdge[];
}) {
  // ONE arrow per node-pair (NOT one parallel line per edge). multiple
  // supporting edges between the same nodes are conceptually one
  // relation supported by N sources — drawing N parallel lines made
  // labels overlap and didn't add information. instead we stack all
  // edge IDs ABOVE the single arrow with the canonical
  // "PloverDB-edge:N" notation so the user knows what kind of ID it
  // is, and the predicate sits as a chip BELOW the arrow line.
  //
  // SVG height is computed from the actual content so labels never
  // clip: top band for the stacked edge IDs, middle for the arrow
  // and predicate chip, bottom for each node's label + curie +
  // category line. width is fixed at 640 and scales via class="w-full"
  // so it always fits the card column.

  // unique predicates list (e.g. usually all "treats" — collapse to one)
  const uniquePredicates = Array.from(
    new Set(edges.map((e) => (e.predicate || "?").replace(/^biolink:/, ""))),
  );

  // height budget computed from real content. the bottom band has to
  // fit, IN ORDER below the circle: 16px gap → N wrapped label lines
  // (15px each) → CURIE line (~15px) → category line (~15px) → ~8px
  // bottom padding for font descenders + the card border. NODE_R
  // accounts for the half of the circle below ARROW_Y. labels are
  // wrapped here (instead of inside MiniNode) so we know the actual
  // line count before computing H — a 3-line label needs ~30px more
  // bottom band than a 1-line label, and an over-eager constant for
  // the worst case adds dead space for short-label cards.
  const W = 640;
  const TOP_PADDING = 16;
  const EDGE_ID_LINE_H = 16;
  const TOP_BAND = TOP_PADDING + edges.length * EDGE_ID_LINE_H + 12;
  const NODE_R = 32;
  const ARROW_Y = TOP_BAND + NODE_R + 4;
  const pinnedLines = wrapLabel(pinned.label || pinned.curie, 18, 3);
  const answerLines = wrapLabel(answer.label || answer.curie, 18, 3);
  const maxLabelLines = Math.max(pinnedLines.length, answerLines.length, 1);
  const NODE_BOTTOM_BAND =
    NODE_R       // bottom half of the circle (sits below ARROW_Y)
    + 16         // gap between circle and first label line
    + maxLabelLines * 15  // wrapped label
    + 18         // CURIE line (offset 4 below last label, ~14 line-height)
    + 16         // category line (offset 17 below CURIE)
    + 8;         // font-descender + bottom padding so nothing hugs the border
  const H = ARROW_Y + NODE_BOTTOM_BAND;

  const LEFT_X = 90;
  const RIGHT_X = W - 90;
  const palettePinned = categoryPalette(pinned.category);
  const paletteAnswer = categoryPalette(answer.category);

  // arrow colour: emerald if ANY edge is PubTator-verified, amber if
  // checked-but-none-verified, zinc otherwise. matches the badge in
  // the card header so the visual story is consistent.
  const arrowStroke = useMemo(() => {
    const anyVerified = edges.some((e) => e.pubtator_verified?.verified);
    if (anyVerified) return "#10b981";
    const anyChecked = edges.some((e) => e.pubtator_verified !== null);
    if (anyChecked) return "#f59e0b";
    return "#71717a";
  }, [edges]);

  // unique marker id per-instance so multiple MiniGraphs on the same
  // page (one per evidence card) don't share a single <marker> def
  // and overwrite each other's colour.
  const arrowId = useMemo(
    () => `arrow-${pinned.curie}-${answer.curie}`.replace(/[^a-zA-Z0-9_-]/g, "_"),
    [pinned.curie, answer.curie],
  );

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid meet"
      className="w-full max-w-full"
      style={{ maxHeight: H }}
      role="img"
      aria-label={`Mini graph: ${pinned.curie} to ${answer.curie}`}
    >
      <defs>
        <marker
          id={arrowId}
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="6"
          markerHeight="6"
          orient="auto-start-reverse"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" fill={arrowStroke} />
        </marker>
      </defs>

      {/* stacked PloverDB edge IDs ABOVE the arrow, one per line, with
          the full "PloverDB-edge:N" label so the user knows what kind
          of identifier it is */}
      {edges.map((edge, i) => (
        <g key={edge.id}>
          <title>
            {edge.predicate ?? "(no predicate)"} · PloverDB-edge:{edge.id}
            {edge.pubtator_verified?.verified ? " · PubTator: verified" : ""}
          </title>
          <text
            x={W / 2}
            y={TOP_PADDING + i * EDGE_ID_LINE_H + 11}
            textAnchor="middle"
            fontSize="11"
            fontFamily="ui-monospace, SFMono-Regular, monospace"
            className="fill-zinc-600 dark:fill-zinc-400"
          >
            PloverDB-edge:{edge.id}
          </text>
        </g>
      ))}

      {/* the single arrow between pinned and answer */}
      <line
        x1={LEFT_X + NODE_R}
        y1={ARROW_Y}
        x2={RIGHT_X - NODE_R - 4 /* leave room for arrowhead */}
        y2={ARROW_Y}
        stroke={arrowStroke}
        strokeWidth={2.5}
        markerEnd={`url(#${arrowId})`}
      />

      {/* predicate chip BELOW the arrow line. if multiple distinct
          predicates exist across the edges, join them with " / " so
          we surface the variation. */}
      <PredicateChip
        x={W / 2}
        y={ARROW_Y + 16}
        text={uniquePredicates.join(" / ")}
      />

      {/* pinned node — left side, color-coded as "pinned" (deeper hue) */}
      <MiniNode
        node={pinned}
        x={LEFT_X}
        y={ARROW_Y}
        r={NODE_R}
        fill={palettePinned.pinned}
        stroke={palettePinned.pinnedStroke}
        role="pinned"
      />
      {/* answer node — right side */}
      <MiniNode
        node={answer}
        x={RIGHT_X}
        y={ARROW_Y}
        r={NODE_R}
        fill={paletteAnswer.fill}
        stroke={paletteAnswer.stroke}
        role="answer"
      />
    </svg>
  );
}

function MiniNode({
  node,
  x,
  y,
  r,
  fill,
  stroke,
  role,
}: {
  node: AnswerGraphNode;
  x: number;
  y: number;
  r: number;
  fill: string;
  stroke: string;
  role: "pinned" | "answer";
}) {
  // wrap into up to 3 lines × 18 chars; full label in tooltip.
  const lines = wrapLabel(node.label || node.curie, 18, 3);
  return (
    <g>
      <title>{`${role.toUpperCase()}: ${node.label || node.curie}\n${node.curie}${node.category ? "\n" + node.category : ""}`}</title>
      <circle
        cx={x}
        cy={y}
        r={r}
        fill={fill}
        stroke={stroke}
        strokeWidth={role === "pinned" ? 3 : 2}
      />
      {/* tiny role label INSIDE the circle — readable, no overlap.
          replaces the cryptic "P" glyph with explicit "PINNED" / "ANSWER"
          text so the user knows what they're looking at at a glance.
          the node fill colours (categoryPalette) are pastels in BOTH
          themes, so the text inside must always be dark — a "dark:"
          variant that lightens here ends up white-on-pastel and is
          unreadable in dark mode. fill-zinc-900 holds strong contrast
          against every pastel in the palette. */}
      <text
        x={x}
        y={y + 4}
        textAnchor="middle"
        fontSize="9"
        fontWeight="bold"
        fontFamily="ui-sans-serif, system-ui"
        className="fill-zinc-900"
      >
        {role.toUpperCase()}
      </text>
      {/* label below */}
      {lines.map((line, i) => (
        <text
          key={i}
          x={x}
          y={y + r + 16 + i * 15}
          textAnchor="middle"
          fontSize="12"
          fontFamily="ui-sans-serif, system-ui"
          className="fill-zinc-900 dark:fill-zinc-100 font-semibold"
        >
          {line}
        </text>
      ))}
      {/* CURIE */}
      <text
        x={x}
        y={y + r + 16 + lines.length * 15 + 4}
        textAnchor="middle"
        fontSize="10"
        fontFamily="ui-monospace, SFMono-Regular, monospace"
        className="fill-zinc-500 dark:fill-zinc-400"
      >
        {node.curie}
      </text>
      {/* category */}
      {node.category && (
        <text
          x={x}
          y={y + r + 16 + lines.length * 15 + 17}
          textAnchor="middle"
          fontSize="9"
          fontFamily="ui-monospace, SFMono-Regular, monospace"
          className="fill-zinc-500 dark:fill-zinc-400 uppercase tracking-wider"
        >
          {node.category.replace(/^biolink:/, "")}
        </text>
      )}
    </g>
  );
}

function PredicateChip({ x, y, text }: { x: number; y: number; text: string }) {
  const w = text.length * 6.5 + 14;
  const h = 18;
  return (
    <g pointerEvents="none">
      <rect
        x={x - w / 2}
        y={y - h / 2}
        width={w}
        height={h}
        rx={4}
        fill="white"
        stroke="#a1a1aa"
        strokeWidth={1.25}
        className="dark:fill-zinc-900 dark:stroke-zinc-600"
      />
      <text
        x={x}
        y={y + 4}
        textAnchor="middle"
        fontSize="11"
        fontFamily="ui-monospace, SFMono-Regular, monospace"
        className="fill-zinc-700 dark:fill-zinc-200"
      >
        {text}
      </text>
    </g>
  );
}

// ---- per-edge provenance row inside the collapsible accordion ----

function EdgeDetailRow({ edge }: { edge: AnswerGraphEdge }) {
  // each edge row is one PloverDB knowledge-graph record. it has its
  // own predicate, knowledge_level, primary knowledge source (KS), and
  // optional list of supporting PMIDs assigned BY THAT KS. lay this
  // out with explicit field labels so the reader can map every value
  // to its meaning without prior knowledge of TRAPI / Biolink shape.
  return (
    <li className="text-xs rounded border border-zinc-200 dark:border-zinc-700 bg-zinc-50/40 dark:bg-zinc-900/40 p-3">
      {/* edge-id header */}
      <div className="font-mono text-[11px] text-zinc-700 dark:text-zinc-200 mb-2 font-semibold">
        PloverDB-edge:{edge.id}
      </div>

      {/* labeled field rows — each one explicit about what it is */}
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[11px] mb-2">
        <dt className="text-zinc-500">Predicate</dt>
        <dd className="font-mono text-zinc-700 dark:text-zinc-200">
          {edge.predicate || "(not set)"}
        </dd>

        <dt className="text-zinc-500">Knowledge level</dt>
        <dd>
          {edge.knowledge_level ? (
            <KnowledgeLevelBadge level={edge.knowledge_level} small />
          ) : (
            <span className="text-zinc-400 italic">not recorded</span>
          )}
        </dd>

        <dt className="text-zinc-500">Agent type</dt>
        <dd className="font-mono text-zinc-700 dark:text-zinc-200">
          {edge.agent_type || (
            <span className="text-zinc-400 italic">not recorded</span>
          )}
        </dd>

        <dt className="text-zinc-500">Source</dt>
        <dd className="font-mono text-zinc-700 dark:text-zinc-200">
          {edge.primary_knowledge_source || (
            <span className="text-zinc-400 italic">not recorded</span>
          )}
        </dd>

        <dt className="text-zinc-500">PMIDs cited by source</dt>
        <dd className="font-mono text-zinc-700 dark:text-zinc-200">
          {edge.supporting_publications.length === 0 ? (
            <span className="text-zinc-400 italic">
              none (curated database evidence — not paper-derived)
            </span>
          ) : (
            edge.supporting_publications.length
          )}
        </dd>
      </dl>

      {edge.pubtator_verified && (
        <div className="mt-2 pt-2 border-t border-zinc-200 dark:border-zinc-700">
          <PubTatorEdgeDetail edge={edge} />
        </div>
      )}

      {edge.supporting_text_snippets.length > 0 && (
        <div className="mt-2 pt-2 border-t border-zinc-200 dark:border-zinc-700">
          <div className="text-zinc-500 text-[11px] mb-1.5">
            Supporting text from cited PMIDs
            <span className="text-zinc-400 ml-1.5">
              (extracted by KG2c from the source&apos;s annotation)
            </span>
          </div>
          <ul className="flex flex-col gap-1.5">
            {edge.supporting_text_snippets.map((s, i) => (
              <li key={i} className="rounded border border-zinc-200 dark:border-zinc-700 p-2 bg-white dark:bg-zinc-950/40">
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
                  <p className="mt-1 text-zinc-700 dark:text-zinc-300 italic leading-relaxed">
                    &ldquo;{s.sentence}&rdquo;
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </li>
  );
}

function PubTatorEdgeDetail({ edge }: { edge: AnswerGraphEdge }) {
  const v = edge.pubtator_verified!;
  const totalChecked = edge.supporting_publications.length;
  return (
    <div className="text-[11px]">
      <span className="text-zinc-500">PubTator independent NER check: </span>
      {v.verified ? (
        <span className="text-emerald-700 dark:text-emerald-400">
          ✓ {v.co_mention_pmids.length} of {totalChecked} cited PMID
          {totalChecked === 1 ? "" : "s"} mentions both endpoints
        </span>
      ) : (
        <>
          <span className="text-amber-700 dark:text-amber-400">
            ✗ none of the {totalChecked} cited PMID{totalChecked === 1 ? "" : "s"} co-mentions both endpoints
            {v.missing_pmids.length > 0 && (
              <> · {v.missing_pmids.length} not indexed by PubTator</>
            )}
          </span>
          <span className="text-zinc-500 ml-1">
            — this is a signal, not a rejection: see the &ldquo;About the badges&rdquo; note above.
          </span>
        </>
      )}
    </div>
  );
}

// ---- small components ----

function KnowledgeLevelBadge({ level, small }: { level: string; small?: boolean }) {
  const tone =
    level === "knowledge_assertion"
      ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300"
      : level === "prediction"
        ? "bg-purple-100 text-purple-800 dark:bg-purple-950/60 dark:text-purple-300"
        : level === "statistical_association"
          ? "bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
          : "bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200";
  const size = small ? "text-[10px] px-1.5 py-0.5" : "text-[11px] px-2 py-0.5";
  return <span className={`font-mono ${size} rounded ${tone}`}>{level}</span>;
}

function PubTatorBadge({ status }: { status: "verified" | "unverified" | "na" }) {
  // explicit wording — not just ✓/✗ — so a first-time researcher
  // reads the badge as a SIGNAL with context, not a verdict on the
  // answer. the three states (curated / verified / no co-mention)
  // have very different meanings (see PubTatorExplainer below).
  if (status === "na") {
    return (
      <span
        className="inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
        title="No supporting PMIDs to check (often DrugBank/DrugCentral curated assertions — among the strongest evidence kinds, just not paper-derived). See 'About these badges'."
      >
        <span className="w-1.5 h-1.5 rounded-full bg-zinc-400" />
        curated source · n/a
      </span>
    );
  }
  if (status === "verified") {
    return (
      <span
        className="inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300"
        title="At least one cited PMID co-mentions both endpoints, per PubTator's independent NER. Strong external confirmation of the cited evidence."
      >
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
        co-mention verified
      </span>
    );
  }
  return (
    <span
      className="inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
      title="PMIDs were checked but none co-mention both endpoints. May reflect a PubTator NER miss, a CURIE-namespace mismatch, or a citation that doesn't directly discuss the relation — not necessarily weak evidence. Click into the edge to inspect the specific PMID."
    >
      <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
      no co-mention found
    </span>
  );
}


// inline collapsible explanation that sits at the top of the Evidence
// section. closed by default so it doesn't dominate the page, but
// PROMINENT enough (info icon + "About these badges") that researchers
// can find it. when expanded, lays out the three PubTator states with
// the same visual badges as on the cards + clear plain-English text.
function PubTatorExplainer() {
  return (
    <details className="mt-3 rounded border border-zinc-200 dark:border-zinc-800 bg-white/60 dark:bg-zinc-900/40 text-xs leading-relaxed">
      <summary className="cursor-pointer px-3 py-2 flex items-center gap-2 select-none text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100/60 dark:hover:bg-zinc-800/40 rounded">
        <InfoIcon />
        <span className="font-medium">How to read this evidence: edges, predicates, PMIDs, badges</span>
        <span className="text-zinc-500 font-normal">— click for explanation</span>
      </summary>
      <div className="px-4 pb-4 pt-2 flex flex-col gap-4 text-zinc-700 dark:text-zinc-300">
        <section>
          <div className="font-semibold mb-1">What is a PloverDB edge?</div>
          <p>
            An <strong>edge</strong> is one record in the RTX-KG2.10.2c knowledge graph
            that asserts a relation between two biomedical entities. Each card above shows
            ONE answer entity, but each answer is typically connected to the pinned entity
            by <strong>multiple edges</strong> — different upstream sources independently
            asserted the same relation. Each PloverDB edge gets its own internal ID
            (e.g. <code className="font-mono">PloverDB-edge:46640401</code>); these are
            stable handles that let you trace any claim back to its source record.
          </p>
        </section>
        <section>
          <div className="font-semibold mb-1">What is a predicate?</div>
          <p>
            The <strong>predicate</strong> is the type of relation, drawn from{" "}
            <a
              href="https://biolink.github.io/biolink-model/"
              target="_blank"
              rel="noreferrer"
              className="text-blue-600 dark:text-blue-400 hover:underline"
            >
              the Biolink Model
            </a>
            . Examples: <code className="font-mono">biolink:treats</code>,{" "}
            <code className="font-mono">biolink:gene_associated_with_condition</code>,{" "}
            <code className="font-mono">biolink:preventative_for_condition</code>,{" "}
            <code className="font-mono">biolink:physically_interacts_with</code>. The
            predicate tells you what KIND of relation is being asserted; same two entities
            can be connected by different predicates from different sources.
          </p>
        </section>
        <section>
          <div className="font-semibold mb-1">Where do the PMIDs come from?</div>
          <p>
            Each edge can carry a list of <strong>supporting PMIDs</strong> — these are
            <strong> attached to the edge by the upstream knowledge source </strong>
            (DrugBank, DrugCentral, SemMedDB, etc.) that originally asserted the relation.
            They are the literature evidence that source cited when adding the edge to KG2c.
            PloverDB stores them on the edge as the{" "}
            <code className="font-mono">biolink:publications</code> attribute.
            Edges from <em>curated databases</em> (DrugBank, FDA labels) often have ZERO
            supporting PMIDs — the database itself is the evidence. Edges from{" "}
            <em>text-mined sources</em> (SemMedDB, RTX-KG2 SemMedDB ingest) carry PMIDs of
            the abstracts the relation was mined from.
          </p>
        </section>
        <section>
          <div className="font-semibold mb-1">knowledge_level (Biolink)</div>
          <p>
            How the assertion was made. Strongest first:{" "}
            <code className="font-mono">knowledge_assertion</code> (human-curated, e.g. DrugBank){" "}
            &gt; <code className="font-mono">logical_entailment</code> &gt;{" "}
            <code className="font-mono">prediction</code> &gt;{" "}
            <code className="font-mono">statistical_association</code> &gt;{" "}
            <code className="font-mono">observation</code>. Stage 11 of the pipeline only picks
            answers from the strongest tier present in the response.
          </p>
        </section>
        <section>
          <div className="font-semibold mb-2">PubTator independent verification</div>
          <p className="mb-2">
            <a
              href="https://www.ncbi.nlm.nih.gov/research/pubtator3/"
              target="_blank"
              rel="noreferrer"
              className="text-blue-600 dark:text-blue-400 hover:underline"
            >
              PubTator3
            </a>{" "}
            is NLM&apos;s biomedical NER pipeline that independently re-annotates PubMed abstracts
            with normalized biomedical CURIEs. For every PloverDB edge that cites PMIDs, we ask
            PubTator: <em>does any of these PMIDs actually co-mention both endpoints?</em> This
            doesn&apos;t replace PloverDB&apos;s evidence — it adds a second-opinion signal.
            Three possible states:
          </p>
          <ul className="flex flex-col gap-2 pl-1">
            <li className="flex items-start gap-2">
              <span className="inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300 shrink-0 mt-0.5">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
                co-mention verified
              </span>
              <span>
                At least one cited PMID co-mentions both endpoints per PubTator&apos;s NER.
                Strong external confirmation of the evidence.
              </span>
            </li>
            <li className="flex items-start gap-2">
              <span className="inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300 shrink-0 mt-0.5">
                <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
                no co-mention found
              </span>
              <span>
                PMIDs were checked but none co-mention both endpoints. This can be
                (a) a PubTator NER miss on an older or atypically-spelled entity,
                (b) a CURIE-namespace mismatch between KG2c and PubTator&apos;s MeSH IDs,
                (c) a cited paper that mentions the entities only in passing,
                or (d) a genuinely weak citation. Click into the edge to read the specific PMID
                before drawing a conclusion.{" "}
                <strong>This is NOT a rejection of the answer</strong> — it&apos;s a signal to
                investigate the citation.
              </span>
            </li>
            <li className="flex items-start gap-2">
              <span className="inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300 shrink-0 mt-0.5">
                <span className="w-1.5 h-1.5 rounded-full bg-zinc-400" />
                curated source · n/a
              </span>
              <span>
                The edge has zero supporting PMIDs to check — the evidence is structured-DB
                curation (DrugBank, DrugCentral, FDA labels) rather than literature-derived.
                This is <strong>not a weakness</strong>: curated databases are often the
                strongest evidence kind, just not paper-cited.
              </span>
            </li>
          </ul>
        </section>
      </div>
    </details>
  );
}


function InfoIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden className="shrink-0">
      <path
        fillRule="evenodd"
        d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-11a1 1 0 11-2 0 1 1 0 012 0zm-1 2a1 1 0 011 1v4a1 1 0 11-2 0v-4a1 1 0 011-1z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function Markdown({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        // PMIDs / CURIEs in prose get the same linkify treatment as the
        // standalone MarkdownAnswer — but we use a tighter inline style
        // since these snippets live inside cards, not at full width.
        // PMID links specifically render as pill-style badges so it's
        // obvious each one is an individually clickable citation.
        a: ({ href, children }) => {
          const isPmid =
            typeof href === "string" &&
            href.includes("pubmed.ncbi.nlm.nih.gov");
          if (isPmid) {
            return (
              <a
                href={href}
                target="_blank"
                rel="noreferrer"
                title="Open this paper on PubMed"
                className="no-underline inline-flex items-center font-mono text-[0.78em] leading-none text-blue-700 dark:text-blue-300 bg-blue-50 dark:bg-blue-950/40 hover:bg-blue-100 dark:hover:bg-blue-900/60 border border-blue-200 dark:border-blue-900 px-1.5 py-0.5 mx-0.5 rounded transition-colors"
              >
                {children}
              </a>
            );
          }
          return (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              className="text-blue-600 dark:text-blue-400 hover:underline font-mono text-[0.95em]"
            >
              {children}
            </a>
          );
        },
        code: ({ children }) => (
          <code className="font-mono text-[0.9em] px-1 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800">
            {children}
          </code>
        ),
      }}
    >
      {linkifyCitations(linkifyCURIEs(text))}
    </ReactMarkdown>
  );
}

function ChevronRight() {
  return (
    <svg
      className="text-zinc-400 shrink-0 transition-transform group-open:rotate-90"
      width="12"
      height="12"
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

// ---- helpers ----

function parseSections(md: string): Record<string, string> {
  // split on `## Heading` (level-2 markdown). returns { Answer:..., Evidence:..., ... }
  const out: Record<string, string> = {};
  const lines = md.split(/\r?\n/);
  let current = "";
  let buf: string[] = [];
  for (const line of lines) {
    const m = line.match(/^##\s+(.+?)\s*$/);
    if (m) {
      if (current) out[current] = buf.join("\n").trim();
      current = m[1];
      buf = [];
    } else {
      buf.push(line);
    }
  }
  if (current) out[current] = buf.join("\n").trim();
  return out;
}

function extractEvidenceProseByCurie(
  evidenceMd: string,
  answers: AnswerGraphNode[],
): Map<string, string> {
  // each bullet in the Evidence section is one entity. parse bullets,
  // detect which CURIE each one references, and stash the prose.
  // matching strategy: each answer's CURIE is in the answers array;
  // we look for that CURIE substring inside each bullet.
  const out = new Map<string, string>();
  if (!evidenceMd) return out;
  // split into bullets (lines starting with "- " or "* "), keeping
  // continuation lines that don't start with the bullet marker.
  const bullets: string[] = [];
  let cur = "";
  for (const line of evidenceMd.split(/\r?\n/)) {
    if (/^\s*[-*]\s+/.test(line)) {
      if (cur) bullets.push(cur);
      cur = line.replace(/^\s*[-*]\s+/, "");
    } else if (cur) {
      cur += "\n" + line;
    }
  }
  if (cur) bullets.push(cur);
  for (const b of bullets) {
    const bLower = b.toLowerCase();
    for (const a of answers) {
      if (bLower.includes(a.curie.toLowerCase())) {
        out.set(a.curie, b.trim());
        break;
      }
    }
  }
  return out;
}

function groupEdgesByAnswerCurie(
  edges: AnswerGraphEdge[],
  pinnedCurie: string,
): Map<string, AnswerGraphEdge[]> {
  const out = new Map<string, AnswerGraphEdge[]>();
  for (const e of edges) {
    const other = e.source === pinnedCurie ? e.target : e.source;
    if (!out.has(other)) out.set(other, []);
    out.get(other)!.push(e);
  }
  return out;
}

function summarizeProvenance(edges: AnswerGraphEdge[]): { knowledge_level: string | null } {
  // highest tier wins. mirrors the Stage 11 selector's tier order.
  const order = [
    "knowledge_assertion",
    "logical_entailment",
    "prediction",
    "statistical_association",
    "observation",
    "not_provided",
  ];
  let best: string | null = null;
  let bestIdx = order.length;
  for (const e of edges) {
    const k = e.knowledge_level;
    if (!k) continue;
    const idx = order.indexOf(k);
    if (idx >= 0 && idx < bestIdx) {
      best = k;
      bestIdx = idx;
    }
  }
  return { knowledge_level: best };
}

function bestPubTatorStatus(edges: AnswerGraphEdge[]): "verified" | "unverified" | "na" {
  let anyChecked = false;
  for (const e of edges) {
    const v = e.pubtator_verified;
    if (v === null || v === undefined) continue;
    anyChecked = true;
    if (v.verified) return "verified";
  }
  return anyChecked ? "unverified" : "na";
}

function categoryPalette(category: string | null) {
  switch ((category || "").replace(/^biolink:/, "")) {
    case "Disease":
      return { fill: "#fecaca", stroke: "#ef4444", pinned: "#fca5a5", pinnedStroke: "#b91c1c" };
    case "Drug":
    case "ChemicalEntity":
    case "SmallMolecule":
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

function wrapLabel(text: string, maxLen: number, maxLines: number): string[] {
  if (text.length <= maxLen) return [text];
  const words = text.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let current = "";
  for (const w of words) {
    const wordToUse = w.length > maxLen ? w.slice(0, maxLen - 1) + "…" : w;
    const candidate = current ? `${current} ${wordToUse}` : wordToUse;
    if (candidate.length <= maxLen) {
      current = candidate;
    } else {
      if (current) lines.push(current);
      current = wordToUse;
      if (lines.length >= maxLines) {
        const last = lines.pop() ?? "";
        const truncated = last.length + 1 > maxLen ? last.slice(0, maxLen - 1) + "…" : last + "…";
        lines.push(truncated);
        return lines;
      }
    }
  }
  if (current) lines.push(current);
  return lines.slice(0, maxLines);
}

// linkifyCitations + linkifyCURIEs are imported from @/lib/linkify so
// the in-app prose AND the PDF / Markdown exports share one
// implementation. see that file for the regex + outer-bracket rationale.
