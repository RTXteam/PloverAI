"use client";

// the run's pipeline, for the inspector: every one of the 15 stages in
// order with its kind (LLM / service / function), the prompts and model
// output of the LLM stages and the artifact of the others
// (StageList), and every raw intermediate the run wrote (RawArtifacts).

import { JsonView } from "./JsonView";
import type { QueryResponse, StagePromptEntry } from "@/lib/api";

// the full 15-stage pipeline ladder. each entry describes one stage as
// the pipeline names it: number (matches log lines and prompt_log keys
// 1:1), label, one-line description, and `kind` — one of:
//   "LLM"      — call to an OpenRouter model; carries a prompt artifact.
//   "service"  — HTTP call to an external service (NameRes, NodeNorm,
//                ARAX, Retriever); carries an intermediate artifact.
//   "function" — pure local computation (re-rank, similarity check,
//                graph-view assembly); no separate artifact, but listed
//                so the UI reflects the actual pipeline order.
//
// for LLM stages the row reads the entry from `intermediates.prompts`
// via `promptKey`. for service stages the row reads its data slice via
// `getData(r)`. for function stages there's nothing to render — the
// row shows the stage description and a "pure function" note when
// expanded.
type StageKind = "LLM" | "service" | "function";

type StageEntry = {
  number: string;
  label: string;
  description: string;
  kind: StageKind;
  promptKey?: string;
  getData?: (r: QueryResponse) => unknown;
  // longer explanation for pure-function stages that don't expose any
  // intermediate artifact (Stages 5 + 7). shown in the expanded row in
  // place of the JsonView so the user understands what the stage did
  // without having to read pipeline.py.
  details?: string;
};

// how much citable evidence the reasoning graph's facts carry (Stage 14).
function evidenceCounts(graph: NonNullable<QueryResponse["reasoning_graph"]>) {
  const facts = graph.edges;
  const sum = (pick: (e: (typeof facts)[number]) => number) => facts.reduce((total, e) => total + pick(e), 0);
  return {
    facts: facts.length,
    approvals: facts.filter((e) => e.evidence?.approval).length,
    fda_applications: sum((e) => e.evidence?.fda_approvals.length ?? 0),
    drug_labels: sum((e) => e.evidence?.labels.length ?? 0),
    registered_trials: sum((e) => e.evidence?.n_trials ?? 0),
    pubmed_articles: sum((e) => e.evidence?.n_pmids ?? 0),
  };
}

// helper: shorten the nasty `any` chain we'd otherwise need to dig
// `intermediates.nodenorm.pinned` / `.answers` out of an unknown blob.
function nodenormSlice(r: QueryResponse, key: "pinned" | "answers"): unknown {
  const n = r.intermediates.nodenorm as Record<string, unknown> | null | undefined;
  return n?.[key] ?? null;
}

