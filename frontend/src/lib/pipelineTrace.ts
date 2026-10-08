// the pipeline as eleven readable steps, each with what it produced, for
// the trace strip under the question. a finished run is read from what
// it saved (QueryResponse); a running one from its log lines as they
// stream (every stage logs "→" when it starts and "✓" or "✗" when it
// ends, with latency, tokens and counts) plus the structured stage
// events. the eleven steps group the pipeline's 15 stages; `anchor` is
// the stage number the inspector's pipeline list opens at.

import type { QueryResponse } from "@/lib/api";
import type { LiveState } from "@/lib/liveState";
import { pickedAnswers } from "@/lib/liveState";
import { queryGraphOf, readQuery, type QueryReading } from "@/lib/queryGraph";

export type StepKind = "LLM" | "service" | "code";
export type StepState = "done" | "active" | "pending" | "stopped";

export type TraceStep = {
  key: StepKey;
  label: string;
  kind: StepKind;
  anchor: string;
  state: StepState;
  detail: string | null;
  // lines for the hover card.
  more: string[];
  seconds: number | null;
};

type StepKey = "scope" | "entity" | "lookup" | "pick" | "normalize" | "query" | "validate" | "arax" | "answers" | "evidence" | "explain";

const STEPS: { key: StepKey; label: string; kind: StepKind; anchor: string }[] = [
  { key: "scope", label: "Scope", kind: "LLM", anchor: "1" },
  { key: "entity", label: "Entity", kind: "LLM", anchor: "2" },
  { key: "lookup", label: "Lookup", kind: "service", anchor: "3" },
  { key: "pick", label: "Pick", kind: "LLM", anchor: "4" },
  { key: "normalize", label: "Normalize", kind: "service", anchor: "6" },
  { key: "query", label: "Query", kind: "LLM", anchor: "8" },
  { key: "validate", label: "Validate", kind: "code", anchor: "9" },
  { key: "arax", label: "ARAX", kind: "service", anchor: "10" },
  { key: "answers", label: "Answers", kind: "LLM", anchor: "11" },
  { key: "evidence", label: "Evidence", kind: "code", anchor: "13" },
  { key: "explain", label: "Explain", kind: "LLM", anchor: "15" },
];

// the LLM stage names the cost ledger and the log use.
const LLM_STEP: Record<string, StepKey> = {
  scope_check: "scope",
  entity_extract: "entity",
  candidate_pick: "pick",
  trapi_build: "query",
  answer_pick: "answers",
  explain: "explain",
};

// where a run that did not finish stopped, by its status or outcome.
const STOPPED_AT: Record<string, StepKey> = {
  out_of_scope: "scope",
  entity_empty: "entity",
  nameres_failed: "lookup",
  no_candidate_match: "pick",
  nodenorm_failed: "normalize",
  low_confidence_resolution: "normalize",
  query_declined: "query",
  invalid_query: "validate",
  arax_error: "arax",
  no_results: "arax",
  no_answer_picked: "answers",
};

const bare = (term: string) => term.replace(/^biolink:/, "");

function queryDetail(reading: QueryReading | null): string | null {
  if (!reading) return null;
  return [reading.predicate ?? "related to", reading.inferred ? "inferred" : null, ...reading.qualifiers].filter(Boolean).join(" · ");
}

type StepPart = { detail: string | null; more: string[]; seconds: number | null; done: boolean };

function empty(): Record<StepKey, StepPart> {
  const parts = {} as Record<StepKey, StepPart>;
  for (const step of STEPS) parts[step.key] = { detail: null, more: [], seconds: null, done: false };
  return parts;
}

function assemble(parts: Record<StepKey, StepPart>, stoppedAt: StepKey | null, stopNote: string | null, activeKey: StepKey | null): TraceStep[] {
  let stopped = false;
  return STEPS.map((step) => {
    const part = parts[step.key];
    let state: StepState = part.done ? "done" : "pending";
    if (stopped) state = "pending";
    if (step.key === stoppedAt) {
      state = "stopped";
      stopped = true;
    } else if (!stopped && step.key === activeKey) state = "active";
    return {
      ...step,
      state,
      detail: step.key === stoppedAt && stopNote ? (part.detail ? `${part.detail} · ${stopNote}` : stopNote) : part.detail,
      more: part.more,
      seconds: part.seconds,
    };
  });
}

// ------------------------------------------------------------- finished

type CostStage = { stage: string; input_tokens: number; output_tokens: number; total_usd: number; latency_s: number };

