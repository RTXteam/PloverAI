"use client";

// the Ask view of the workbench, top to bottom:
//   question bar — the question, example questions, the model, Run;
//   query band — the one-hop query the pipeline built, drawn as two
//     nodes and an edge (which end is pinned to which entity, the
//     predicate, whether ARAX is asked to reason), with the run's result,
//     time, cost and exports on the right;
//   trace — the pipeline as eleven steps, each with what it produced and
//     how long it took; hover for details, click to open the step's
//     prompt and artifacts in the inspector;
//   workspace — three panes: ARAX's ranked answers, the reasoning graph
//     with its legend and caption, and the inspector.

import { useEffect, useMemo, useState } from "react";
import type { ExampleQuestion, ModelInfo, QueryResponse, ReasoningGraph as Graph } from "@/lib/api";
import { ModelDropdown } from "@/components/ModelDropdown";
import { QuestionsDropdown } from "@/components/QuestionsDropdown";
import { Glyph, ReasoningGraph, type Selection } from "@/components/ReasoningGraph";
import { FigureCaption, FigureLegend } from "@/components/FigureLegend";
import { AnswersTable, type RankedAnswer } from "@/components/AnswersTable";
import { Inspector, type InspectorTab, type LogLine } from "@/components/Inspector";
import { MarkdownAnswer } from "@/components/MarkdownAnswer";
import { TraceStrip } from "@/components/TraceStrip";
import { downloadResultJSON, downloadResultMarkdown, downloadResultPDF } from "@/lib/export";
import { TONE_CLASS, outcomeText, seconds, toneOf, usd } from "@/lib/format";
import { ghostOf, liveGraphOf, pickedAnswers, pinnedLabel, type LiveState } from "@/lib/liveState";
import { traceOfLive, traceOfResult } from "@/lib/pipelineTrace";
import { queryGraphOf, queryLine, readQuery, type QueryEnd, type QueryReading } from "@/lib/queryGraph";
import { kindOf } from "@/lib/vizPalette";

type Props = {
  question: string;
  onQuestion: (q: string) => void;
  models: ModelInfo[];
  modelId: string;
  onModel: (id: string) => void;
  examples: ExampleQuestion[];
  loading: boolean;
  startedAt: number | null;
  onRun: () => void;
  result: QueryResponse | null;
  live: LiveState;
  logs: LogLine[];
  error: string | null;
};

