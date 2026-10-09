"use client";

// the right pane of the workbench: one run, read in tabs.
//   explanation — the LLM's narrative, every [F#] a chip with its
//                 evidence; hovering one isolates its step in the graph;
//   evidence    — every fact, like a paper's reference list;
//   selection   — the node or link clicked in the graph;
//   query       — the TRAPI query the LLM wrote, the one sent to ARAX,
//                 and the validator's verdict;
//   pipeline    — all 15 stages with prompts and artifacts;
//   raw         — every intermediate the run wrote;
//   log         — the pipeline's log lines as they streamed.
// the page owns the tab, so it can switch it on events (a run starts, a
// node is clicked) without an effect.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { QueryResponse, ReasoningGraph } from "@/lib/api";
import { MarkdownAnswer } from "@/components/MarkdownAnswer";
import { EvidenceList, FactCitation, INITIAL_FACTS } from "@/components/FactEvidence";
import { SelectionDetails, type Selection } from "@/components/ReasoningGraph";
import { RawArtifacts, StageList } from "@/components/PipelineStages";
import { JsonView } from "@/components/JsonView";
import { queryGraphOf, queryLine, readQuery } from "@/lib/queryGraph";

export type InspectorTab = "explanation" | "evidence" | "selection" | "query" | "pipeline" | "raw" | "log";

export type LogLine = { level: string; msg: string; t: number };

const FLASH_MS = 1600;