const STAGE_ORDER: StageEntry[] = [
  {
    number: "1",
    label: "Scope check",
    description: "Guardrail: LLM decides whether the input is a biomedical question worth running.",
    kind: "LLM",
    promptKey: "stage_1_scope_check",
  },
  {
    number: "2",
    label: "Entity extract",
    description: "LLM picks the focal biomedical entity name from the question.",
    kind: "LLM",
    promptKey: "stage_2_entity_extract",
  },
  {
    number: "3",
    label: "NameRes lookup",
    description: "RENCI Name Resolution: free-text entity name → ranked CURIE candidates via BM25.",
    kind: "service",
    getData: (r) => r.intermediates.nameres,
  },
  {
    number: "4",
    label: "Candidate pick",
    description: "LLM picks the best NameRes candidate (or declares 'no match') to defend against typos and label-type collisions. The first candidates are checked against the Translator Tier 0 graph first, so the LLM avoids a perfect-label CURIE the graph has no facts for.",
    kind: "LLM",
    promptKey: "stage_4_candidate_pick",
    getData: (r) => r.intermediates.candidate_probes,
  },
  {
    number: "5",
    label: "IC re-rank",
    description: "If the question prefers a general concept, re-rank NameRes candidates by NodeNorm information_content (ascending = more general).",
    kind: "function",
    details:
      "NodeNorm reports an information_content score for every CURIE — low values mean broad concepts (e.g. \"Cancer\"), high values mean niche ones (e.g. \"small-cell lung carcinoma stage IIIB\"). When the extractor (Stage 2) labels the question as asking for a GENERAL concept, this step re-sorts the top-K NameRes candidates by ascending IC so the broadest match wins. For questions tagged as SPECIFIC, the original BM25 ranking from NameRes is kept untouched.",
  },
  {
    number: "6",
    label: "NodeNorm canonicalize pinned",
    description: "RENCI Node Normalization: resolve the pinned CURIE to its canonical form + Biolink categories.",
    kind: "service",
    getData: (r) => nodenormSlice(r, "pinned"),
  },
  {
    number: "7",
    label: "Consistency check",
    description: "difflib similarity between the user's mention and the resolved canonical label. Aborts the run if the resolved label drifts too far, so ARAX is never asked about the wrong entity.",
    kind: "function",
    details:
      "Compares the user's free-text mention against the canonical label that NameRes + NodeNorm resolved to, using the max of difflib's SequenceMatcher.ratio (character-level similarity) and a substring-containment check. If the resulting score falls below 0.50, the run aborts with status low_confidence_resolution rather than firing a TRAPI query against a probably-wrong entity. This is what catches a typo like \"diabites\" silently landing on \"sialidosis type 2\" (score ≈ 0.38) while still passing genuine near-matches like \"warfrin\" → \"warfarin\" (score ≈ 0.93).",
  },
  {
    number: "8",
    label: "TRAPI build",
    description: "LLM constructs the TRAPI query graph from the pinned entity, and asks ARAX to reason (knowledge_type inferred) for treatment and gene-regulation questions. Predicates come from the Tier 0 graph, with fact counts for the chosen entity.",
    kind: "LLM",
    promptKey: "stage_8_trapi_build",
    getData: (r) => r.intermediates.predicate_probe,
  },
  {
    number: "9",
    label: "Validation",
    description: "reasoner-validator gate: TRAPI schema + Biolink check. Invalid → pipeline stops before ARAX.",
    kind: "function",
    getData: (r) => r.intermediates.validation,
  },
  {
    number: "10",
    label: "ARAX query",
    description: "POST the validated query to ARAX, the Translator reasoner, which reasons over the Tier 0 knowledge graph and returns ranked answers with the evidence behind each.",
    kind: "service",
    getData: (r) => ({
      request: r.intermediates.reasoner_request,
      response_summary: r.intermediates.reasoner_response_summary,
    }),
  },
  {
    number: "11",
    label: "Answer pick",
    description: "LLM selects the answers from ARAX's ranked list, reading each answer's score, evidence and shortest reasoning paths.",
    kind: "LLM",
    promptKey: "stage_11_answer_pick",
  },
  {
    number: "12",
    label: "NodeNorm canonicalize answers",
    description: "Canonicalize every CURIE the LLM picked + collect equivalents for downstream scoring.",
    kind: "service",
    getData: (r) => nodenormSlice(r, "answers"),
  },
  {
    number: "13",
    label: "Reasoning graph",
    description: "Trace each picked answer's reasoning paths to the question's entity through ARAX's evidence, and number the facts on them F1…Fn: the graph above.",
    kind: "function",
    getData: (r) =>
      r.reasoning_graph
        ? {
            answers: r.reasoning_graph.nodes.filter((n) => n.role === "answer").length,
            entities: r.reasoning_graph.nodes.length,
            facts: r.reasoning_graph.edges.length,
          }
        : // a lookup-condition run, opened by URL: its answer graph view.
          r.answer_graph_view
        ? {
            n_pinned: 1,
            n_answers: r.answer_graph_view.answer_nodes.length,
            n_edges: r.answer_graph_view.edges.length,
          }
        : null,
  },
  {
    number: "14",
    label: "Evidence extraction",
    description: "Read what each fact rests on: approvals and FDA applications, drug labels (DailyMed), registered trials (ClinicalTrials.gov), PubMed articles and literature co-mention. These are the citations you can hover in the explanation.",
    kind: "function",
    getData: (r) =>
      r.reasoning_graph
        ? evidenceCounts(r.reasoning_graph)
        : // the lookup condition's PubTator check (both null if the run stopped early).
          {
            call_summary: r.answer_graph_view?.pubtator_call_summary ?? null,
            metrics: r.answer_graph_view?.pubtator_metrics ?? null,
          },
  },
  {
    number: "15",
    label: "Explanation",
    description: "LLM writes the summary above from the numbered facts, citing each claim as [F#].",
    kind: "LLM",
    promptKey: "stage_15_explain",
  },
];

export function StageList({ r }: { r: QueryResponse }) {
  // resolve prompts blob once so the per-row lookup is a single read.
  const prompts = (r.intermediates.prompts as Record<string, StagePromptEntry> | null) ?? null;
  return (
    <div>
      <p className="px-3 py-2 text-[11px] leading-relaxed text-zinc-500 dark:text-zinc-400 border-b border-zinc-200 dark:border-zinc-800">
        All 15 stages in order. LLM rows hold the prompts and the model&apos;s output, service rows the artifact,
        function rows run locally. Stages that did not run are dimmed.
      </p>
      <ol className="divide-y divide-zinc-200 dark:divide-zinc-800">
        {STAGE_ORDER.map((s) => (
          <StageRow
            key={s.number}
            stage={s}
            promptEntry={s.promptKey ? (prompts?.[s.promptKey] ?? null) : null}
            data={s.getData ? s.getData(r) : undefined}
          />
        ))}
      </ol>
    </div>
  );
}

