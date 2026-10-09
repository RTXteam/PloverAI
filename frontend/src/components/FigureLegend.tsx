"use client";

// the key and the caption of the reasoning figure (ReasoningGraph):
// node kinds, the question entity, line styles
// and widths, and one paragraph saying what the figure shows.

import { useMemo } from "react";
import type { ReasoningGraph as Graph } from "@/lib/api";
import { Glyph } from "@/components/ReasoningGraph";
import { STRENGTH_ORDER, buildLinks } from "@/lib/reasoningLinks";
import { EVIDENCE_STYLE, KIND_LABEL, kindOf, linkWidth, type Kind } from "@/lib/vizPalette";

// the key to the figure, set apart from the caption so it reads at a
// glance: node kinds, the question entity, the answers, line styles
// and widths, in ink at the caption's size.
export function FigureLegend({ graph }: { graph: Graph }) {
  const kinds = useMemo(() => {
    const present = new Set<Kind>(graph.nodes.map((n) => kindOf(n.category)));
    return (Object.keys(KIND_LABEL) as Kind[]).filter((k) => present.has(k));
  }, [graph]);
  const links = useMemo(() => buildLinks(graph), [graph]);
  const strengths = STRENGTH_ORDER.filter((s) => links.some((l) => l.strength === s));
  const maxFacts = Math.max(1, ...links.map((l) => l.facts.length));

  return (
    <div
      className="grid gap-x-6 gap-y-1.5 rounded-md border px-3 py-2.5 text-[12px] sm:grid-cols-[auto_1fr]"
      style={{ borderColor: "var(--viz-rule)", background: "var(--viz-surface)", color: "var(--viz-ink)" }}
    >
      <span className="font-medium" style={{ color: "var(--viz-ink-2)" }}>
        Nodes
      </span>
      <span className="flex flex-wrap items-center gap-x-4 gap-y-1">
        {kinds.map((kind) => (
          <span key={kind} className="inline-flex items-center gap-1.5">
            <Glyph kind={kind} size={14} />
            {KIND_LABEL[kind]}
          </span>
        ))}
        <span>question entity: the largest node, in bold</span>
      </span>
      <span className="font-medium" style={{ color: "var(--viz-ink-2)" }}>
        Links
      </span>
      <span className="flex flex-wrap items-center gap-x-4 gap-y-1">
        {strengths.map((strength) => (
          <span key={strength} className="inline-flex items-center gap-1.5">
            <LineSample width={2} dash={EVIDENCE_STYLE[strength].dash} />
            {EVIDENCE_STYLE[strength].label}
          </span>
        ))}
        {maxFacts > 1 && (
          <span className="inline-flex items-center gap-1.5">
            <LineSample width={linkWidth(1)} />1 fact
            <LineSample width={linkWidth(maxFacts)} />
            {maxFacts} facts
          </span>
        )}
      </span>
    </div>
  );
}

export function FigureCaption({ graph, question }: { graph: Graph; question: string }) {
  const nAnswers = graph.nodes.filter((n) => n.role === "answer").length;
  return (
    <figcaption className="text-[12px] leading-relaxed" style={{ color: "var(--viz-ink-2)" }}>
      <span className="font-semibold" style={{ color: "var(--viz-ink)" }}>
        Reasoning graph.
      </span>{" "}
      The facts ARAX returned for its {nAnswers} selected answer{nAnswers === 1 ? "" : "s"}
      {question ? <> to “{question}”</> : null}, from the NCATS Translator Tier 0 knowledge graph. In the layered
      view the answers stand on the left in ARAX rank order and the question&apos;s entity on the right. Each link
      joins two entities on a reasoning path: its arrow and label give the most specific statement among its
      strongest facts (subject → object), its width the number of facts, and its line style the strongest evidence.
      Hover to isolate an answer&apos;s paths, click a node or link for its sources.
    </figcaption>
  );
}

function LineSample({ width, dash = null }: { width: number; dash?: number[] | null }) {
  return (
    <svg width="28" height="8" aria-hidden>
      <line
        x1="0"
        y1="4"
        x2="28"
        y2="4"
        stroke="var(--viz-ink-2)"
        strokeWidth={width}
        strokeDasharray={dash ? dash.join(" ") : undefined}
      />
    </svg>
  );
}
