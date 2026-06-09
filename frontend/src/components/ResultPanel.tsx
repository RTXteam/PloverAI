"use client";

// research-grade result view. shows the run as a stack of focused
// cards: hero meta → markdown answer → TRAPI query (LLM-constructed) →
// pipeline stages with reasoning → raw intermediates. every section
// is independently exportable; the whole run is downloadable as JSON
// or Markdown via the action bar.

import { motion } from "framer-motion";
import { MarkdownAnswer } from "./MarkdownAnswer";
import { StructuredAnswer } from "./StructuredAnswer";
import { JsonView } from "./JsonView";
import {
  downloadResultJSON,
  downloadResultMarkdown,
  downloadResultPDF,
} from "@/lib/export";
import type { QueryResponse, StagePromptEntry } from "@/lib/api";

type Props = {
  question: string;
  model: string;
  r: QueryResponse;
};

// the full 15-stage pipeline ladder. each entry describes one stage as
// the pipeline names it: number (matches log lines and prompt_log keys
// 1:1), label, one-line description, and `kind` — one of:
//   "LLM"      — call to an OpenRouter model; carries a prompt artifact.
//   "service"  — HTTP call to an external service (NameRes, NodeNorm,
//                PloverDB, PubTator); carries an intermediate artifact.
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
    description: "LLM picks the best NameRes candidate (or declares 'no match') to defend against typos and label-type collisions. Per-candidate edge-density probe runs first and is injected so the LLM avoids perfect-label-but-empty-in-KG2c CURIEs.",
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
    description: "difflib similarity between the user's mention and the resolved canonical label. Aborts the run if the resolved label drifts too far so we don't query PloverDB against the wrong entity.",
    kind: "function",
    details:
      "Compares the user's free-text mention against the canonical label that NameRes + NodeNorm resolved to, using the max of difflib's SequenceMatcher.ratio (character-level similarity) and a substring-containment check. If the resulting score falls below 0.50, the run aborts with status low_confidence_resolution rather than firing a TRAPI query against a probably-wrong entity. This is what catches a typo like \"diabites\" silently landing on \"sialidosis type 2\" (score ≈ 0.38) while still passing genuine near-matches like \"warfrin\" → \"warfarin\" (score ≈ 0.93).",
  },
  {
    number: "8",
    label: "TRAPI build",
    description: "LLM constructs the one-hop TRAPI query graph from the pinned entity. A per-CURIE predicate-density probe runs first and is injected into the LLM's prompt so it can prefer populated predicates over plausible-but-empty ones.",
    kind: "LLM",
    promptKey: "stage_8_trapi_build",
    getData: (r) => r.intermediates.predicate_probe,
  },
  {
    number: "9",
    label: "Validation",
    description: "reasoner-validator gate: TRAPI schema + Biolink check. Invalid → pipeline stops without hitting PloverDB.",
    kind: "function",
    getData: (r) => r.intermediates.validation,
  },
  {
    number: "10",
    label: "PloverDB query",
    description: "POST the validated TRAPI query to kg2cploverdb.ci.transltr.io/query and parse the response.",
    kind: "service",
    getData: (r) => ({
      request: r.intermediates.plover_request,
      response_summary: r.intermediates.plover_response_summary,
    }),
  },
  {
    number: "11",
    label: "Answer pick",
    description: "LLM selects the answer entities from the PloverDB response, ranked by relevance (evidence tier breaks ties).",
    kind: "LLM",
    promptKey: "stage_11_answer_pick",
  },
  {
    number: "12",
    label: "NodeNorm canonicalize answers",
    description: "Canonicalize every CURIE the LLM picked, then collapse cross-namespace duplicates (the same protein returned as both a gene id and a ChEMBL target id) so each target is counted once.",
    kind: "service",
    getData: (r) => nodenormSlice(r, "answers"),
  },
  {
    number: "13",
    label: "Build graph view",
    description: "Reshape (pinned entity + picked answers + KG slice) into the node-link graph view, with per-edge provenance, authoritative Biolink typing, and the matched-concept endpoints behind the query → matched concept → answer chain. Grouped targets are decomposed to their component genes.",
    kind: "function",
    getData: (r) =>
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
    label: "PubTator enrichment",
    description: "For each edge, ask PubTator whether the supporting PMIDs independently co-mention both endpoints. Adds an external verification signal.",
    kind: "service",
    getData: (r) => ({
      call_summary: r.answer_graph_view?.pubtator_call_summary ?? null,
      metrics: r.answer_graph_view?.pubtator_metrics ?? null,
    }),
  },
  {
    number: "15",
    label: "Explanation",
    description: "LLM writes the structured Markdown summary you see above, under faithfulness guards: cite every claim, keep to each edge's predicate, and never invent an entity type (such as a 'complex') the data doesn't assert.",
    kind: "LLM",
    promptKey: "stage_15_explain",
  },
];