export function traceOfResult(r: QueryResponse): TraceStep[] {
  const parts = empty();
  const cost = ((r.intermediates.cost as { stages?: CostStage[] } | null)?.stages ?? []).filter((c) => LLM_STEP[c.stage]);
  for (const c of cost) {
    const part = parts[LLM_STEP[c.stage]];
    part.done = true;
    part.seconds = c.latency_s;
    part.more.push(`${c.input_tokens} tokens in · ${c.output_tokens} out · $${c.total_usd.toFixed(5)} · ${c.latency_s.toFixed(1)} s`);
  }

  const nameres = (r.intermediates.nameres ?? null) as {
    mention?: string;
    expected_category?: string;
    granularity_preference?: string;
    biolink_type_filter_applied?: string[];
    candidates?: unknown[];
    chosen_curie?: string;
    reranked_by_ic?: boolean;
    latency_s?: number;
  } | null;
  const probes = (r.intermediates.candidate_probes ?? null) as {
    answer_cat?: string;
    by_curie?: Record<string, { total_edges?: number }>;
  } | null;
  const pinned = (r.intermediates.nodenorm as { pinned?: { canonical_curie?: string; label?: string; categories?: string[] } } | null)?.pinned;

  if (r.outcome === "out_of_scope") parts.scope.detail = "out of scope";
  else if (parts.scope.done) parts.scope.detail = "in scope";

  if (nameres?.mention) {
    parts.entity.detail = `“${nameres.mention}”${nameres.expected_category ? ` · ${bare(nameres.expected_category)}` : ""}`;
    parts.entity.more.unshift(
      ...[
        probes?.answer_cat ? `answer type: ${bare(probes.answer_cat)}` : null,
        nameres.granularity_preference ? `granularity: ${nameres.granularity_preference}` : null,
      ].filter((x): x is string => Boolean(x)),
    );
  }
  if (Array.isArray(nameres?.candidates)) {
    parts.lookup.done = true;
    parts.lookup.detail = `${nameres.candidates.length} candidates`;
    parts.lookup.seconds = nameres.latency_s ?? null;
    parts.lookup.more.push(
      ...[
        nameres.biolink_type_filter_applied?.length ? `type filter: ${nameres.biolink_type_filter_applied.map(bare).join(", ")}` : null,
        nameres.reranked_by_ic ? "re-ranked by information content (general concept)" : null,
      ].filter((x): x is string => Boolean(x)),
    );
  }
  if (nameres?.chosen_curie) {
    parts.pick.detail = nameres.chosen_curie;
    const facts = probes?.by_curie?.[nameres.chosen_curie]?.total_edges;
    if (typeof facts === "number") parts.pick.more.unshift(`${facts.toLocaleString()} facts in Tier 0 to the answer type`);
  }
  if (pinned?.canonical_curie) {
    parts.normalize.done = true;
    parts.normalize.detail = pinned.label ?? pinned.canonical_curie;
    parts.normalize.more.push(`canonical: ${pinned.canonical_curie}`, ...(pinned.categories?.[0] ? [`type: ${bare(pinned.categories[0])}`] : []));
  }

  const written = readQuery(queryGraphOf(r.intermediates.trapi_query));
  const sent = readQuery(queryGraphOf(r.intermediates.reasoner_request));
  if (written) {
    parts.query.detail = queryDetail(written);
    if (sent && queryDetail(sent) !== queryDetail(written)) parts.query.more.unshift(`sent to ARAX as: ${queryDetail(sent)}`);
  }

  const validation = r.intermediates.validation as { passed?: boolean } | null | undefined;
  if (validation && typeof validation.passed === "boolean") {
    parts.validate.done = true;
    parts.validate.detail = validation.passed ? "valid" : "invalid";
    parts.validate.more.push("reasoner-validator: TRAPI schema and Biolink terms");
  }

  const nResults = r.intermediates.reasoner_response_summary?.n_results;
  if (typeof nResults === "number") {
    parts.arax.done = true;
    parts.arax.detail = `${nResults} candidates`;
    const summary = r.intermediates.reasoner_response_summary;
    if (summary) parts.arax.more.push(`${summary.n_nodes} entities · ${summary.n_edges} facts in its answer`);
  }

  const picked = pickedAnswers(r);
  if (parts.answers.done) parts.answers.detail = `${picked.length} picked`;

  const graph = r.reasoning_graph;
  if (graph && graph.edges.length > 0) {
    parts.evidence.done = true;
    parts.evidence.detail = `${graph.edges.length} facts`;
    const trials = graph.edges.reduce((sum, e) => sum + (e.evidence?.n_trials ?? 0), 0);
    const pmids = graph.edges.reduce((sum, e) => sum + (e.evidence?.n_pmids ?? 0), 0);
    const approved = graph.edges.filter((e) => e.evidence?.approval).length;
    parts.evidence.more.push(`${trials} registered trials · ${approved} approval records · ${pmids} PubMed articles`);
  }
  if (parts.explain.done) parts.explain.detail = r.explanation ? "written" : null;

  const stoppedAt = r.success && r.outcome === "answered" ? null : (STOPPED_AT[r.outcome ?? ""] ?? STEPS.find((s) => !parts[s.key].done)?.key ?? null);
  return assemble(parts, stoppedAt, stoppedAt ? (r.outcome ?? "stopped").replaceAll("_", " ") : null, null);
}

// -------------------------------------------------------------- running

type LogLine = { level: string; msg: string; t: number };