function StageRow({
  stage,
  promptEntry,
  data,
}: {
  stage: StageEntry;
  promptEntry: StagePromptEntry | null;
  // undefined ⇒ this kind of stage has no data accessor at all
  // (function with nothing to render). null / falsy ⇒ accessor exists
  // but the stage didn't produce data this run.
  data: unknown;
}) {
  // did the stage produce a usable artifact?
  //   LLM      → prompt entry present
  //   service  → data accessor returned something non-null (we treat
  //              "all child keys null" as also empty so the row is
  //              honestly dimmed when the stage didn't run)
  //   function → data accessor returned non-null (Stage 9 validation,
  //              Stage 13 graph view), else "pure function" placeholder
  const ran =
    stage.kind === "LLM"
      ? promptEntry != null
      : stage.getData
      ? hasSomeValue(data)
      : true; // pure function with no accessor — always considered "ran"

  const resp = promptEntry?.response;
  const reasoning = resp?.reasoning;
  // badge sizing: 1-digit numbers fit in a circle; 2-digit ("10"..."15")
  // need a wider rounded rect so the digit pair doesn't crowd the edge.
  const isWide = stage.number.length > 1;

  return (
    <li id={`stage-${stage.number}`} className={`scroll-mt-2 ${ran ? "" : "opacity-60"}`}>
      <details className="group">
        <summary className="cursor-pointer list-none px-3 py-2 flex items-start gap-2.5 hover:bg-zinc-50 dark:hover:bg-zinc-900/40">
          <span
            className={
              "inline-flex items-center justify-center text-[11px] font-mono " +
              "bg-zinc-100 dark:bg-zinc-800 text-zinc-700 dark:text-zinc-300 shrink-0 " +
              (isWide
                ? "h-6 px-2 rounded-md min-w-[2.25rem]"
                : "h-6 w-6 rounded-full")
            }
            title={`Stage ${stage.number}`}
          >
            {stage.number}
          </span>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-medium">{stage.label}</span>
              <KindChip kind={stage.kind} />
              {stage.kind === "LLM" && resp?.input_tokens !== undefined && (
                <span className="text-[11px] font-mono text-zinc-500 dark:text-zinc-400">
                  in={resp.input_tokens}tok · out={resp.output_tokens}tok · {resp.latency_s?.toFixed(2)}s
                </span>
              )}
              {reasoning && <ReasoningBadge />}
              {!ran && (
                <span className="text-[10px] uppercase tracking-wider font-medium px-1.5 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800 text-zinc-500 dark:text-zinc-400">
                  did not run
                </span>
              )}
            </div>
            <p className="text-xs text-zinc-500 dark:text-zinc-400 mt-0.5">
              {stage.description}
            </p>
          </div>
          <Chevron />
        </summary>
        <StageRowDetails
          stage={stage}
          promptEntry={promptEntry}
          data={data}
          ran={ran}
        />
      </details>
    </li>
  );
}