export function ResultPanel({ question, model, r }: Props) {
  return (
    <motion.section
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2 }}
      className="flex flex-col gap-5"
    >
      <ActionBar question={question} model={model} r={r} />
      <HeroMeta r={r} />
      {/* primary answer view:
            - if we have both the structured graph view AND the LLM's
              markdown explanation, render StructuredAnswer — it parses
              the markdown into 4 sections (Answer / Evidence / Confidence
              / Limitations) and replaces the Evidence bullet list with
              one mini-graph card PER picked answer entity (showing the
              pinned→edge→answer node-link with the PloverDB edge IDs).
            - otherwise (out_of_scope, low_confidence_resolution, no
              picks, etc.) fall back to the prose-only MarkdownAnswer
              so the user still sees a result. */}
      {r.answer_graph_view && r.answer_graph_view.edges.length > 0 && r.explanation ? (
        <StructuredAnswer view={r.answer_graph_view} explanation={r.explanation} />
      ) : (
        r.explanation && <MarkdownAnswer text={r.explanation} />
      )}
      <TrapiCard query={r.intermediates.trapi_query} />
      <StageList r={r} />
      <RawArtifacts r={r} />
    </motion.section>
  );
}

function ActionBar({ question, model, r }: Props) {
  return (
    <div className="flex items-center justify-between gap-3 flex-wrap">
      <h2 className="text-lg font-semibold tracking-tight">Result</h2>
      <div className="flex gap-2">
        <ExportButton onClick={() => downloadResultPDF(question, model, r)}>
          <DownloadIcon /> Export · PDF
        </ExportButton>
        <ExportButton onClick={() => downloadResultMarkdown(question, model, r)}>
          <DownloadIcon /> Export · Markdown
        </ExportButton>
        <ExportButton onClick={() => downloadResultJSON(question, model, r)}>
          <DownloadIcon /> Export · JSON
        </ExportButton>
      </div>
    </div>
  );
}

function ExportButton({ children, onClick }: { children: React.ReactNode; onClick: () => void }) {
  return (
    <motion.button
      whileHover={{ y: -1 }}
      whileTap={{ scale: 0.97 }}
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 hover:bg-zinc-100 dark:hover:bg-zinc-800 px-3 py-1.5 text-xs font-medium"
    >
      {children}
    </motion.button>
  );
}

function HeroMeta({ r }: { r: QueryResponse }) {
  // "out_of_scope" is a deliberate refusal, not a failure — show it
  // as info (amber), not error (red), and not success (emerald).
  const tone: "ok" | "info" | "failed" =
    r.outcome === "out_of_scope" ? "info" : r.success ? "ok" : "failed";
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-gradient-to-br from-zinc-50 to-white dark:from-zinc-900 dark:to-zinc-950 p-4">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
        <StatusPill tone={tone} text={r.outcome ?? (r.success ? "ok" : "failed")} />
        <MetaCell label="run" value={r.run_id} mono />
        <MetaCell label="cost" value={`$${r.cost_usd.toFixed(6)}`} mono />
        <MetaCell label="elapsed" value={`${r.elapsed_s.toFixed(2)}s`} mono />
      </div>
    </div>
  );
}

function MetaCell({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <span className="text-zinc-600 dark:text-zinc-400">
      <span className="text-zinc-500 mr-1.5">{label}</span>
      <span className={mono ? "font-mono text-zinc-800 dark:text-zinc-200" : "text-zinc-800 dark:text-zinc-200"}>{value}</span>
    </span>
  );
}

