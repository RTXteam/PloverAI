"use client";

// the evidence behind a fact of the ARAX reasoning graph, drawn three
// ways from the same data (lib/factText.ts):
//   FactCitation — the chip a citation such as [F9] becomes in the
//     explanation ("F9 · 7 trials, phase 4"). hovering or focusing it
//     opens a card with the reasoning path the fact sits on (its step
//     marked), the statement, the source and the evidence, with links;
//     clicking it jumps to the fact in the evidence list.
//   EvidenceList — every fact under the explanation, like the reference
//     list of a paper.
//   EvidenceDetails — the links themselves: approvals, FDA applications,
//     drug labels, registered trials, PubMed articles, co-mention.

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ReasoningFact, ReasoningGraph } from "@/lib/api";
import {
  citedFactIds,
  evidenceOf,
  factTag,
  fdaApplicationUrl,
  levelText,
  pathOfFact,
  pubmedUrl,
  sourceText,
  statement,
} from "@/lib/factText";
import { kindOf } from "@/lib/vizPalette";
import { Glyph } from "@/components/ReasoningGraph";

const OPEN_DELAY_MS = 120;
const CLOSE_DELAY_MS = 180;
const CARD_WIDTH = 400;

export function FactCitation({
  citation,
  graph,
  labels,
  onHighlight,
  onOpen,
}: {
  citation: string;
  graph: ReasoningGraph;
  labels: Map<string, string>;
  onHighlight: (factId: string | null) => void;
  onOpen: (factId: string) => void;
}) {
  const facts = useMemo(() => {
    const byId = new Map(graph.edges.map((f) => [f.fact_id, f]));
    return citedFactIds(citation).flatMap((id) => byId.get(id) ?? []);
  }, [citation, graph]);
  const [open, setOpen] = useState(false);
  const [alignRight, setAlignRight] = useState(false);
  const anchor = useRef<HTMLSpanElement>(null);
  const timer = useRef<number | null>(null);

  const schedule = (next: boolean) => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setOpen(next), next ? OPEN_DELAY_MS : CLOSE_DELAY_MS);
  };
  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);
  // the card opens to the right of the chip unless that would leave the window.
  useLayoutEffect(() => {
    if (!open || !anchor.current) return;
    const rect = anchor.current.getBoundingClientRect();
    setAlignRight(rect.left + CARD_WIDTH > window.innerWidth - 16);
  }, [open]);

  if (facts.length === 0) {
    return <span className="font-mono text-[0.8em]" style={{ color: "var(--viz-muted)" }}>{citation}</span>;
  }
  const single = facts.length === 1 ? facts[0] : null;
  const tag = single ? factTag(single) : `${facts.length} facts`;

  return (
    <span
      ref={anchor}
      className="not-prose relative inline-block align-baseline"
      onMouseEnter={() => {
        onHighlight(facts[0].fact_id);
        schedule(true);
      }}
      onMouseLeave={() => {
        onHighlight(null);
        schedule(false);
      }}
    >
      <button
        type="button"
        onFocus={() => {
          onHighlight(facts[0].fact_id);
          setOpen(true);
        }}
        onBlur={() => {
          onHighlight(null);
          setOpen(false);
        }}
        onClick={() => onOpen(facts[0].fact_id)}
        aria-expanded={open}
        className="mx-0.5 inline-flex items-baseline gap-1 whitespace-nowrap rounded border px-1.5 py-px text-[0.72em] leading-snug transition-colors hover:bg-[var(--viz-rule)] focus:bg-[var(--viz-rule)] focus:outline-none"
        style={{ borderColor: "var(--viz-rule)", color: "var(--viz-ink-2)", background: "var(--viz-surface)" }}
      >
        <span className="font-mono tabular-nums" style={{ color: "var(--viz-ink)" }}>
          {citation.includes("|")
            ? `${facts[0].fact_id} +${facts.length - 1}`
            : citation.replace(/\s*[-–—]\s*F?/, "–F")}
        </span>
        <span>· {tag}</span>
      </button>
      {open && (
        <span
          role="dialog"
          className={`absolute top-full z-40 mt-1 block rounded-md border p-3 text-left text-[12px] leading-snug shadow-lg ${alignRight ? "right-0" : "left-0"}`}
          style={{
            width: CARD_WIDTH,
            maxWidth: "calc(100vw - 32px)",
            background: "var(--viz-surface)",
            borderColor: "var(--viz-rule)",
            color: "var(--viz-ink)",
          }}
          onMouseEnter={() => schedule(true)}
          onMouseLeave={() => schedule(false)}
        >
          {single ? (
            <FactCard fact={single} graph={graph} labels={labels} />
          ) : (
            <FactGroup facts={facts} labels={labels} onOpen={onOpen} />
          )}
        </span>
      )}
    </span>
  );
}

