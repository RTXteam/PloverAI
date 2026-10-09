// what the page knows about a run, step by step. while a query streams,
// the structured "stage" events of POST /api/v1/query/stream
// (pipeline._ui_event) fill a LiveState:
//   entity  → the question's entity is known;
//   query   → the query went to ARAX; a dashed "ghost" answer stands for
//             what ARAX is looking for ("which chemical / drug?");
//   arax_progress → what ARAX says it is doing, in plain words;
//   results → how many candidates ARAX ranked;
//   answers → the picked answers;
//   graph   → the reasoning paths.
// a finished run (QueryResponse) is read into the same six steps, so the
// progress strip looks the same during and after a run.

import type { QueryResponse, ReasoningGraph, ReasoningNode, StageEvent } from "@/lib/api";
import type { Ghost } from "@/components/ReasoningGraph";
import { humanPredicate } from "@/lib/reasoningLinks";
import { queryGraphOf, readQuery, type QueryGraph } from "@/lib/queryGraph";
import { KIND_LABEL, kindOf } from "@/lib/vizPalette";

export type LiveState = {
  entity: { curie: string; label: string | null; category: string | null; answerCategory: string | null } | null;
  querySent: boolean;
  queryGraph: QueryGraph | null;
  nResults: number | null;
  answers: { curie: string; label: string | null }[] | null;
  graph: ReasoningGraph | null;
  // ARAX's latest progress line.
  araxNote: string | null;
  // when each stage event arrived (client clock), to time the steps.
  arrivedAt: Partial<Record<StageEvent["stage"], number>>;
};

export const EMPTY_LIVE: LiveState = {
  entity: null,
  querySent: false,
  queryGraph: null,
  nResults: null,
  answers: null,
  graph: null,
  araxNote: null,
  arrivedAt: {},
};

export function reduceLive(previous: LiveState, event: StageEvent): LiveState {
  const state = { ...previous, arrivedAt: { ...previous.arrivedAt, [event.stage]: Date.now() } };
  switch (event.stage) {
    case "entity":
      return {
        ...state,
        entity: {
          curie: event.curie,
          label: event.label,
          category: event.category,
          answerCategory: event.answer_category,
        },
      };
    case "query":
      return { ...state, querySent: true, queryGraph: queryGraphOf(event.query_graph) };
    case "arax_progress":
      return { ...state, araxNote: event.message };
    case "results":
      return { ...state, nResults: event.n_results };
    case "answers":
      return { ...state, answers: event.answers };
    case "graph":
      return event.kind === "reasoning" ? { ...state, graph: event.graph } : state;
  }
}

export type Step = { label: string; done: string | null };

// the stage event that starts each step (the one that ends the step
// before it); the first step starts with the query itself.
export const STEP_STARTED_BY: (StageEvent["stage"] | null)[] = [null, "entity", "query", "results", "answers", "graph"];

const STEP_LABELS = [
  "Read the question",
  "Build the query",
  "ARAX reasons over Tier 0",
  "Choose the answers",
  "Trace the evidence",
  "Write the explanation",
];

function steps(done: (string | null)[]): Step[] {
  return STEP_LABELS.map((label, i) => ({ label, done: done[i] ?? null }));
}

function queryDone(graph: QueryGraph | null, sentAsReasoning: boolean): string {
  const reading = readQuery(graph);
  return [reading?.predicate, sentAsReasoning || reading?.inferred ? "reasoning" : null].filter(Boolean).join(", ") || "sent";
}

export function stepsOf(live: LiveState): Step[] {
  const n = live.graph?.edges.length ?? 0;
  return steps([
    live.entity ? `“${live.entity.label ?? live.entity.curie}”` : null,
    live.querySent ? queryDone(live.queryGraph, false) : null,
    live.nResults !== null ? `${live.nResults} candidates` : null,
    live.answers ? `${live.answers.length} answers` : null,
    live.graph ? `${n} fact${n === 1 ? "" : "s"}` : null,
  ]);
}

// the label of the question's pinned entity, wherever the run recorded it.
export function pinnedLabel(r: QueryResponse): string | null {
  const graph = r.reasoning_graph;
  const fromGraph = graph?.nodes.find((n) => n.role === "query")?.label;
  if (fromGraph) return fromGraph;
  const pinned = (r.intermediates.nodenorm as { pinned?: { label?: unknown } } | null | undefined)?.pinned;
  return typeof pinned?.label === "string" ? pinned.label : null;
}

export function pickedAnswers(r: QueryResponse): { curie: string; label: string | null }[] {
  const list = (r.answer as { answers?: unknown } | null)?.answers;
  return Array.isArray(list)
    ? list.filter((a): a is { curie: string; label: string | null } => typeof a?.curie === "string")
    : [];
}

export function stepsOfResult(r: QueryResponse): Step[] {
  const sent = readQuery(queryGraphOf(r.intermediates.reasoner_request));
  const nResults = r.intermediates.reasoner_response_summary?.n_results;
  const picked = pickedAnswers(r);
  const n = r.reasoning_graph?.edges.length ?? 0;
  const label = pinnedLabel(r);
  return steps([
    label ? `“${label}”` : null,
    r.intermediates.trapi_query ? queryDone(queryGraphOf(r.intermediates.trapi_query), sent?.inferred ?? false) : null,
    typeof nResults === "number" ? `${nResults} candidates` : null,
    picked.length > 0 ? `${picked.length} answers` : null,
    r.reasoning_graph ? `${n} fact${n === 1 ? "" : "s"}` : null,
    r.explanation && r.success ? "done" : null,
  ]);
}

// the stand-in answer: the query edge's answer side, labelled by the
// category ARAX is asked for.
export function ghostOf(live: LiveState): Ghost | null {
  if (!live.entity || !live.querySent || live.answers) return null;
  const reading = readQuery(live.queryGraph);
  const kind = kindOf(live.entity.answerCategory);
  return {
    label: kind === "other" ? "which answers?" : `which ${KIND_LABEL[kind]}?`,
    predicate: reading?.predicate ? humanPredicate(reading.predicate) : null,
    answerIsSubject: reading ? !reading.subject.pinned : true,
  };
}

// before the reasoning graph arrives, what is known so far: the
// question's entity, then the picked answers waiting on their links.
export function liveGraphOf(live: LiveState): ReasoningGraph | null {
  if (live.graph) return live.graph;
  if (!live.entity) return null;
  const nodes: ReasoningNode[] = [
    { id: live.entity.curie, label: live.entity.label ?? live.entity.curie, category: live.entity.category, role: "query" },
  ];
  for (const answer of live.answers ?? []) {
    if (answer.curie === live.entity.curie) continue;
    nodes.push({ id: answer.curie, label: answer.label ?? answer.curie, category: live.entity.answerCategory, role: "answer" });
  }
  return { query: live.entity.curie, nodes, edges: [], paths: {}, path_facts: {} };
}