export function Inspector({
  tab,
  onTab,
  result,
  graph,
  selection,
  logs,
  loading,
  waitingFor,
  onHighlightFact,
  linkedFacts,
}: {
  tab: InspectorTab;
  onTab: (tab: InspectorTab) => void;
  result: QueryResponse | null;
  graph: ReasoningGraph | null;
  selection: Selection;
  logs: LogLine[];
  loading: boolean;
  // while a query runs: the step it is on, for the explanation placeholder.
  waitingFor: string | null;
  onHighlightFact: (factId: string | null) => void;
  // the facts behind what the graph's pointer is over.
  linkedFacts?: ReadonlySet<string>;
}) {
  // the pane scrolls, so it lists every fact: a fact lit from the graph is always there.
  const [showAllFacts, setShowAllFacts] = useState(true);
  const [flashId, setFlashId] = useState<string | null>(null);
  const labels = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n.label])), [graph]);
  const hasGraph = Boolean(graph && graph.edges.length > 0);

  const openFact = useCallback(
    (id: string) => {
      if (!graph) return;
      const index = graph.edges.findIndex((f) => f.fact_id === id);
      if (index >= INITIAL_FACTS) setShowAllFacts(true);
      onTab("evidence");
      setFlashId(id);
      window.setTimeout(() => setFlashId((current) => (current === id ? null : current)), FLASH_MS);
      // after the evidence tab has rendered the (possibly newly shown) row.
      window.requestAnimationFrame(() =>
        document.getElementById(`fact-${id}`)?.scrollIntoView({ block: "center", behavior: "smooth" }),
      );
    },
    [graph, onTab],
  );

  const renderFact = useCallback(
    (citation: string) =>
      graph ? (
        <FactCitation citation={citation} graph={graph} labels={labels} onHighlight={onHighlightFact} onOpen={openFact} />
      ) : (
        <code>{citation}</code>
      ),
    [graph, labels, onHighlightFact, openFact],
  );

  const tabs: { id: InspectorTab; label: string; show: boolean }[] = [
    // a run without a graph shows its explanation in the middle pane; a
    // running one shows where the explanation will be.
    { id: "explanation", label: "Explanation", show: loading || Boolean(result?.explanation && hasGraph) },
    { id: "evidence", label: `Evidence${hasGraph ? ` · ${graph?.edges.length}` : ""}`, show: hasGraph },
    { id: "selection", label: "Selected", show: Boolean(selection && graph) },
    { id: "query", label: "Query", show: Boolean(result?.intermediates.trapi_query) },
    { id: "pipeline", label: "Pipeline", show: Boolean(result) },
    { id: "raw", label: "Raw", show: Boolean(result) },
    { id: "log", label: "Log", show: loading || logs.length > 0 },
  ];
  const visible = tabs.filter((t) => t.show);
  const active = visible.some((t) => t.id === tab) ? tab : (visible[0]?.id ?? null);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div role="tablist" className="scroll-thin flex h-8 shrink-0 items-stretch gap-3 overflow-x-auto border-b border-zinc-200 px-3 text-[11.5px] dark:border-zinc-800">
        {visible.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={active === t.id}
            onClick={() => onTab(t.id)}
            className={`-mb-px flex items-center whitespace-nowrap border-b-2 ${
              active === t.id
                ? "border-sky-600 text-zinc-900 dark:border-sky-400 dark:text-zinc-100"
                : "border-transparent text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div data-inspector-body className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        {active === null && (
          <p className="px-3 py-4 text-[12px] text-zinc-500 dark:text-zinc-400">
            The explanation, the evidence behind every fact, the query and the pipeline appear here.
          </p>
        )}
        {active === "explanation" && loading && <ExplanationPlaceholder waitingFor={waitingFor} />}
        {active === "explanation" && !loading && result?.explanation && (
          <div className="fade-in px-3 py-3">
            <MarkdownAnswer text={result.explanation} renderFact={renderFact} bare />
          </div>
        )}
        {active === "evidence" && graph && (
          <EvidenceList
            graph={graph}
            labels={labels}
            showAll={showAllFacts}
            onShowAll={() => setShowAllFacts(true)}
            flashId={flashId}
            onHighlight={onHighlightFact}
            linked={linkedFacts}
            bare
          />
        )}
        {active === "selection" && selection && graph && (
          <div className="px-3 py-3">
            <SelectionDetails selection={selection} graph={graph} />
          </div>
        )}
        {active === "query" && result && <QueryTab result={result} />}
        {active === "pipeline" && result && <StageList r={result} />}
        {active === "raw" && result && <RawArtifacts r={result} />}
        {active === "log" && <LogTab logs={logs} loading={loading} />}
      </div>
    </div>
  );
}

// where the explanation will appear, while the run is still going: its
// three sections as grey lines, and the step the run is on.
function ExplanationPlaceholder({ waitingFor }: { waitingFor: string | null }) {
  const bar = (width: string) => <span className="block h-2.5 rounded bg-zinc-100 dark:bg-zinc-800" style={{ width }} />;
  const section = (title: string, widths: string[]) => (
    <section className="flex flex-col gap-2">
      <h4 className="text-[13px] font-semibold text-zinc-400 dark:text-zinc-600">{title}</h4>
      {widths.map((w, i) => (
        <span key={i}>{bar(w)}</span>
      ))}
    </section>
  );
  return (
    <div className="flex flex-col gap-5 px-3 py-3" aria-busy="true">
      <p className="text-[11.5px] text-zinc-500 dark:text-zinc-400">
        The explanation is written once ARAX has answered and the evidence is traced.
        {waitingFor ? ` Now: ${waitingFor}.` : ""}
      </p>
      <div className="flex animate-pulse flex-col gap-5">
        {section("Summary", ["96%", "92%", "88%", "60%"])}
        {section("Why each answer", ["94%", "90%", "72%", "93%", "85%", "55%"])}
        {section("How strong is the evidence", ["91%", "87%", "64%"])}
      </div>
    </div>
  );
}

function QueryTab({ result }: { result: QueryResponse }) {
  const written = readQuery(queryGraphOf(result.intermediates.trapi_query));
  const sent = readQuery(queryGraphOf(result.intermediates.reasoner_request));
  const rewritten = written && sent && queryLine(written) !== queryLine(sent);
  return (
    <div className="flex flex-col gap-3 px-3 py-3 text-[12px]">
      <section className="flex flex-col gap-1.5">
        <h4 className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">Written by the LLM (Stage 8)</h4>
        {written && <p className="font-mono text-[11.5px] text-zinc-700 dark:text-zinc-300">{queryLine(written)}</p>}
        <JsonView value={result.intermediates.trapi_query} />
      </section>
      {rewritten && (
        <section className="flex flex-col gap-1.5">
          <h4 className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">Sent to ARAX</h4>
          <p className="text-zinc-600 dark:text-zinc-400">
            Code put the query in the form ARAX reasons on (<code className="font-mono">ask_arax_to_reason</code>).
            The benchmark scores the LLM&apos;s query above, not this one.
          </p>
          <p className="font-mono text-[11.5px] text-zinc-700 dark:text-zinc-300">{sent ? queryLine(sent) : ""}</p>
          <JsonView value={result.intermediates.reasoner_request} />
        </section>
      )}
      <section className="flex flex-col gap-1.5">
        <h4 className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">reasoner-validator (Stage 9)</h4>
        <JsonView value={result.intermediates.validation} maxLines={30} />
      </section>
    </div>
  );
}

function LogTab({ logs, loading }: { logs: LogLine[]; loading: boolean }) {
  const end = useRef<HTMLDivElement>(null);
  // follow new lines while the reader is at the bottom; once they scroll
  // up to read one, stay where they are.
  const following = useRef(true);
  useEffect(() => {
    const pane = end.current?.closest<HTMLElement>("[data-inspector-body]");
    if (!pane) return;
    const onScroll = () => {
      following.current = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 48;
    };
    pane.addEventListener("scroll", onScroll, { passive: true });
    return () => pane.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => {
    const pane = end.current?.closest<HTMLElement>("[data-inspector-body]");
    if (pane && following.current) pane.scrollTop = pane.scrollHeight;
  }, [logs.length]);
  return (
    <div className="px-3 py-2 font-mono text-[11px] leading-snug">
      {logs.length === 0 && <p className="text-zinc-500">{loading ? "waiting for the first log line…" : "no log"}</p>}
      {logs.map((line, i) => (
        <div key={i} className="flex gap-2 whitespace-pre-wrap">
          <span className="w-12 shrink-0 tabular-nums text-zinc-400">{line.t.toFixed(1)}s</span>
          <span className={`w-14 shrink-0 ${levelColor(line.level)}`}>{line.level}</span>
          <span className="min-w-0 break-words text-zinc-700 dark:text-zinc-300">{line.msg.replace(/\[\/?[^\]]*\]/g, "")}</span>
        </div>
      ))}
      <div ref={end} />
    </div>
  );
}

function levelColor(level: string): string {
  if (level === "ERROR" || level === "CRITICAL") return "text-red-600 dark:text-red-400";
  if (level === "WARNING") return "text-amber-600 dark:text-amber-400";
  return "text-zinc-500 dark:text-zinc-400";
}