export function AskView(props: Props) {
  const { result, loading, error } = props;
  // the workspace keeps its own pane state (tab, chosen answer); a new
  // run, or the switch from live to finished, starts it over.
  const workspaceKey = loading ? "live" : (result?.run_id ?? "empty");
  return (
    <div className="flex h-full min-h-0 flex-col">
      <QuestionBar {...props} />
      {error && (
        <div className="border-b border-red-200 bg-red-50 px-4 py-2 text-[12.5px] text-red-800 dark:border-red-900 dark:bg-red-950/50 dark:text-red-200">
          {error}
        </div>
      )}
      {loading || result ? (
        <Workspace key={workspaceKey} {...props} />
      ) : (
        <Welcome examples={props.examples} onPick={props.onQuestion} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------- question

function QuestionBar({ question, onQuestion, models, modelId, onModel, examples, loading, onRun }: Props) {
  const canRun = !loading && question.trim().length > 0 && Boolean(modelId);
  return (
    <form
      className="flex shrink-0 items-center gap-2 border-b border-zinc-200 px-4 py-2 dark:border-zinc-800"
      onSubmit={(e) => {
        e.preventDefault();
        if (canRun) onRun();
      }}
    >
      <input
        value={question}
        onChange={(e) => onQuestion(e.target.value)}
        disabled={loading}
        placeholder="Ask a biomedical question, for example: What drugs treat rheumatoid arthritis?"
        aria-label="Question"
        className="h-9 min-w-0 flex-1 rounded border border-zinc-300 bg-white px-3 text-[14px] placeholder:text-zinc-400 focus:border-sky-500 focus:outline-none disabled:opacity-60 dark:border-zinc-700 dark:bg-zinc-900 dark:placeholder:text-zinc-500"
      />
      <QuestionsDropdown questions={examples} disabled={loading} onSelect={(q) => onQuestion(q.nl_question)} />
      <ModelDropdown models={models} value={modelId} onChange={onModel} disabled={loading} compact />
      <button
        type="submit"
        disabled={!canRun}
        className="inline-flex h-9 items-center rounded bg-sky-700 px-4 text-[13px] font-medium text-white hover:bg-sky-800 disabled:bg-zinc-300 disabled:text-zinc-500 dark:bg-sky-600 dark:hover:bg-sky-500 dark:disabled:bg-zinc-800"
      >
        {loading ? "Running…" : "Run"}
      </button>
    </form>
  );
}

function Welcome({ examples, onPick }: { examples: ExampleQuestion[]; onPick: (q: string) => void }) {
  return (
    <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-6 py-6">
      <div className="max-w-3xl text-[13px] leading-relaxed text-zinc-600 dark:text-zinc-400">
        <p>
          PloverAI turns a biomedical question into a TRAPI query, sends it to ARAX, the reasoner of the NCATS Biomedical Data
          Translator, and explains ARAX&apos;s answer with every claim cited to a fact in the Translator&apos;s Tier 0 knowledge
          graph. Every stage, prompt and artifact of a run is shown and kept.
        </p>
        <h2 className="mt-5 text-[11px] font-medium uppercase tracking-wide text-zinc-500">Examples</h2>
        <ul className="mt-2 flex flex-col">
          {examples.slice(0, 10).map((q) => (
            <li key={q.id}>
              <button
                type="button"
                onClick={() => onPick(q.nl_question)}
                className="w-full border-b border-zinc-100 py-1.5 text-left text-zinc-800 hover:text-sky-700 dark:border-zinc-900 dark:text-zinc-200 dark:hover:text-sky-400"
              >
                {q.nl_question}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

// --------------------------------------------------------------- workspace

function rankedRows(r: QueryResponse | null): { rows: RankedAnswer[]; total: number | null } {
  const data = r?.intermediates.reduced_data as
    | { reasoner?: string; total_answers?: number; rows?: unknown[] }
    | null
    | undefined;
  if (!data || data.reasoner !== "arax" || !Array.isArray(data.rows)) return { rows: [], total: null };
  const rows = data.rows.flatMap((row): RankedAnswer[] => {
    const x = row as { rank?: unknown; curie?: unknown; name?: unknown; score?: unknown; evidence_edges?: unknown };
    if (typeof x.curie !== "string") return [];
    return [
      {
        rank: typeof x.rank === "number" ? x.rank : null,
        curie: x.curie,
        name: typeof x.name === "string" ? x.name : x.curie,
        score: typeof x.score === "number" ? x.score : null,
        facts: typeof x.evidence_edges === "number" ? x.evidence_edges : null,
      },
    ];
  });
  return { rows, total: typeof data.total_answers === "number" ? data.total_answers : rows.length };
}

function useClock(running: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  return now;
}

function Workspace({ result, live, loading, logs, startedAt }: Props) {
  // while a question runs, the inspector shows the live log. the
  // workspace is mounted again when the run ends (AskView keys it on the
  // run), and then opens the explanation, or the pipeline when the run
  // ended without one, to show where it stopped.
  const [tab, setTab] = useState<InspectorTab>(() =>
    loading ? "log" : result?.explanation && (result.reasoning_graph?.nodes.length ?? 0) > 1 ? "explanation" : "pipeline",
  );
  const [selection, setSelection] = useState<Selection>(null);
  const [focusAnswer, setFocusAnswer] = useState<string | null>(null);
  // hover, linked both ways: the answers under the graph's pointer are
  // marked in the table; the row under the table's pointer lights its
  // paths in the graph.
  const [graphHover, setGraphHover] = useState<ReadonlySet<string>>(() => new Set());
  const [graphFacts, setGraphFacts] = useState<ReadonlySet<string>>(() => new Set());
  const [tableHover, setTableHover] = useState<string | null>(null);
  const [highlightFact, setHighlightFact] = useState<string | null>(null);
  const now = useClock(loading);
  const elapsed = startedAt ? Math.max(0, (now - startedAt) / 1000) : 0;

  const graph: Graph | null = useMemo(() => {
    if (loading) return liveGraphOf(live);
    const g = result?.reasoning_graph;
    return g && g.nodes.length > 1 ? g : null;
  }, [loading, live, result]);
  const ghost = useMemo(() => (loading ? ghostOf(live) : null), [loading, live]);
  const trace = useMemo(
    () => (loading ? traceOfLive(live, logs, elapsed) : result ? traceOfResult(result) : []),
    [loading, live, logs, elapsed, result],
  );

  const { rows, total } = useMemo(() => {
    if (!loading) return rankedRows(result);
    const answers = (live.answers ?? []).map((a) => ({ rank: null, curie: a.curie, name: a.label ?? a.curie, score: null, facts: null }));
    return { rows: answers, total: live.nResults };
  }, [loading, live, result]);
  const picked = useMemo(
    () => new Set((loading ? (live.answers ?? []) : result ? pickedAnswers(result) : []).map((a) => a.curie)),
    [loading, live, result],
  );
  const ranks = useMemo(
    () => new Map(rows.filter((r) => r.rank !== null).map((r) => [r.curie, r.rank as number])),
    [rows],
  );

  const onGraphSelect = (next: Selection) => {
    setSelection(next);
    if (next?.kind === "node" && graph?.nodes.some((n) => n.id === next.id && n.role === "answer")) setFocusAnswer(next.id);
    if (next) setTab("selection");
  };
  const onTableSelect = (curie: string | null) => {
    setFocusAnswer(curie);
    if (curie && graph?.nodes.some((n) => n.id === curie)) {
      setSelection({ kind: "node", id: curie });
      setTab("selection");
    }
  };
  const openStage = (anchor: string) => {
    setTab("pipeline");
    // after the pipeline tab has rendered: open and show the stage.
    window.requestAnimationFrame(() => {
      const row = document.getElementById(`stage-${anchor}`);
      row?.querySelector("details")?.setAttribute("open", "");
      row?.scrollIntoView({ block: "start", behavior: "smooth" });
    });
  };

  const active = trace.find((s) => s.state === "active");
  const question = result?.question ?? "";
  // wide screens: three fixed panes, each scrolling on its own. narrow
  // ones: the panes stack and the whole workspace scrolls.
  return (
    <div className="scroll-thin flex min-h-0 flex-1 flex-col overflow-y-auto lg:overflow-hidden">
      <QueryBand result={result} live={live} loading={loading} elapsed={elapsed} />
      <TraceStrip steps={trace} onOpen={result ? openStage : undefined} />
      <div className="grid grid-cols-1 lg:min-h-0 lg:flex-1 lg:grid-cols-[minmax(15rem,20%)_minmax(0,1fr)_minmax(20rem,30%)]">
        <section aria-label="Answers" className="h-[22rem] border-b border-zinc-200 dark:border-zinc-800 lg:h-auto lg:min-h-0 lg:border-b-0 lg:border-r">
          <AnswersTable
            rows={rows}
            total={total}
            picked={picked}
            selected={focusAnswer}
            onSelect={onTableSelect}
            linked={graphHover}
            onHover={setTableHover}
            placeholder={loading ? "ARAX's answers appear here when it has ranked them." : "This run has no ranked answers."}
          />
        </section>
        <section aria-label="Reasoning graph" className="flex min-h-[28rem] min-w-0 flex-col border-b border-zinc-200 dark:border-zinc-800 lg:min-h-0 lg:border-b-0 lg:border-r">
          {graph ? (
            <>
              <div className="min-h-0 flex-1">
                <ReasoningGraph
                  graph={graph}
                  live={loading}
                  ghost={ghost}
                  ranks={ranks}
                  focusAnswerId={focusAnswer}
                  hoverAnswerId={tableHover}
                  onHoverAnswers={(curies) => setGraphHover(new Set(curies))}
                  onHoverFacts={(ids) => setGraphFacts(new Set(ids))}
                  highlightFactId={highlightFact}
                  onSelect={onGraphSelect}
                  fill
                />
              </div>
              {!loading && (
                <figure className="scroll-thin m-0 flex max-h-[38%] shrink-0 flex-col gap-2 overflow-y-auto border-t border-zinc-200 px-3 py-2 dark:border-zinc-800">
                  <FigureLegend graph={graph} />
                  <FigureCaption graph={graph} question={question} />
                </figure>
              )}
            </>
          ) : (
            <NoGraph result={result} loading={loading} />
          )}
        </section>
        <section aria-label="Inspector" className="min-h-[24rem] lg:min-h-0">
          <Inspector
            tab={tab}
            onTab={setTab}
            result={result}
            graph={graph && !loading ? graph : null}
            selection={selection}
            logs={logs}
            loading={loading}
            waitingFor={active ? `${active.label}${active.seconds !== null ? `, ${Math.round(active.seconds)} s` : ""}` : null}
            onHighlightFact={setHighlightFact}
            linkedFacts={graphFacts}
          />
        </section>
      </div>
    </div>
  );
}

function NoGraph({ result, loading }: { result: QueryResponse | null; loading: boolean }) {
  if (loading) {
    return (
      <>
        <div className="flex h-8 shrink-0 items-center border-b border-zinc-200 px-3 text-[11.5px] text-zinc-500 dark:border-zinc-800">
          Reasoning graph
        </div>
        <p className="px-4 py-4 text-[12px] text-zinc-500 dark:text-zinc-400">
          The graph grows here as the pipeline finds the entity, the query and the answers.
        </p>
      </>
    );
  }
  if (!result) return null;
  return (
    <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
      <div className="flex h-8 shrink-0 items-center border-b border-zinc-200 px-3 text-[11.5px] text-zinc-500 dark:border-zinc-800">
        No reasoning graph: the run ended as&nbsp;
        <span className={TONE_CLASS[toneOf(result.success ? "ok" : "failed", result.outcome)]}>
          {outcomeText(result.success ? "ok" : "failed", result.outcome)}
        </span>
      </div>
      {result.explanation && (
        <div className="fade-in px-4 py-3">
          <MarkdownAnswer text={result.explanation} bare />
        </div>
      )}
    </div>
  );
}

// -------------------------------------------------------------- query band

function QueryBand({
  result,
  live,
  loading,
  elapsed,
}: {
  result: QueryResponse | null;
  live: LiveState;
  loading: boolean;
  elapsed: number;
}) {
  const written: QueryReading | null = loading
    ? readQuery(live.queryGraph)
    : readQuery(queryGraphOf(result?.intermediates.trapi_query));
  const sent = loading ? null : readQuery(queryGraphOf(result?.intermediates.reasoner_request));
  const label = loading ? (live.entity?.label ?? null) : result ? pinnedLabel(result) : null;
  const rewritten = written && sent && queryLine(written) !== queryLine(sent) ? sent : null;
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-6 gap-y-2 border-b border-zinc-200 bg-zinc-50/80 px-4 py-2.5 dark:border-zinc-800 dark:bg-zinc-900/50">
      <div className="flex w-24 shrink-0 flex-col">
        <span className="text-[10.5px] font-semibold uppercase tracking-wider text-zinc-500">Interpreted as</span>
        <span className="text-[11px] text-zinc-400 dark:text-zinc-500">one-hop TRAPI</span>
      </div>
      {written ? (
        <div className="flex min-w-0 flex-1 items-start gap-3">
          <QueryNode end={written.subject} label={label} />
          <QueryEdge reading={written} rewritten={rewritten} />
          <QueryNode end={written.object} label={label} />
        </div>
      ) : (
        <p className="flex-1 text-[12.5px] text-zinc-500">
          {loading
            ? live.entity
              ? `Found the entity “${live.entity.label ?? live.entity.curie}”; building the query…`
              : "Reading the question…"
            : "No query was built for this question."}
        </p>
      )}
      <div className="ml-auto flex shrink-0 items-center gap-3 text-[11.5px] text-zinc-500">
        {loading ? (
          <>
            {live.araxNote && live.nResults === null && (
              <span className="max-w-[22rem] truncate" title={live.araxNote}>
                ARAX: {live.araxNote}
              </span>
            )}
            <span className="tabular-nums">{Math.round(elapsed)} s</span>
          </>
        ) : (
          result && <RunMeta result={result} />
        )}
      </div>
    </div>
  );
}

function QueryNode({ end, label }: { end: QueryEnd; label: string | null }) {
  const kind = kindOf(end.category ? `biolink:${end.category}` : null);
  const name = end.pinned ? (label ?? end.ids[0]) : `any ${end.category ?? "entity"}`;
  return (
    <div className="flex min-w-0 max-w-[16rem] items-center gap-2">
      {end.pinned ? (
        <Glyph kind={kind} size={30} />
      ) : (
        <span className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-full border-[1.5px] border-dashed border-zinc-400 text-[13px] font-semibold text-zinc-500 dark:border-zinc-600">
          ?
        </span>
      )}
      <span className="flex min-w-0 flex-col leading-tight">
        <span className="truncate text-[13px] font-medium text-zinc-900 dark:text-zinc-100" title={name}>
          {name}
        </span>
        <span className="truncate text-[11px] text-zinc-500">
          {end.pinned ? (
            <>
              <span className="font-mono">{end.ids.join(", ")}</span>
              {end.category ? ` · ${end.category}` : ""}
            </>
          ) : (
            "the answers ARAX ranks"
          )}
        </span>
      </span>
    </div>
  );
}

function QueryEdge({ reading, rewritten }: { reading: QueryReading; rewritten: QueryReading | null }) {
  return (
    <div className="flex min-w-[12rem] max-w-[22rem] flex-1 flex-col items-stretch pt-1">
      <div className="flex items-center text-[11.5px]">
        <span className="h-px flex-1 bg-zinc-400 dark:bg-zinc-600" />
        <span className="mx-1 rounded border border-zinc-300 bg-white px-1.5 py-0.5 font-medium italic text-zinc-800 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-200">
          {reading.predicate ?? "related to"}
        </span>
        <span className="h-px flex-1 bg-zinc-400 dark:bg-zinc-600" />
        <svg width="7" height="9" viewBox="0 0 7 9" aria-hidden className="text-zinc-400 dark:text-zinc-600">
          <path d="M0 0 7 4.5 0 9z" fill="currentColor" />
        </svg>
      </div>
      <div className="mt-1 text-center text-[10.5px] leading-snug text-zinc-500">
        {rewritten ? (
          <span title="ask_arax_to_reason put the LLM's query in the form ARAX reasons on">
            as the LLM wrote it · code sent ARAX: {queryLine(rewritten)}
          </span>
        ) : (
          [reading.inferred ? "inferred: ARAX reasons over paths" : "a direct lookup of facts", ...reading.qualifiers].join(" · ")
        )}
      </div>
    </div>
  );
}

function RunMeta({ result }: { result: QueryResponse }) {
  const tone = toneOf(result.success ? "ok" : "failed", result.outcome);
  const question = result.question ?? "";
  const model = result.model_id ?? "";
  const exportButton =
    "h-6 rounded border border-zinc-300 px-1.5 text-[11px] text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800";
  return (
    <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <span className={`font-medium ${TONE_CLASS[tone]}`}>{outcomeText(result.success ? "ok" : "failed", result.outcome)}</span>
      <span className="font-mono tabular-nums">{model}</span>
      <span className="tabular-nums">{seconds(result.elapsed_s)}</span>
      <span className="tabular-nums">{usd(result.cost_usd)}</span>
      <span className="hidden font-mono text-[10.5px] xl:inline" title="run id">
        {result.run_id}
      </span>
      <span className="flex gap-1">
        <button type="button" className={exportButton} onClick={() => downloadResultPDF(question, model, result)}>
          PDF
        </button>
        <button type="button" className={exportButton} onClick={() => downloadResultMarkdown(question, model, result)}>
          MD
        </button>
        <button type="button" className={exportButton} onClick={() => downloadResultJSON(question, model, result)}>
          JSON
        </button>
      </span>
    </span>
  );
}