export function traceOfLive(live: LiveState, logs: LogLine[], elapsedSeconds: number): TraceStep[] {
  const parts = empty();
  const startedAt: ByStep<number> = {};
  let stoppedAt: StepKey | null = null;
  const start = (key: StepKey, t: number) => {
    if (startedAt[key] === undefined) startedAt[key] = t;
  };
  const finish = (key: StepKey, t: number, seconds?: number) => {
    start(key, t);
    parts[key].done = true;
    parts[key].seconds = seconds ?? Math.max(0, t - (startedAt[key] ?? t));
  };

  for (const line of logs) {
    const msg = line.msg.replace(/\[\/?[^\]]*\]/g, "");
    let m: RegExpMatchArray | null;
    if ((m = msg.match(/→ openrouter\s+stage=(\w+)/)) && LLM_STEP[m[1]]) {
      const key = LLM_STEP[m[1]];
      start(key, line.t);
      if (key === "explain" && !parts.evidence.done) finish("evidence", line.t);
    } else if ((m = msg.match(/✓ openrouter\s+stage=(\w+)\s+in=(\d+)tok\s+out=(\d+)tok\s+cost=\$([\d.]+)\s+latency=([\d.]+)s/)) && LLM_STEP[m[1]]) {
      const key = LLM_STEP[m[1]];
      finish(key, line.t, Number(m[5]));
      parts[key].more.push(`${m[2]} tokens in · ${m[3]} out · $${Number(m[4]).toFixed(5)} · ${Number(m[5]).toFixed(1)} s`);
      // the code steps run right after: validation after the query,
      // evidence after the answers.
      if (key === "query") startedAt.validate = line.t;
      if (key === "answers") startedAt.evidence = line.t;
    } else if ((m = msg.match(/✗ openrouter\s+stage=(\w+)/)) && LLM_STEP[m[1]]) {
      stoppedAt = LLM_STEP[m[1]];
    } else if ((m = msg.match(/→ nameres\b.*?mention='([^']*)'/))) {
      start("lookup", line.t);
      parts.entity.detail = `“${m[1]}”`;
    } else if ((m = msg.match(/✓ nameres\s+candidates=(\d+)/))) {
      finish("lookup", line.t);
      parts.lookup.detail = `${m[1]} candidates`;
    } else if (/✗ nameres/.test(msg)) {
      stoppedAt = "lookup";
    } else if (/→ retriever\s+PROBE/.test(msg)) {
      start("pick", line.t);
    } else if (/→ nodenorm/.test(msg) && parts.pick.done && startedAt.query === undefined) {
      start("normalize", line.t);
    } else if (/✓ nodenorm/.test(msg) && startedAt.normalize !== undefined && startedAt.query === undefined) {
      finish("normalize", line.t);
    } else if ((m = msg.match(/✓ validator\s+(\w+)/))) {
      finish("validate", line.t);
      parts.validate.detail = m[1] === "passed" ? "valid" : "invalid";
    } else if (/✗ validator/.test(msg)) {
      stoppedAt = "validate";
    } else if (/→ arax\b/.test(msg)) {
      start("arax", line.t);
    } else if ((m = msg.match(/✓ arax\b.*?results=(\d+).*?latency=([\d.]+)s/))) {
      finish("arax", line.t, Number(m[2]));
      parts.arax.detail = `${m[1]} candidates`;
    } else if (/✗ arax\b/.test(msg)) {
      stoppedAt = "arax";
    }
  }

  // details the structured events carry.
  if (live.entity) {
    parts.normalize.detail = live.entity.label ?? live.entity.curie;
    parts.pick.detail = live.entity.curie;
    if (!parts.normalize.done && startedAt.query !== undefined) parts.normalize.done = true;
  }
  if (parts.scope.done) parts.scope.detail = "in scope";
  parts.query.detail = queryDetail(readQuery(live.queryGraph)) ?? parts.query.detail;
  if (live.answers) parts.answers.detail = `${live.answers.length} picked`;
  if (live.graph) {
    parts.evidence.detail = `${live.graph.edges.length} facts`;
    if (!parts.evidence.done && startedAt.explain !== undefined) parts.evidence.done = true;
  }

  // the step running now: the first one not done, timed from its start
  // (or from the end of the step before it).
  const firstOpen = STEPS.find((s) => !parts[s.key].done)?.key ?? null;
  if (firstOpen && stoppedAt === null) {
    const index = STEPS.findIndex((s) => s.key === firstOpen);
    const previous = index > 0 ? STEPS[index - 1].key : null;
    const since = startedAt[firstOpen] ?? (previous ? (startedAt[previous] ?? 0) + (parts[previous].seconds ?? 0) : 0);
    parts[firstOpen].seconds = Math.max(0, elapsedSeconds - since);
  }
  return assemble(parts, stoppedAt, stoppedAt ? "failed" : null, stoppedAt ? null : firstOpen);
}

type ByStep<T> = { [K in StepKey]?: T };