function StageRowDetails({
  stage,
  promptEntry,
  data,
  ran,
}: {
  stage: StageEntry;
  promptEntry: StagePromptEntry | null;
  data: unknown;
  ran: boolean;
}) {
  // unified expanded view — branches by stage kind. all three branches
  // share the same outer padding so the visual rhythm of the list
  // stays even.
  return (
    <div className="px-3 pb-3 pt-1 flex flex-col gap-3">
      {stage.kind === "LLM" && promptEntry && (
        <>
          {/* supplemental data the LLM saw at call time, when the stage
              defines a getData accessor. for Stage 8 this is the
              predicate-density probe — rendered ABOVE the prompts so
              the user sees "here is what we measured" → "here is what
              we asked the LLM" → "here is what the LLM said". */}
          {stage.getData && hasSomeValue(data) && (
            <div>
              <div className="text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1.5">
                Probe data injected into the prompt
              </div>
              <JsonView value={data} maxLines={30} />
            </div>
          )}
          {promptEntry.system && (
            <CollapsedBlock title="System prompt" value={promptEntry.system} />
          )}
          {(promptEntry.user || promptEntry.user_truncated) && (
            <CollapsedBlock
              title={promptEntry.user_truncated ? "User prompt (truncated)" : "User prompt"}
              value={promptEntry.user ?? promptEntry.user_truncated ?? ""}
            />
          )}
          {promptEntry.response?.reasoning && (
            <div>
              <div className="text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1.5">
                Reasoning
              </div>
              <div className="text-xs leading-relaxed whitespace-pre-wrap rounded border border-amber-200 dark:border-amber-900/60 bg-amber-50/50 dark:bg-amber-950/20 p-3 font-mono">
                {promptEntry.response.reasoning}
              </div>
            </div>
          )}
          {promptEntry.response?.finish_reason && (
            <div className="text-[11px] font-mono text-zinc-500 dark:text-zinc-400">
              finish_reason: {promptEntry.response.finish_reason}
              {promptEntry.response.model_returned
                ? ` · model: ${promptEntry.response.model_returned}`
                : ""}
            </div>
          )}
        </>
      )}

      {stage.kind === "service" && ran && (
        <div>
          <div className="text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1.5">
            Intermediate artifact
          </div>
          <JsonView value={data} maxLines={40} />
        </div>
      )}

      {stage.kind === "function" && stage.getData && ran && (
        <div>
          <div className="text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1.5">
            Summary
          </div>
          <JsonView value={data} maxLines={40} />
        </div>
      )}

      {stage.kind === "function" && !stage.getData && (
        <div>
          <div className="text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1.5">
            What this stage does
          </div>
          <p className="text-xs leading-relaxed text-zinc-600 dark:text-zinc-300">
            {stage.details ??
              "Pure local computation with no separate artifact emitted to the intermediates blob."}
          </p>
        </div>
      )}

      {!ran && (stage.kind === "LLM" || stage.kind === "service") && (
        <p className="text-xs leading-relaxed text-zinc-500 dark:text-zinc-400 italic">
          Stage did not run for this query. The pipeline stopped earlier
          (out_of_scope refusal, entity resolution failure, validator
          rejection, etc.) so this stage was never reached.
        </p>
      )}
    </div>
  );
}

// helper: deep-ish "has something useful" check. unwraps a one-level
// object so {pinned: null, answers: null} is treated as empty even
// though the outer object is non-null. used to honestly mark a row
// as "didn't run" when the accessor returns a sentinel envelope.
function hasSomeValue(v: unknown): boolean {
  if (v == null) return false;
  if (typeof v !== "object") return true;
  const obj = v as Record<string, unknown>;
  if (Array.isArray(v)) return v.length > 0;
  return Object.values(obj).some((x) => x != null);
}

function KindChip({ kind }: { kind: StageKind }) {
  // amber for LLM (matches reasoning chip), sky for service (HTTP
  // call), zinc for function (local). small + uppercase to read as a
  // tag, not a button.
  const styles =
    kind === "LLM"
      ? "bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
      : kind === "service"
      ? "bg-sky-100 text-sky-800 dark:bg-sky-950/60 dark:text-sky-300"
      : "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300";
  return (
    <span className={`text-[10px] uppercase tracking-wider font-medium px-1.5 py-0.5 rounded ${styles}`}>
      {kind}
    </span>
  );
}

function CollapsedBlock({ title, value }: { title: string; value: string }) {
  return (
    <details>
      <summary className="cursor-pointer text-xs font-medium text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100">
        {title}
      </summary>
      <pre className="mt-1.5 text-[11px] leading-relaxed whitespace-pre-wrap rounded border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950/60 p-3 font-mono max-h-72 overflow-y-auto">
        {value}
      </pre>
    </details>
  );
}

export function RawArtifacts({ r }: { r: QueryResponse }) {
  const items: Array<[string, unknown]> = [
    ["nameres", r.intermediates.nameres],
    ["candidate_probes", r.intermediates.candidate_probes],
    ["nodenorm", r.intermediates.nodenorm],
    ["predicate_probe", r.intermediates.predicate_probe],
    ["validation", r.intermediates.validation],
    ["reasoner_request", r.intermediates.reasoner_request],
    ["reasoner_response_summary", r.intermediates.reasoner_response_summary],
    ["reduced_data", r.intermediates.reduced_data],
    ["answer", r.answer],
    ["cost", r.intermediates.cost],
  ];
  return (
    <div className="flex flex-col gap-2 p-3">
        {items.map(([key, value]) => (
          <details key={key} className="rounded border border-zinc-200 dark:border-zinc-800 overflow-hidden">
            <summary className="cursor-pointer px-3 py-1.5 text-xs font-mono select-none bg-zinc-100 dark:bg-zinc-800">
              {key}
            </summary>
            {value === null || value === undefined ? (
              <p className="text-xs text-zinc-500 p-3 italic">(not produced)</p>
            ) : (
              <JsonView value={value} maxLines={120} />
            )}
          </details>
        ))}
    </div>
  );
}

function ReasoningBadge() {
  return (
    <span className="text-[10px] uppercase tracking-wider font-medium px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300">
      reasoning
    </span>
  );
}

function Chevron() {
  return (
    <svg
      className="text-zinc-400 shrink-0 transition-transform group-open:rotate-90"
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