function FactCard({ fact, graph, labels }: { fact: ReasoningFact; graph: ReasoningGraph; labels: Map<string, string> }) {
  const s = statement(fact, labels);
  return (
    <span className="flex flex-col gap-2">
      <span className="flex items-baseline justify-between gap-3">
        <span className="font-mono font-semibold">{fact.fact_id}</span>
        <span style={{ color: "var(--viz-muted)" }}>{levelText(fact)}</span>
      </span>
      <PathStrip fact={fact} graph={graph} labels={labels} />
      <span className="block text-[13px]">
        <span className="font-semibold">{s.subject}</span> <em>{s.predicate}</em>{" "}
        <span className="font-semibold">{s.object}</span>
      </span>
      <span className="block" style={{ color: "var(--viz-ink-2)" }}>
        Source: {sourceText(fact)}
      </span>
      <EvidenceDetails fact={fact} limit={3} />
      <span className="block text-[11px]" style={{ color: "var(--viz-muted)" }}>
        Click the citation to find it in the evidence list.
      </span>
    </span>
  );
}

function FactGroup({
  facts,
  labels,
  onOpen,
}: {
  facts: ReasoningFact[];
  labels: Map<string, string>;
  onOpen: (factId: string) => void;
}) {
  const shown = facts.slice(0, 8);
  return (
    <span className="flex flex-col gap-1.5">
      <span className="font-semibold">{facts.length} facts</span>
      {shown.map((fact) => {
        const s = statement(fact, labels);
        return (
          <button
            key={fact.fact_id}
            type="button"
            onClick={() => onOpen(fact.fact_id)}
            className="block rounded px-1 py-0.5 text-left hover:bg-[var(--viz-rule)]"
          >
            <span className="font-mono">{fact.fact_id}</span>{" "}
            <span style={{ color: "var(--viz-ink-2)" }}>
              {s.subject} <em>{s.predicate}</em> {s.object}
            </span>{" "}
            <span style={{ color: "var(--viz-muted)" }}>· {factTag(fact)}</span>
          </button>
        );
      })}
      {facts.length > shown.length && (
        <span style={{ color: "var(--viz-muted)" }}>and {facts.length - shown.length} more in the evidence list</span>
      )}
    </span>
  );
}

// the reasoning path the fact sits on, answer to question entity, with
// the fact's own step drawn in ink and the others muted.
function PathStrip({ fact, graph, labels }: { fact: ReasoningFact; graph: ReasoningGraph; labels: Map<string, string> }) {
  const located = pathOfFact(graph, fact.fact_id);
  const nodes = located?.nodes.length ? located.nodes : [fact.source, fact.target];
  const step = located ? located.step : 0;
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  return (
    <span
      className="flex items-center gap-1 overflow-hidden rounded border px-2 py-2"
      style={{ borderColor: "var(--viz-rule)" }}
      aria-label={`Reasoning path: ${nodes.map((id) => labels.get(id) ?? id).join(" to ")}`}
    >
      {nodes.map((id, i) => {
        const node = byId.get(id);
        const active = i === step || i === step + 1;
        return (
          <span key={`${id}-${i}`} className="flex min-w-0 items-center gap-1">
            {i > 0 && <StepArrow active={i === step + 1} />}
            <span className="flex min-w-0 items-center gap-1" style={{ opacity: active ? 1 : 0.55 }}>
              <Glyph kind={kindOf(node?.category ?? null)} />
              <span className="truncate text-[11px]" style={{ maxWidth: 92, fontWeight: active ? 600 : 400 }}>
                {labels.get(id) ?? id}
              </span>
            </span>
          </span>
        );
      })}
    </span>
  );
}

function StepArrow({ active }: { active: boolean }) {
  return (
    <svg width="22" height="8" viewBox="0 0 22 8" aria-hidden className="shrink-0">
      <line
        x1="0"
        y1="4"
        x2="17"
        y2="4"
        stroke={active ? "var(--viz-ink)" : "var(--viz-edge)"}
        strokeWidth={active ? 1.75 : 1.25}
        strokeDasharray={active ? undefined : "2 2"}
      />
      <polygon points="17,1 22,4 17,7" fill={active ? "var(--viz-ink)" : "var(--viz-edge)"} />
    </svg>
  );
}