function StatusPill({ tone, text }: { tone: "ok" | "info" | "failed"; text: string }) {
  const cls =
    tone === "ok"
      ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300"
      : tone === "info"
        ? "bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
        : "bg-red-100 text-red-800 dark:bg-red-950/60 dark:text-red-300";
  return (
    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${cls}`}>
      {text}
    </span>
  );
}

function TrapiCard({ query }: { query: unknown }) {
  if (!query) return null;
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.05, duration: 0.2 }}
      className="rounded-lg border border-blue-200 dark:border-blue-900/60 bg-blue-50/40 dark:bg-blue-950/20 overflow-hidden"
    >
      <div className="flex items-center justify-between gap-2 px-4 py-3 border-b border-blue-200 dark:border-blue-900/60">
        <div>
          <h3 className="text-sm font-semibold tracking-tight">TRAPI query graph</h3>
          <p className="text-xs text-zinc-600 dark:text-zinc-400 mt-0.5">
            <span className="font-medium text-zinc-800 dark:text-zinc-200">Constructed by the LLM</span>{" "}
            from your natural-language question, validated against TRAPI 1.5 + Biolink, and POSTed to PloverDB.
          </p>
        </div>
        <PipelineBadge>JSON</PipelineBadge>
      </div>
      <div className="p-3">
        <JsonView value={query} />
      </div>
    </motion.div>
  );
}

function StageList({ r }: { r: QueryResponse }) {
  // resolve prompts blob once so the per-row lookup is a single read.
  const prompts = (r.intermediates.prompts as Record<string, StagePromptEntry> | null) ?? null;
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.1, duration: 0.2 }}
      className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 overflow-hidden"
    >
      <div className="flex items-start justify-between gap-2 px-4 py-3 border-b border-zinc-200 dark:border-zinc-800">
        <div>
          <h3 className="text-sm font-semibold tracking-tight">Pipeline stages</h3>
          <p className="text-xs text-zinc-600 dark:text-zinc-400 mt-0.5">
            All 15 stages, in order. LLM rows show prompts + model output;
            service rows show the intermediate artifact; function rows are
            pure-local steps with no separate artifact. Rows that did not run
            (pipeline stopped earlier) are dimmed.
          </p>
        </div>
        <div className="hidden md:flex items-center gap-1.5 shrink-0">
          <KindChip kind="LLM" />
          <KindChip kind="service" />
          <KindChip kind="function" />
        </div>
      </div>
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
    </motion.div>
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
    <li className={ran ? "" : "opacity-60"}>
      <details className="group">
        <summary className="cursor-pointer list-none px-4 py-3 flex items-start gap-3 hover:bg-zinc-50 dark:hover:bg-zinc-900/40">
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
    <div className="px-4 pb-4 pt-1 flex flex-col gap-3">
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

function RawArtifacts({ r }: { r: QueryResponse }) {
  const items: Array<[string, unknown]> = [
    ["nameres", r.intermediates.nameres],
    ["candidate_probes", r.intermediates.candidate_probes],
    ["nodenorm", r.intermediates.nodenorm],
    ["predicate_probe", r.intermediates.predicate_probe],
    ["validation", r.intermediates.validation],
    ["plover_request", r.intermediates.plover_request],
    ["plover_response_summary", r.intermediates.plover_response_summary],
    ["answer", r.answer],
    ["cost", r.intermediates.cost],
  ];
  return (
    <motion.details
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ delay: 0.15 }}
      className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50/60 dark:bg-zinc-900/40"
    >
      <summary className="cursor-pointer px-4 py-3 text-sm font-medium select-none">
        Raw pipeline artifacts
      </summary>
      <div className="flex flex-col gap-3 p-4">
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
    </motion.details>
  );
}

function PipelineBadge({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-[10px] uppercase tracking-wider font-medium px-2 py-1 rounded bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900">
      {children}
    </span>
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

function DownloadIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
      <path
        fillRule="evenodd"
        d="M3 17a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm6.293-13.293A1 1 0 0110 4v6.586l2.293-2.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 111.414-1.414L9 10.586V4a1 1 0 01.293-.707z"
        clipRule="evenodd"
      />
    </svg>
  );
}