const linkClass = "underline decoration-dotted underline-offset-2 hover:decoration-solid";

export function EvidenceDetails({ fact, limit }: { fact: ReasoningFact; limit: number }) {
  const e = evidenceOf(fact);
  const rows: React.ReactNode[] = [];
  if (e.approval || e.fda_approvals.length > 0) {
    rows.push(
      <span key="approval" className="block">
        {e.approval ? <span className="font-medium">{capitalise(e.approval)}</span> : "FDA application"}
        {e.fda_approvals.length > 0 && (
          <>
            {e.approval ? " · FDA application " : " "}
            {e.fda_approvals.slice(0, limit).map((application, i) => (
              <span key={application}>
                {i > 0 && ", "}
                <a href={fdaApplicationUrl(application)} target="_blank" rel="noopener noreferrer" className={linkClass}>
                  {application}
                </a>
              </span>
            ))}
          </>
        )}
      </span>,
    );
  }
  if (e.labels.length > 0) {
    rows.push(
      <span key="labels" className="block">
        FDA drug label{e.labels.length === 1 ? "" : "s"} on DailyMed:{" "}
        {e.labels.slice(0, limit).map((label, i) => (
          <span key={label.id}>
            {i > 0 && ", "}
            <a href={label.url} target="_blank" rel="noopener noreferrer" className={linkClass}>
              label {i + 1}
            </a>
          </span>
        ))}
        {e.labels.length > limit && ` and ${e.labels.length - limit} more`}
      </span>,
    );
  }
  if (e.n_trials > 0) {
    rows.push(
      <span key="trials" className="block">
        <span className="font-medium">
          {e.n_trials} registered trial{e.n_trials === 1 ? "" : "s"}
        </span>
        {e.max_phase && `, most advanced phase ${e.max_phase}`}
        {e.trials.slice(0, limit).map((trial) => (
          <span key={trial.id} className="mt-0.5 block pl-3" style={{ color: "var(--viz-ink-2)" }}>
            <a href={trial.url} target="_blank" rel="noopener noreferrer" className={`font-mono ${linkClass}`}>
              {trial.id}
            </a>
            {[
              trial.phase ? `phase ${trial.phase}` : null,
              trial.status,
              trial.enrollment ? `${trial.enrollment.toLocaleString()} participants` : null,
            ]
              .filter(Boolean)
              .map((bit) => ` · ${bit}`)
              .join("")}
            {trial.title && (
              <span className="block truncate" style={{ color: "var(--viz-muted)" }} title={trial.title}>
                {trial.title}
              </span>
            )}
          </span>
        ))}
        {e.n_trials > Math.min(limit, e.trials.length) && (
          <span className="block pl-3" style={{ color: "var(--viz-muted)" }}>
            and {e.n_trials - Math.min(limit, e.trials.length)} more
          </span>
        )}
      </span>,
    );
  } else if (e.max_phase) {
    rows.push(
      <span key="phase" className="block">
        Most advanced research phase {e.max_phase}
      </span>,
    );
  }
  if (e.ngd !== null) {
    rows.push(
      <span key="ngd" className="block">
        Mentioned together in PubMed abstracts: co-mention distance{" "}
        <span className="font-medium tabular-nums">{e.ngd.toFixed(2)}</span>{" "}
        <span style={{ color: "var(--viz-muted)" }}>(lower means more often; above 1 is weak)</span>
      </span>,
    );
  }
  if (e.n_pmids > 0) {
    const shown = e.pmids.slice(0, limit * 2);
    rows.push(
      <span key="pmids" className="block">
        {e.n_pmids} PubMed article{e.n_pmids === 1 ? "" : "s"}:{" "}
        {shown.map((pmid, i) => (
          <span key={pmid}>
            {i > 0 && ", "}
            <a href={pubmedUrl(pmid)} target="_blank" rel="noopener noreferrer" className={`font-mono ${linkClass}`}>
              {pmid.replace(/^PMID:/i, "")}
            </a>
          </span>
        ))}
        {e.n_pmids > shown.length && ` and ${e.n_pmids - shown.length} more`}
      </span>,
    );
  }
  if (e.record_url) {
    rows.push(
      <span key="record" className="block">
        <a href={e.record_url} target="_blank" rel="noopener noreferrer" className={linkClass}>
          Source record
        </a>
      </span>,
    );
  }
  if (rows.length === 0) {
    rows.push(
      <span key="none" className="block" style={{ color: "var(--viz-muted)" }}>
        The source gives no further evidence for this fact.
      </span>,
    );
  }
  return <span className="flex flex-col gap-1">{rows}</span>;
}

export function EvidenceList({
  graph,
  labels,
  showAll,
  onShowAll,
  flashId,
  onHighlight,
  linked,
  bare = false,
}: {
  graph: ReasoningGraph;
  labels: Map<string, string>;
  showAll: boolean;
  onShowAll: () => void;
  flashId: string | null;
  onHighlight: (factId: string | null) => void;
  // the facts behind what the graph's pointer is over: marked, and the
  // first of them scrolled into view.
  linked?: ReadonlySet<string>;
  // no card and no heading, for a page that already frames the list.
  bare?: boolean;
}) {
  const facts = graph.edges;
  const shown = showAll ? facts : facts.slice(0, INITIAL_FACTS);
  const firstLinked = linked && linked.size > 0 ? (shown.find((f) => linked.has(f.fact_id))?.fact_id ?? null) : null;
  useEffect(() => {
    if (firstLinked) document.getElementById(`fact-${firstLinked}`)?.scrollIntoView({ block: "nearest" });
  }, [firstLinked]);
  return (
    <section
      id="evidence"
      className={bare ? "px-3 py-2 text-[12.5px]" : "rounded-lg border p-5 text-[13px]"}
      style={{ borderColor: "var(--viz-rule)", background: "var(--viz-surface)", color: "var(--viz-ink)" }}
    >
      {!bare && <h3 className="text-[15px] font-semibold">Evidence</h3>}
      <p className={bare ? "text-[11px]" : "mt-1"} style={{ color: "var(--viz-ink-2)" }}>
        The {facts.length} facts ARAX returned for these answers, numbered as the explanation cites them. Hover a
        fact to light its link in the graph; hover the graph to find its facts here.
      </p>
      <ol className="mt-3 flex flex-col">
        {shown.map((fact) => {
          const s = statement(fact, labels);
          const lit = linked?.has(fact.fact_id) ?? false;
          // hovered here or lit from the graph: a tinted row with a bar in
          // the selection hue, the same mark the graph's link gets.
          return (
            <li
              key={fact.fact_id}
              id={`fact-${fact.fact_id}`}
              data-lit={lit || undefined}
              className="group relative -mx-3 grid scroll-mt-24 grid-cols-[2.75rem_1fr] gap-2 border-t px-3 py-2.5 transition-colors duration-150 hover:bg-[var(--viz-tint)] data-[lit]:bg-[var(--viz-tint)]"
              style={{
                borderColor: "var(--viz-rule)",
                background: flashId === fact.fact_id ? "var(--viz-rule)" : undefined,
              }}
              onMouseEnter={() => onHighlight(fact.fact_id)}
              onMouseLeave={() => onHighlight(null)}
            >
              <span
                aria-hidden
                className="absolute inset-y-1.5 left-0 w-[2px] rounded-full opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-data-[lit]:opacity-100"
                style={{ background: "var(--viz-focus)" }}
              />
              <span className="font-mono tabular-nums text-[var(--viz-muted)] group-hover:text-[var(--viz-focus)] group-data-[lit]:text-[var(--viz-focus)]">
                {fact.fact_id}
              </span>
              <span className="flex min-w-0 flex-col gap-1">
                <span>
                  <span className="font-medium">{s.subject}</span> <em>{s.predicate}</em>{" "}
                  <span className="font-medium">{s.object}</span>
                </span>
                <span style={{ color: "var(--viz-ink-2)" }}>
                  {sourceText(fact)} · {levelText(fact)}
                </span>
                <span className="text-[12px]" style={{ color: "var(--viz-ink-2)" }}>
                  <EvidenceDetails fact={fact} limit={2} />
                </span>
              </span>
            </li>
          );
        })}
      </ol>
      {!showAll && facts.length > INITIAL_FACTS && (
        <button
          type="button"
          onClick={onShowAll}
          className="mt-2 rounded border px-2 py-1 text-[12px] hover:bg-[var(--viz-rule)]"
          style={{ borderColor: "var(--viz-rule)", color: "var(--viz-ink-2)" }}
        >
          Show all {facts.length} facts
        </button>
      )}
    </section>
  );
}

export const INITIAL_FACTS = 10;

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
