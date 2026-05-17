// download the current result as JSON, Markdown, or PDF. all
// client-side — the user can re-open the JSON file later to inspect a
// past run without the backend running. the Markdown and PDF exports
// are shareable thesis-grade summaries covering every Stage in the
// new 1-15 pipeline + the candidate / predicate density probes + the
// answer-graph view + per-edge provenance.
//
// the three formats are layered:
//   JSON     — full machine-readable dump (envelope + full QueryResponse)
//   Markdown — thesis-grade human-readable transcript of every stage,
//              the picked answers, evidence, and where to look in the JSON
//              for raw artifacts. opens cleanly in Obsidian / GitHub / VS Code.
//   PDF      — same content as Markdown, rendered as a self-contained
//              HTML document in a new tab and handed to the browser's
//              print engine. user picks "Save as PDF" in the print
//              dialog. no JS PDF library required, no extra bundle.

import { marked } from "marked";
import type {
  AnswerGraphEdge,
  AnswerGraphView,
  QueryResponse,
  StagePromptEntry,
} from "./api";
import { linkifyCitations, linkifyCURIEs } from "./linkify";

const EXPORT_VERSION = "v15";

// marked options: GitHub-flavoured markdown (tables, strikethrough,
// task lists), break-on-single-newline OFF so blank-line-separated
// paragraphs render the way the LLM intended.
marked.setOptions({
  gfm: true,
  breaks: false,
});

// ---------- entry points ----------

export function downloadResultJSON(
  question: string,
  model: string,
  r: QueryResponse,
): void {
  const filename = safeFileBase(r, "json");
  // we wrap the QueryResponse in an envelope so importers see the
  // question + model context that isn't part of the response itself.
  // version field lets future readers detect schema drift.
  const payload = {
    ploverai_export_version: EXPORT_VERSION,
    exported_at: new Date().toISOString(),
    question,
    model,
    result: r,
  };
  downloadBlob(
    JSON.stringify(payload, null, 2),
    filename,
    "application/json",
  );
}

export function downloadResultMarkdown(
  question: string,
  model: string,
  r: QueryResponse,
): void {
  const filename = safeFileBase(r, "md");
  downloadBlob(buildMarkdown(question, model, r), filename, "text/markdown");
}

export function downloadResultPDF(
  question: string,
  model: string,
  r: QueryResponse,
): void {
  // open a fresh tab with a self-contained printable HTML document,
  // then trigger the browser's print dialog. user picks "Save as PDF"
  // in that dialog. no third-party PDF library, no extra bundle, and
  // the browser's print rendering is high-quality and consistent.
  //
  // we capture the live in-page mini-graph SVGs FIRST (before the new
  // tab opens) because the SVGs only exist while the ResultPanel is
  // mounted in this tab's DOM. once captured, the new tab is fully
  // self-contained and works even if the parent tab navigates away.
  const capturedSVGs = captureMiniGraphSVGs(r.answer_graph_view);
  const html = buildPrintHTML(question, model, r, capturedSVGs);
  const w = window.open("", "_blank");
  if (!w) {
    // most likely a pop-up blocker. fall back to Markdown so the user
    // at least gets something exportable.
    alert(
      "Pop-up blocked — could not open the print tab. " +
        "Allow pop-ups for this page, or use Export · Markdown.",
    );
    return;
  }
  w.document.open();
  w.document.write(html);
  w.document.close();
  // we do NOT auto-trigger print() here. some browsers race the
  // window.print call against the async style/layout pass and end up
  // printing a half-rendered page. the printable HTML embeds a fixed
  // "Print / Save as PDF" button at the top-right (.print-btn,
  // hidden in actual print output via .no-print) so the user clicks
  // it when ready. as a bonus this lets them eyeball the export
  // before saving — fewer wasted PDF files.
  w.focus();
}

// pull every mini-graph SVG out of the live DOM and tag each one with
// the answer entity it depicts. relies on the aria-label that
// StructuredAnswer.MiniGraph sets ("Mini graph: <pinned> to <answer>")
// to map an SVG back to its answer node. when the page isn't showing
// a StructuredAnswer (no graph view, or only MarkdownAnswer fallback),
// this returns an empty array and the PDF falls back to the textual
// graph summary.
type CapturedSVG = { answerCurie: string; svg: string; label: string };

function captureMiniGraphSVGs(
  view: AnswerGraphView | null,
): CapturedSVG[] {
  if (!view || typeof document === "undefined") return [];
  const labelByCurie = new Map<string, string>();
  view.answer_nodes.forEach((n) => labelByCurie.set(n.curie, n.label || n.curie));

  // scope to the result section so we never pick up favicons or
  // unrelated icon SVGs. ResultPanel renders as <section>; the
  // mini-graphs each set role="img".
  const nodes = document.querySelectorAll('section svg[role="img"]');
  const out: CapturedSVG[] = [];
  const seen = new Set<string>();
  for (const svg of Array.from(nodes)) {
    const aria = svg.getAttribute("aria-label") || "";
    const m = aria.match(/Mini graph:.*to (.+)$/);
    if (!m) continue;
    const answerCurie = m[1].trim();
    if (seen.has(answerCurie)) continue;
    seen.add(answerCurie);
    const clone = svg.cloneNode(true) as SVGElement;
    if (!clone.getAttribute("xmlns")) {
      clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    }
    out.push({
      answerCurie,
      svg: new XMLSerializer().serializeToString(clone),
      label: labelByCurie.get(answerCurie) || answerCurie,
    });
  }
  return out;
}

// ---------- markdown builder ----------

function buildMarkdown(
  question: string,
  model: string,
  r: QueryResponse,
): string {
  const lines: string[] = [];

  // YAML frontmatter — works in Obsidian, GitHub, jekyll, etc.
  lines.push("---");
  lines.push(`ploverai_export_version: ${EXPORT_VERSION}`);
  lines.push(`run_id: ${r.run_id}`);
  lines.push(`model: ${model}`);
  lines.push(`question: ${jsonString(question)}`);
  lines.push(`outcome: ${r.outcome ?? (r.success ? "ok" : "failed")}`);
  lines.push(`success: ${r.success}`);
  lines.push(`cost_usd: ${r.cost_usd}`);
  lines.push(`elapsed_s: ${r.elapsed_s}`);
  const plover = r.intermediates.plover_response_summary;
  if (plover) {
    lines.push(`plover_n_results: ${plover.n_results}`);
    lines.push(`plover_n_nodes: ${plover.n_nodes}`);
    lines.push(`plover_n_edges: ${plover.n_edges}`);
  }
  lines.push(`exported_at: ${new Date().toISOString()}`);
  lines.push("---");
  lines.push("");

  // header + answer prose. linkify PMIDs and CURIEs so an MD viewer
  // (Obsidian / GitHub / VS Code) shows them as clickable links
  // instead of literal "[PMID:N]" / "MONDO:0009061" text.
  lines.push(`# ${question}`);
  lines.push("");
  lines.push("## Answer");
  lines.push("");
  lines.push(
    r.explanation
      ? linkifyCURIEs(linkifyCitations(r.explanation))
      : "_(no explanation produced)_",
  );
  lines.push("");

  // answer-graph summary
  if (r.answer_graph_view) {
    lines.push("## Answer graph");
    lines.push("");
    lines.push(...graphSummaryMarkdown(r.answer_graph_view));
    lines.push("");
  }

  // pipeline stages — overview table + per-stage detail blocks.
  // the detail blocks mirror what's EXPANDABLE in the UI's StageList
  // (system+user prompts for LLM stages, intermediate JSON for service
  // stages, descriptions + summary data for function stages). file
  // grows to ~50-200 KB on a typical run because the prompts are long,
  // but the user explicitly asked for full-transcript fidelity so the
  // MD export can serve as a thesis appendix.
  lines.push("## Pipeline stages");
  lines.push("");
  lines.push(...pipelineSummaryMarkdown(r));
  lines.push("");

  lines.push("## Pipeline detail (per stage)");
  lines.push("");
  lines.push(
    "Each stage below mirrors what's expandable in the result page's " +
      "Pipeline-stages card. LLM stages include the system + user prompts " +
      "we sent and the model's reasoning trace (when returned). Service " +
      "stages include the raw intermediate artifact (also in the JSON " +
      "export). Function stages include a short description and any " +
      "summary data the stage produced.",
  );
  lines.push("");
  lines.push(...pipelineDetailMarkdown(r));
  lines.push("");

  // density probes
  const candidateProbes = r.intermediates.candidate_probes as
    | Record<string, unknown>
    | null
    | undefined;
  if (candidateProbes && Object.keys(candidateProbes).length > 0) {
    lines.push("## Candidate-density probe");
    lines.push("");
    lines.push(...candidateProbesMarkdown(candidateProbes));
    lines.push("");
  }

  const predicateProbe = r.intermediates.predicate_probe as
    | Record<string, unknown>
    | null
    | undefined;
  if (predicateProbe) {
    lines.push("## Predicate-density probe (chosen CURIE)");
    lines.push("");
    lines.push(...predicateProbeMarkdown(predicateProbe));
    lines.push("");
  }

  // TRAPI query
  if (r.intermediates.trapi_query) {
    lines.push("## TRAPI query (LLM-constructed, Stage 8)");
    lines.push("");
    lines.push("```json");
    lines.push(JSON.stringify(r.intermediates.trapi_query, null, 2));
    lines.push("```");
    lines.push("");
  }

  // pointer to JSON for full data
  lines.push("## Raw artifacts");
  lines.push("");
  lines.push(
    "Per-stage prompts, NameRes/NodeNorm raw responses, PloverDB " +
      "request/response, PubTator NER verification, and the full LLM " +
      "reasoning trace are in the companion JSON export for this run " +
      `(\`${safeFileBase(r, "json")}\`). The on-disk pipeline artifacts ` +
      "live under `pipeline/code/outputs/RUN_<timestamp>/` on the " +
      "server.",
  );
  lines.push("");

  return lines.join("\n");
}

function graphSummaryMarkdown(g: AnswerGraphView): string[] {
  const lines: string[] = [];
  const pin = g.pinned_node;
  lines.push(
    `**Pinned:** ${pin.label || "(no label)"} (\`${pin.curie}\`)` +
      (pin.category ? ` — ${pin.category.replace(/^biolink:/, "")}` : ""),
  );
  lines.push("");
  if (g.pubtator_metrics) {
    const m = g.pubtator_metrics;
    const rate =
      m.rate != null ? `${Math.round(m.rate * 100)}%` : "—";
    lines.push(
      `**PubTator verification:** ${m.verified}/${m.verified + m.unverified} ` +
        `edges verified` +
        (m.not_applicable > 0 ? ` (${m.not_applicable} n/a)` : "") +
        ` · rate ${rate}`,
    );
    lines.push("");
  }
  if (g.answer_nodes.length === 0) {
    lines.push("_(no answer entities returned)_");
    return lines;
  }
  lines.push(
    `**Picked answers (${g.answer_nodes.length}):**`,
  );
  lines.push("");
  lines.push(
    "| # | Label | CURIE | Category | Edges to pinned | PubTator |",
  );
  lines.push(
    "|---|---|---|---|---|---|",
  );
  g.answer_nodes.forEach((a, i) => {
    const edges = g.edges.filter(
      (e) => e.source === a.curie || e.target === a.curie,
    );
    const verifiedBadge = pubtatorBadge(edges);
    lines.push(
      `| ${i + 1} | ${escapeCell(a.label || a.curie)} | \`${a.curie}\` | ` +
        `${(a.category || "").replace(/^biolink:/, "")} | ${edges.length} | ` +
        `${verifiedBadge} |`,
    );
  });
  lines.push("");

  // per-edge provenance: predicate + PMIDs + KG source
  lines.push("### Edge provenance");
  lines.push("");
  g.edges.forEach((e, i) => {
    const pred = (e.predicate || "?").replace(/^biolink:/, "");
    const lvl = e.knowledge_level || "—";
    const ks = e.primary_knowledge_source || "—";
    lines.push(
      `**Edge ${i + 1}** (\`PloverDB-edge:${e.id}\`): ` +
        `\`${e.source}\` — ${pred} → \`${e.target}\``,
    );
    lines.push(`  - knowledge_level: ${lvl}`);
    lines.push(`  - primary_knowledge_source: ${ks}`);
    if (e.supporting_publications.length > 0) {
      const pmidLinks = e.supporting_publications
        .map(
          (p) =>
            `[${p}](https://pubmed.ncbi.nlm.nih.gov/${p.replace(/^PMID:/, "")}/)`,
        )
        .join(", ");
      lines.push(`  - supporting_publications: ${pmidLinks}`);
    }
    if (e.pubtator_verified) {
      const v = e.pubtator_verified;
      const status = v.verified ? "✓ co-mention found" : "✗ no co-mention";
      lines.push(
        `  - pubtator: ${status} ` +
          `(${v.co_mention_pmids.length} co, ${v.subject_only_pmids.length} subj-only, ` +
          `${v.object_only_pmids.length} obj-only, ${v.missing_pmids.length} not-indexed)`,
      );
    }
    lines.push("");
  });
  return lines;
}

function pubtatorBadge(edges: AnswerGraphEdge[]): string {
  if (edges.length === 0) return "—";
  const hasVerified = edges.some((e) => e.pubtator_verified?.verified);
  if (hasVerified) return "✓ verified";
  const hasChecked = edges.some((e) => e.pubtator_verified !== null);
  if (hasChecked) return "✗ no co-mention";
  return "n/a";
}

function pipelineSummaryMarkdown(r: QueryResponse): string[] {
  const prompts = (r.intermediates.prompts as
    | Record<string, StagePromptEntry>
    | null
    | undefined) ?? {};
  // The 15-stage ladder mirrors STAGE_ORDER in ResultPanel.tsx but
  // we inline it here to keep export.ts self-contained.
  const stages: Array<{
    n: string;
    kind: "LLM" | "service" | "function";
    label: string;
    promptKey?: string;
    note?: string;
  }> = [
    { n: "1", kind: "LLM", label: "Scope check", promptKey: "stage_1_scope_check" },
    { n: "2", kind: "LLM", label: "Entity extract", promptKey: "stage_2_entity_extract" },
    { n: "3", kind: "service", label: "NameRes lookup" },
    { n: "4", kind: "LLM", label: "Candidate pick", promptKey: "stage_4_candidate_pick" },
    { n: "5", kind: "function", label: "IC re-rank" },
    { n: "6", kind: "service", label: "NodeNorm canonicalize pinned" },
    { n: "7", kind: "function", label: "Consistency check" },
    { n: "8", kind: "LLM", label: "TRAPI build", promptKey: "stage_8_trapi_build" },
    { n: "9", kind: "function", label: "Validation" },
    { n: "10", kind: "service", label: "PloverDB query" },
    { n: "11", kind: "LLM", label: "Answer pick", promptKey: "stage_11_answer_pick" },
    { n: "12", kind: "service", label: "NodeNorm canonicalize answers" },
    { n: "13", kind: "function", label: "Build graph view" },
    { n: "14", kind: "service", label: "PubTator enrichment" },
    { n: "15", kind: "LLM", label: "Explanation", promptKey: "stage_15_explain" },
  ];

  const lines: string[] = [];
  lines.push("| # | Kind | Stage | Status | In tok | Out tok | Latency |");
  lines.push("|---|---|---|---|---|---|---|");
  stages.forEach((s) => {
    let status = "—";
    let inTok = "—";
    let outTok = "—";
    let lat = "—";
    if (s.promptKey && prompts[s.promptKey]) {
      const e = prompts[s.promptKey];
      const resp = e.response;
      if (resp?.input_tokens !== undefined) {
        inTok = String(resp.input_tokens);
        outTok = String(resp.output_tokens ?? "—");
        lat = resp.latency_s != null ? `${resp.latency_s.toFixed(2)}s` : "—";
        status = "✓ ran";
      } else {
        status = "ran (no token meta)";
      }
    } else if (s.kind !== "LLM") {
      // non-LLM stages don't have prompt entries — infer status from
      // the relevant intermediate artifact.
      status = inferServiceStatus(s.n, r);
    }
    lines.push(
      `| ${s.n} | ${s.kind} | ${s.label} | ${status} | ${inTok} | ${outTok} | ${lat} |`,
    );
  });
  return lines;
}

function inferServiceStatus(stageNumber: string, r: QueryResponse): string {
  // map non-LLM stages to the intermediate field that would have been
  // written if they ran. presence of a non-null value → "ran".
  const im = r.intermediates;
  switch (stageNumber) {
    case "3":
      return im.nameres ? "✓ ran" : "did not run";
    case "5":
      return "(function)";
    case "6": {
      const n = im.nodenorm as { pinned?: unknown } | null | undefined;
      return n?.pinned ? "✓ ran" : "did not run";
    }
    case "7":
      return "(function)";
    case "9":
      return im.validation ? "✓ ran" : "did not run";
    case "10":
      return im.plover_response_summary ? "✓ ran" : "did not run";
    case "12": {
      const n = im.nodenorm as { answers?: unknown } | null | undefined;
      return n?.answers ? "✓ ran" : "did not run";
    }
    case "13":
      return r.answer_graph_view ? "✓ ran" : "did not run";
    case "14": {
      const v = r.answer_graph_view?.pubtator_call_summary;
      return v?.called ? "✓ ran" : "did not run";
    }
    default:
      return "—";
  }
}

// per-stage detail block: mirrors the expand-to-show-details panel
// the screen-side StageList renders for each stage. one block per
// stage in 1..15 order. LLM stages dump system + user prompts,
// reasoning trace, finish_reason, and model_returned. service stages
// dump the raw intermediate JSON (also in the JSON export — duplicated
// here so the MD export stands alone as a thesis appendix). function
// stages dump a one-paragraph explanation + (when applicable) summary
// data the stage produced.
//
// long content (prompts, intermediates) goes inside <details>...</details>
// HTML blocks. Obsidian + GitHub + most modern MD viewers render those
// as collapsible sections; plain text editors just show them unfolded,
// which is acceptable since the user explicitly asked for full
// transcript fidelity.
function pipelineDetailMarkdown(r: QueryResponse): string[] {
  const prompts = (r.intermediates.prompts as
    | Record<string, StagePromptEntry>
    | null
    | undefined) ?? {};
  const lines: string[] = [];
  const stages = stageDescriptors();

  for (const s of stages) {
    lines.push(`### Stage ${s.n} — ${s.label} (${s.kind})`);
    lines.push("");

    if (s.kind === "LLM") {
      const entry = s.promptKey ? prompts[s.promptKey] : undefined;
      if (!entry) {
        lines.push("**Status:** did not run (pipeline stopped before reaching this stage).");
        lines.push("");
        continue;
      }
      // header line with token counts + finish_reason + model_returned
      const resp = entry.response;
      const parts: string[] = ["✓ ran"];
      if (resp?.input_tokens !== undefined) {
        parts.push(`in=${resp.input_tokens}tok`);
      }
      if (resp?.output_tokens !== undefined) {
        parts.push(`out=${resp.output_tokens}tok`);
      }
      if (resp?.latency_s != null) parts.push(`${resp.latency_s.toFixed(2)}s`);
      if (resp?.finish_reason) parts.push(`finish_reason: ${resp.finish_reason}`);
      if (resp?.model_returned) parts.push(`model: ${resp.model_returned}`);
      lines.push(`**Status:** ${parts.join(" · ")}`);
      lines.push("");

      // for Stage 4 + Stage 8 the LLM also saw probe data injected
      // into the user prompt. point the reader to the standalone
      // probe sections rather than duplicate the table here.
      if (s.n === "4") {
        lines.push(
          "_Probe data shown to the LLM in this stage: see § " +
            "Candidate-density probe below._",
        );
        lines.push("");
      }
      if (s.n === "8") {
        lines.push(
          "_Probe data shown to the LLM in this stage: see § " +
            "Predicate-density probe (chosen CURIE) below._",
        );
        lines.push("");
      }

      if (entry.system) {
        lines.push("<details>");
        lines.push("<summary>System prompt</summary>");
        lines.push("");
        lines.push("```");
        lines.push(entry.system);
        lines.push("```");
        lines.push("");
        lines.push("</details>");
        lines.push("");
      }
      const userText = entry.user ?? entry.user_truncated;
      if (userText) {
        const t = entry.user_truncated ? "User prompt (truncated)" : "User prompt";
        lines.push("<details>");
        lines.push(`<summary>${t}</summary>`);
        lines.push("");
        lines.push("```");
        lines.push(userText);
        lines.push("```");
        lines.push("");
        lines.push("</details>");
        lines.push("");
      }
      if (resp?.reasoning) {
        lines.push("<details>");
        lines.push("<summary>Reasoning</summary>");
        lines.push("");
        lines.push("```");
        lines.push(resp.reasoning);
        lines.push("```");
        lines.push("");
        lines.push("</details>");
        lines.push("");
      }
      continue;
    }

    if (s.kind === "service") {
      const data = s.getData?.(r);
      if (data == null || (typeof data === "object" && Object.keys(data as object).length === 0)) {
        lines.push("**Status:** did not run (no intermediate artifact on disk).");
        lines.push("");
        continue;
      }
      lines.push("**Status:** ✓ ran");
      lines.push("");
      if (s.description) {
        lines.push(s.description);
        lines.push("");
      }
      lines.push("<details>");
      lines.push("<summary>Intermediate artifact</summary>");
      lines.push("");
      lines.push("```json");
      lines.push(JSON.stringify(data, null, 2));
      lines.push("```");
      lines.push("");
      lines.push("</details>");
      lines.push("");
      continue;
    }

    // function stage
    if (s.details) {
      // longer explanation for stages 5 and 7 that have no separate
      // artifact — matches the "What this stage does" block in the UI.
      lines.push("**Status:** pure local computation (no separate artifact).");
      lines.push("");
      lines.push(s.details);
      lines.push("");
      continue;
    }
    const data = s.getData?.(r);
    if (data == null) {
      lines.push("**Status:** did not run.");
      lines.push("");
      continue;
    }
    lines.push("**Status:** ✓ ran");
    lines.push("");
    if (s.description) {
      lines.push(s.description);
      lines.push("");
    }
    lines.push("<details>");
    lines.push("<summary>Summary</summary>");
    lines.push("");
    lines.push("```json");
    lines.push(JSON.stringify(data, null, 2));
    lines.push("```");
    lines.push("");
    lines.push("</details>");
    lines.push("");
  }

  // cost ledger: one row per LLM call. always present when at least
  // one LLM stage ran, since each stage appends to cost.json.
  const cost = r.intermediates.cost as
    | { entries?: Array<Record<string, unknown>> }
    | null
    | undefined;
  if (cost?.entries && cost.entries.length > 0) {
    lines.push("### Cost ledger");
    lines.push("");
    lines.push(
      "Per-LLM-call costs. Sum matches the run total in the frontmatter " +
        "(`cost_usd`) plus rounding.",
    );
    lines.push("");
    lines.push(
      "| Stage | Model | In tok | Out tok | In $ | Out $ | Total $ | Latency |",
    );
    lines.push(
      "|---|---|---|---|---|---|---|---|",
    );
    cost.entries.forEach((e) => {
      const inUsd = ((e.input_usd as number) ?? 0).toFixed(6);
      const outUsd = ((e.output_usd as number) ?? 0).toFixed(6);
      const totUsd = ((e.total_usd as number) ?? 0).toFixed(6);
      const lat = e.latency_s != null ? `${(e.latency_s as number).toFixed(2)}s` : "—";
      lines.push(
        `| ${e.stage ?? "—"} | ${escapeCell(String(e.model_slug ?? e.model_id ?? "—"))} | ` +
          `${e.input_tokens ?? "—"} | ${e.output_tokens ?? "—"} | ` +
          `$${inUsd} | $${outUsd} | $${totUsd} | ${lat} |`,
      );
    });
    lines.push("");
  }

  return lines;
}

// single source of truth for stage metadata — used by the summary
// table, the detail blocks, and (mirrored) by the HTML print builder.
// kept verbose so each entry is self-documenting; the screen-side
// equivalent lives at STAGE_ORDER in ResultPanel.tsx.
type StageDescriptor = {
  n: string;
  kind: "LLM" | "service" | "function";
  label: string;
  description?: string;
  details?: string;
  promptKey?: string;
  getData?: (r: QueryResponse) => unknown;
};

function stageDescriptors(): StageDescriptor[] {
  return [
    {
      n: "1",
      kind: "LLM",
      label: "Scope check",
      description:
        "Guardrail: LLM decides whether the input is a biomedical question worth running.",
      promptKey: "stage_1_scope_check",
    },
    {
      n: "2",
      kind: "LLM",
      label: "Entity extract",
      description:
        "LLM picks the focal biomedical entity name from the question.",
      promptKey: "stage_2_entity_extract",
    },
    {
      n: "3",
      kind: "service",
      label: "NameRes lookup",
      description:
        "RENCI Name Resolution: free-text entity name → ranked CURIE candidates via BM25.",
      getData: (r) => r.intermediates.nameres,
    },
    {
      n: "4",
      kind: "LLM",
      label: "Candidate pick",
      description:
        "LLM picks the best NameRes candidate (or declares 'no match') to defend against typos and label-type collisions.",
      promptKey: "stage_4_candidate_pick",
    },
    {
      n: "5",
      kind: "function",
      label: "IC re-rank",
      details:
        "NodeNorm reports an information_content score for every CURIE — low values mean broad concepts (e.g. \"Cancer\"), high values mean niche ones (e.g. \"small-cell lung carcinoma stage IIIB\"). When the extractor (Stage 2) labels the question as asking for a GENERAL concept, this step re-sorts the top-K NameRes candidates by ascending IC so the broadest match wins. For questions tagged as SPECIFIC, the original BM25 ranking from NameRes is kept untouched.",
    },
    {
      n: "6",
      kind: "service",
      label: "NodeNorm canonicalize pinned",
      description:
        "RENCI Node Normalization: resolve the pinned CURIE to its canonical form + Biolink categories.",
      getData: (r) =>
        (r.intermediates.nodenorm as Record<string, unknown> | null | undefined)?.pinned ?? null,
    },
    {
      n: "7",
      kind: "function",
      label: "Consistency check",
      details:
        "Compares the user's free-text mention against the canonical label that NameRes + NodeNorm resolved to, using the max of difflib's SequenceMatcher.ratio (character-level similarity) and a substring-containment check. If the resulting score falls below 0.50, the run aborts with status low_confidence_resolution rather than firing a TRAPI query against a probably-wrong entity.",
    },
    {
      n: "8",
      kind: "LLM",
      label: "TRAPI build",
      description:
        "LLM constructs the one-hop TRAPI query graph from the pinned entity. A per-CURIE predicate-density probe runs first and is injected into the LLM's prompt so it can prefer populated predicates over plausible-but-empty ones.",
      promptKey: "stage_8_trapi_build",
    },
    {
      n: "9",
      kind: "function",
      label: "Validation",
      description:
        "reasoner-validator gate: TRAPI schema + Biolink check. Invalid → pipeline stops without hitting PloverDB.",
      getData: (r) => r.intermediates.validation,
    },
    {
      n: "10",
      kind: "service",
      label: "PloverDB query",
      description:
        "POST the validated TRAPI query to kg2cploverdb.ci.transltr.io/query and parse the response.",
      getData: (r) => ({
        request: r.intermediates.plover_request,
        response_summary: r.intermediates.plover_response_summary,
      }),
    },
    {
      n: "11",
      kind: "LLM",
      label: "Answer pick",
      description:
        "LLM selects the answer entities from the PloverDB response, ranked by evidence tier.",
      promptKey: "stage_11_answer_pick",
    },
    {
      n: "12",
      kind: "service",
      label: "NodeNorm canonicalize answers",
      description:
        "Canonicalize every CURIE the LLM picked + collect equivalents for downstream scoring.",
      getData: (r) =>
        (r.intermediates.nodenorm as Record<string, unknown> | null | undefined)?.answers ?? null,
    },
    {
      n: "13",
      kind: "function",
      label: "Build graph view",
      description:
        "Reshape (pinned entity + picked answers + KG slice) into the node-link graph view the UI renders, with per-edge provenance.",
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
      n: "14",
      kind: "service",
      label: "PubTator enrichment",
      description:
        "For each edge, ask PubTator whether the supporting PMIDs independently co-mention both endpoints. Adds an external verification signal.",
      getData: (r) => ({
        call_summary: r.answer_graph_view?.pubtator_call_summary ?? null,
        metrics: r.answer_graph_view?.pubtator_metrics ?? null,
      }),
    },
    {
      n: "15",
      kind: "LLM",
      label: "Explanation",
      description:
        "LLM writes the structured Markdown summary you see at the top of the result page.",
      promptKey: "stage_15_explain",
    },
  ];
}

type ProbeEntry = {
  total_edges?: number;
  by_predicate?: Record<string, { count?: number; forward?: number; reverse?: number }>;
  error?: string | null;
};

function candidateProbesMarkdown(
  data: Record<string, unknown>,
): string[] {
  const lines: string[] = [];
  const answerCat = (data.answer_cat as string) || "?";
  const filter = data.filter_applied as string[] | undefined;
  const fallback = data.fallback_to_loose === true;
  lines.push(
    `Per-candidate edge-density probe against \`${answerCat}\` ` +
      `(filter: ${filter ? `\`${JSON.stringify(filter)}\`` : "none"}` +
      `${fallback ? " — loose fallback was triggered" : ""}).`,
  );
  lines.push("");
  const byCurie = (data.by_curie as Record<string, ProbeEntry>) ?? {};
  const rows = Object.entries(byCurie).sort(
    ([, a], [, b]) =>
      (b.total_edges ?? 0) - (a.total_edges ?? 0),
  );
  if (rows.length === 0) {
    lines.push("_(no probe rows)_");
    return lines;
  }
  lines.push("| CURIE | KG2c edges | Note |");
  lines.push("|---|---|---|");
  rows.forEach(([curie, p]) => {
    const note = p.error
      ? `error: ${escapeCell(p.error)}`
      : (p.total_edges ?? 0) === 0
        ? "no edges"
        : "ok";
    lines.push(
      `| \`${curie}\` | ${p.total_edges ?? 0} | ${note} |`,
    );
  });
  return lines;
}

function predicateProbeMarkdown(p: Record<string, unknown>): string[] {
  const lines: string[] = [];
  const curie = (p.pinned_curie as string) || "?";
  const answerCat = (p.answer_cat as string) || "?";
  const total = (p.total_edges as number) ?? 0;
  lines.push(
    `Pinned CURIE \`${curie}\` against \`${answerCat}\` — ` +
      `${total} total edges.`,
  );
  lines.push("");
  const byPred = (p.by_predicate as Record<string, { count?: number; forward?: number; reverse?: number }>) || {};
  const rows = Object.entries(byPred).sort(
    ([, a], [, b]) => (b.count ?? 0) - (a.count ?? 0),
  );
  if (rows.length === 0) {
    lines.push("_(no populated predicates — see fallback diagnostics in JSON)_");
    return lines;
  }
  lines.push("| Predicate | Count | Forward | Reverse |");
  lines.push("|---|---|---|---|");
  rows.forEach(([pred, s]) => {
    lines.push(
      `| \`${pred}\` | ${s.count ?? 0} | ${s.forward ?? 0} | ${s.reverse ?? 0} |`,
    );
  });
  return lines;
}

// ---------- HTML (PDF) builder ----------

function buildPrintHTML(
  question: string,
  model: string,
  r: QueryResponse,
  capturedSVGs: CapturedSVG[] = [],
): string {
  // self-contained HTML: inline CSS, no external assets. designed for
  // the browser's print engine — letter/A4 paper, B&W friendly,
  // section-level page breaks, monospace for CURIEs/code, small page
  // header so multi-page exports are still identifiable.
  const outcome = r.outcome ?? (r.success ? "ok" : "failed");
  const plover = r.intermediates.plover_response_summary;
  const exportedAt = new Date().toISOString();

  // mini-graphs rendered as captured SVGs above the textual graph
  // summary. each gets a small caption naming the answer entity it
  // depicts. when there's no answer_graph_view (out_of_scope refusal,
  // no_results, etc.) capturedSVGs is empty and this block is skipped.
  const renderedGraphsHTML =
    capturedSVGs.length > 0
      ? `<h3>Rendered graphs</h3>` +
        capturedSVGs
          .map(
            (g, i) =>
              `<div class="mini-graph-block">` +
              `<div class="mini-graph-caption">Mini-graph ${i + 1} of ${capturedSVGs.length}: ` +
              `pinned entity → <strong>${escapeHTML(g.label)}</strong> ` +
              `(<code>${escapeHTML(g.answerCurie)}</code>)</div>` +
              `<div class="mini-graph-svg">${g.svg}</div>` +
              `</div>`,
          )
          .join("\n")
      : "";

  const graphHTML = r.answer_graph_view
    ? renderedGraphsHTML + graphSummaryHTML(r.answer_graph_view)
    : "";
  const pipelineHTML = pipelineSummaryHTML(r);
  const candidateProbes = r.intermediates.candidate_probes as
    | Record<string, unknown>
    | null
    | undefined;
  const candidateProbesHTML =
    candidateProbes && Object.keys(candidateProbes).length > 0
      ? candidateProbesHTMLFn(candidateProbes)
      : "";
  const predicateProbe = r.intermediates.predicate_probe as
    | Record<string, unknown>
    | null
    | undefined;
  const predicateProbeHTML = predicateProbe
    ? predicateProbeHTMLFn(predicateProbe)
    : "";

  // render the explanation prose as HTML, not <pre>. linkifyCitations
  // converts "[PMID:N, PMID:M]" into proper markdown links; linkifyCURIEs
  // wraps bare CURIEs (MONDO:0009061 etc.) in bioregistry links; then
  // `marked` parses the resulting markdown into HTML so headings, bold,
  // bullet lists, and the linkified PMID/CURIE references render as
  // actual structured content instead of raw "## Heading" / "[PMID:N]"
  // text. marked.parse is synchronous when given a string (typed
  // string | Promise<string> in the lib types, hence the cast).
  const explanationHTML = r.explanation
    ? `<div class="explanation">${marked.parse(
        linkifyCURIEs(linkifyCitations(r.explanation)),
      ) as string}</div>`
    : `<p class="empty">No explanation produced.</p>`;

  const trapiHTML = r.intermediates.trapi_query
    ? `<pre class="code">${escapeHTML(JSON.stringify(r.intermediates.trapi_query, null, 2))}</pre>`
    : "";

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PloverAI — ${escapeHTML(question)}</title>
  <style>
    @page { size: letter; margin: 0.7in 0.6in; }
    @media print {
      .no-print { display: none !important; }
      body { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
      h2, h3 { page-break-after: avoid; }
      table { page-break-inside: avoid; }
      .section { page-break-inside: auto; }
    }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      color: #111;
      background: #fff;
      font-size: 11pt;
      line-height: 1.45;
      max-width: 7.4in;
      margin: 0 auto;
      padding: 0 0.2in;
    }
    h1 { font-size: 18pt; margin: 0 0 8pt; }
    h2 { font-size: 13pt; margin: 22pt 0 6pt; border-bottom: 1px solid #ccc; padding-bottom: 3pt; }
    h3 { font-size: 11pt; margin: 14pt 0 4pt; }
    .meta-row { display: flex; flex-wrap: wrap; gap: 12pt; font-size: 9pt; color: #444; margin: 8pt 0 18pt; }
    .meta-row .k { color: #888; margin-right: 4pt; }
    .meta-row .v { font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; }
    .pill { display: inline-block; font-size: 8.5pt; padding: 1pt 6pt; border-radius: 3pt; border: 1px solid #888; background: #f6f6f6; }
    .pill.ok { background: #ddf3e3; border-color: #66a07f; color: #0a4023; }
    .pill.info { background: #fbe9c8; border-color: #c79a3a; color: #5a3b00; }
    .pill.failed { background: #f7d4d4; border-color: #a05050; color: #5a1010; }
    code { font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; font-size: 9.5pt; background: #f3f3f3; padding: 0.5pt 3pt; border-radius: 2pt; }
    pre.code { background: #f3f3f3; padding: 8pt; border: 1px solid #ddd; border-radius: 3pt; font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; font-size: 8.5pt; line-height: 1.35; overflow-wrap: break-word; white-space: pre-wrap; }
    table { width: 100%; border-collapse: collapse; margin: 8pt 0; font-size: 9.5pt; }
    th, td { border: 1px solid #ccc; padding: 4pt 6pt; text-align: left; vertical-align: top; }
    th { background: #f3f3f3; font-weight: 600; }
    td code { font-size: 8.5pt; }
    .empty { color: #888; font-style: italic; }
    .section { margin-bottom: 6pt; }
    .footer { color: #888; font-size: 8.5pt; margin-top: 24pt; border-top: 1px solid #ccc; padding-top: 6pt; }
    .kind-LLM      { background: #fef3c7; color: #7c5d00; padding: 0.5pt 4pt; border-radius: 2pt; font-size: 8pt; }
    .kind-service  { background: #e0f2fe; color: #075985; padding: 0.5pt 4pt; border-radius: 2pt; font-size: 8pt; }
    .kind-function { background: #f4f4f5; color: #3f3f46; padding: 0.5pt 4pt; border-radius: 2pt; font-size: 8pt; }
    .print-btn { position: fixed; top: 14px; right: 14px; padding: 8px 14px; background: #111; color: #fff; border: 0; border-radius: 4px; font-family: inherit; font-size: 12px; cursor: pointer; }

    /* Rendered explanation: marked-parsed HTML — give the headings,
       lists, and inline code reasonable thesis-grade styling. */
    .explanation h1, .explanation h2 { font-size: 12pt; margin: 14pt 0 5pt; border-bottom: 1px solid #ddd; padding-bottom: 2pt; }
    .explanation h3 { font-size: 11pt; margin: 10pt 0 4pt; }
    .explanation p { margin: 5pt 0; }
    .explanation ul, .explanation ol { margin: 5pt 0 5pt 20pt; padding: 0; }
    .explanation li { margin: 2pt 0; }
    .explanation a { color: #0050a0; text-decoration: underline; }
    .explanation strong { font-weight: 600; }
    .explanation code { background: #f3f3f3; padding: 0.5pt 3pt; border-radius: 2pt; }

    /* Captured mini-graphs from the live result panel. each SVG was
       cloned out of the React tree, so it still references the
       Tailwind utility classes the in-app components use. we recreate
       just the light-mode versions of those classes here so the SVG
       text fills resolve correctly in this isolated tab (dark: classes
       are ignored since there's no .dark ancestor). */
    .mini-graph-block { margin: 10pt 0 14pt; page-break-inside: avoid; }
    .mini-graph-caption { font-size: 9.5pt; color: #444; margin-bottom: 3pt; }
    .mini-graph-svg svg { max-width: 100%; height: auto; display: block; }
    .fill-zinc-900 { fill: #18181b; }
    .fill-zinc-800 { fill: #27272a; }
    .fill-zinc-700 { fill: #3f3f46; }
    .fill-zinc-600 { fill: #52525b; }
    .fill-zinc-500 { fill: #71717a; }
    .fill-zinc-400 { fill: #a1a1aa; }
    .fill-zinc-300 { fill: #d4d4d8; }
    .fill-zinc-200 { fill: #e4e4e7; }
    .fill-zinc-100 { fill: #f4f4f5; }
    .fill-zinc-50  { fill: #fafafa; }
    .fill-white    { fill: #ffffff; }
    .stroke-zinc-600 { stroke: #52525b; }
    .font-semibold { font-weight: 600; }
    .uppercase     { text-transform: uppercase; }
    .tracking-wider{ letter-spacing: 0.05em; }
    .font-mono     { font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; }
    .italic        { font-style: italic; }
    /* dark-mode-only utilities used by some SVG elements — explicitly
       no-op here so the inherited light-mode rule wins. without this
       rule the dark: classes don't apply (no .dark ancestor) but we
       list them for documentation. */
    [class*="dark:fill-"], [class*="dark:stroke-"] { /* no-op */ }
  </style>
</head>
<body>
  <button class="print-btn no-print" onclick="window.print()">Print / Save as PDF</button>

  <h1>${escapeHTML(question)}</h1>
  <div class="meta-row">
    <span><span class="pill ${outcomeClass(outcome)}">${escapeHTML(outcome)}</span></span>
    <span><span class="k">run</span><span class="v">${escapeHTML(r.run_id)}</span></span>
    <span><span class="k">model</span><span class="v">${escapeHTML(model)}</span></span>
    <span><span class="k">cost</span><span class="v">$${r.cost_usd.toFixed(6)}</span></span>
    <span><span class="k">elapsed</span><span class="v">${r.elapsed_s.toFixed(2)}s</span></span>
    ${plover ? `<span><span class="k">plover_results</span><span class="v">${plover.n_results}</span></span>` : ""}
    <span><span class="k">exported</span><span class="v">${exportedAt}</span></span>
  </div>

  <div class="section">
    <h2>Answer</h2>
    ${explanationHTML}
  </div>

  ${graphHTML ? `<div class="section"><h2>Answer graph</h2>${graphHTML}</div>` : ""}

  <div class="section">
    <h2>Pipeline stages</h2>
    ${pipelineHTML}
  </div>

  ${candidateProbesHTML ? `<div class="section"><h2>Candidate-density probe</h2>${candidateProbesHTML}</div>` : ""}

  ${predicateProbeHTML ? `<div class="section"><h2>Predicate-density probe (chosen CURIE)</h2>${predicateProbeHTML}</div>` : ""}

  ${trapiHTML ? `<div class="section"><h2>TRAPI query (LLM-constructed, Stage 8)</h2>${trapiHTML}</div>` : ""}

  <div class="footer">
    PloverAI export · run_id <code>${escapeHTML(r.run_id)}</code> · generated ${exportedAt}.
    For full per-stage prompts, NameRes/NodeNorm raw responses, PloverDB request/response,
    and PubTator NER traces, see the companion JSON export
    (<code>${escapeHTML(safeFileBase(r, "json"))}</code>).
  </div>
</body>
</html>`;
}

function outcomeClass(outcome: string): string {
  if (outcome === "ok") return "ok";
  if (outcome === "out_of_scope") return "info";
  return "failed";
}

function graphSummaryHTML(g: AnswerGraphView): string {
  const pin = g.pinned_node;
  const pinLine = `<p><strong>Pinned:</strong> ${escapeHTML(pin.label || "(no label)")} ` +
    `(<code>${escapeHTML(pin.curie)}</code>)` +
    (pin.category ? ` — ${escapeHTML(pin.category.replace(/^biolink:/, ""))}` : "") +
    `</p>`;
  let metricsLine = "";
  if (g.pubtator_metrics) {
    const m = g.pubtator_metrics;
    const rate = m.rate != null ? `${Math.round(m.rate * 100)}%` : "—";
    metricsLine = `<p><strong>PubTator verification:</strong> ${m.verified}/${m.verified + m.unverified} edges verified` +
      (m.not_applicable > 0 ? ` (${m.not_applicable} n/a)` : "") +
      ` · rate ${rate}</p>`;
  }
  if (g.answer_nodes.length === 0) {
    return pinLine + metricsLine + `<p class="empty">No answer entities returned.</p>`;
  }
  const rows = g.answer_nodes
    .map((a, i) => {
      const edges = g.edges.filter(
        (e) => e.source === a.curie || e.target === a.curie,
      );
      return `<tr><td>${i + 1}</td><td>${escapeHTML(a.label || a.curie)}</td>` +
        `<td><code>${escapeHTML(a.curie)}</code></td>` +
        `<td>${escapeHTML((a.category || "").replace(/^biolink:/, ""))}</td>` +
        `<td>${edges.length}</td>` +
        `<td>${pubtatorBadge(edges)}</td></tr>`;
    })
    .join("");

  const edgeBlocks = g.edges
    .map((e, i) => {
      const pred = (e.predicate || "?").replace(/^biolink:/, "");
      const lvl = e.knowledge_level || "—";
      const ks = e.primary_knowledge_source || "—";
      const pmidHTML =
        e.supporting_publications.length > 0
          ? e.supporting_publications
              .map(
                (p) =>
                  `<a href="https://pubmed.ncbi.nlm.nih.gov/${p.replace(/^PMID:/, "")}/">${escapeHTML(p)}</a>`,
              )
              .join(", ")
          : "—";
      const ptLine = e.pubtator_verified
        ? `<li>pubtator: ${
            e.pubtator_verified.verified
              ? "✓ co-mention found"
              : "✗ no co-mention"
          } ` +
          `(${e.pubtator_verified.co_mention_pmids.length} co, ` +
          `${e.pubtator_verified.subject_only_pmids.length} subj-only, ` +
          `${e.pubtator_verified.object_only_pmids.length} obj-only, ` +
          `${e.pubtator_verified.missing_pmids.length} not-indexed)</li>`
        : "";
      return `<div class="section"><strong>Edge ${i + 1}</strong> ` +
        `(<code>PloverDB-edge:${escapeHTML(e.id)}</code>): ` +
        `<code>${escapeHTML(e.source)}</code> — ${escapeHTML(pred)} → ` +
        `<code>${escapeHTML(e.target)}</code>` +
        `<ul style="margin: 4pt 0; padding-left: 18pt;">` +
        `<li>knowledge_level: ${escapeHTML(lvl)}</li>` +
        `<li>primary_knowledge_source: ${escapeHTML(ks)}</li>` +
        `<li>supporting_publications: ${pmidHTML}</li>` +
        ptLine +
        `</ul></div>`;
    })
    .join("");

  return pinLine + metricsLine +
    `<p><strong>Picked answers (${g.answer_nodes.length}):</strong></p>` +
    `<table><thead><tr><th>#</th><th>Label</th><th>CURIE</th><th>Category</th><th>Edges</th><th>PubTator</th></tr></thead>` +
    `<tbody>${rows}</tbody></table>` +
    `<h3>Edge provenance</h3>${edgeBlocks}`;
}

function pipelineSummaryHTML(r: QueryResponse): string {
  // re-implements pipelineSummaryMarkdown's stage table as HTML so we
  // can apply the kind-chip background colours and proper <table>
  // styling. duplication is intentional — round-tripping markdown to
  // HTML loses the chip styling and adds a parser dependency.
  const prompts = (r.intermediates.prompts as
    | Record<string, StagePromptEntry>
    | null
    | undefined) ?? {};
  const stages: Array<{
    n: string;
    kind: "LLM" | "service" | "function";
    label: string;
    promptKey?: string;
  }> = [
    { n: "1", kind: "LLM", label: "Scope check", promptKey: "stage_1_scope_check" },
    { n: "2", kind: "LLM", label: "Entity extract", promptKey: "stage_2_entity_extract" },
    { n: "3", kind: "service", label: "NameRes lookup" },
    { n: "4", kind: "LLM", label: "Candidate pick", promptKey: "stage_4_candidate_pick" },
    { n: "5", kind: "function", label: "IC re-rank" },
    { n: "6", kind: "service", label: "NodeNorm canonicalize pinned" },
    { n: "7", kind: "function", label: "Consistency check" },
    { n: "8", kind: "LLM", label: "TRAPI build", promptKey: "stage_8_trapi_build" },
    { n: "9", kind: "function", label: "Validation" },
    { n: "10", kind: "service", label: "PloverDB query" },
    { n: "11", kind: "LLM", label: "Answer pick", promptKey: "stage_11_answer_pick" },
    { n: "12", kind: "service", label: "NodeNorm canonicalize answers" },
    { n: "13", kind: "function", label: "Build graph view" },
    { n: "14", kind: "service", label: "PubTator enrichment" },
    { n: "15", kind: "LLM", label: "Explanation", promptKey: "stage_15_explain" },
  ];

  const rows = stages
    .map((s) => {
      let status = "—";
      let inTok = "—";
      let outTok = "—";
      let lat = "—";
      if (s.promptKey && prompts[s.promptKey]) {
        const resp = prompts[s.promptKey].response;
        if (resp?.input_tokens !== undefined) {
          inTok = String(resp.input_tokens);
          outTok = String(resp.output_tokens ?? "—");
          lat = resp.latency_s != null ? `${resp.latency_s.toFixed(2)}s` : "—";
          status = "✓ ran";
        } else {
          status = "ran (no token meta)";
        }
      } else if (s.kind !== "LLM") {
        status = inferServiceStatus(s.n, r);
      }
      return `<tr><td>${s.n}</td>` +
        `<td><span class="kind-${s.kind}">${s.kind}</span></td>` +
        `<td>${escapeHTML(s.label)}</td>` +
        `<td>${escapeHTML(status)}</td>` +
        `<td>${inTok}</td><td>${outTok}</td><td>${lat}</td></tr>`;
    })
    .join("");
  return `<table><thead><tr><th>#</th><th>Kind</th><th>Stage</th><th>Status</th><th>In tok</th><th>Out tok</th><th>Latency</th></tr></thead>` +
    `<tbody>${rows}</tbody></table>`;
}

function candidateProbesHTMLFn(data: Record<string, unknown>): string {
  const answerCat = (data.answer_cat as string) || "?";
  const filter = data.filter_applied as string[] | undefined;
  const fallback = data.fallback_to_loose === true;
  const intro = `<p>Per-candidate edge-density probe against <code>${escapeHTML(answerCat)}</code> ` +
    `(filter: ${filter ? `<code>${escapeHTML(JSON.stringify(filter))}</code>` : "none"}` +
    `${fallback ? " — loose fallback was triggered" : ""}).</p>`;

  const byCurie = (data.by_curie as Record<string, ProbeEntry>) ?? {};
  const rows = Object.entries(byCurie).sort(
    ([, a], [, b]) => (b.total_edges ?? 0) - (a.total_edges ?? 0),
  );
  if (rows.length === 0) return intro + `<p class="empty">No probe rows.</p>`;
  const body = rows
    .map(([curie, p]) => {
      const note = p.error
        ? `error: ${escapeHTML(p.error)}`
        : (p.total_edges ?? 0) === 0
          ? "no edges"
          : "ok";
      return `<tr><td><code>${escapeHTML(curie)}</code></td>` +
        `<td>${p.total_edges ?? 0}</td><td>${note}</td></tr>`;
    })
    .join("");
  return intro +
    `<table><thead><tr><th>CURIE</th><th>KG2c edges</th><th>Note</th></tr></thead>` +
    `<tbody>${body}</tbody></table>`;
}

function predicateProbeHTMLFn(p: Record<string, unknown>): string {
  const curie = (p.pinned_curie as string) || "?";
  const answerCat = (p.answer_cat as string) || "?";
  const total = (p.total_edges as number) ?? 0;
  const intro = `<p>Pinned CURIE <code>${escapeHTML(curie)}</code> against ` +
    `<code>${escapeHTML(answerCat)}</code> — ${total} total edges.</p>`;
  const byPred = (p.by_predicate as Record<string, { count?: number; forward?: number; reverse?: number }>) || {};
  const rows = Object.entries(byPred).sort(
    ([, a], [, b]) => (b.count ?? 0) - (a.count ?? 0),
  );
  if (rows.length === 0) return intro + `<p class="empty">No populated predicates.</p>`;
  const body = rows
    .map(
      ([pred, s]) =>
        `<tr><td><code>${escapeHTML(pred)}</code></td>` +
        `<td>${s.count ?? 0}</td><td>${s.forward ?? 0}</td><td>${s.reverse ?? 0}</td></tr>`,
    )
    .join("");
  return intro +
    `<table><thead><tr><th>Predicate</th><th>Count</th><th>Forward</th><th>Reverse</th></tr></thead>` +
    `<tbody>${body}</tbody></table>`;
}

// ---------- utilities ----------

function safeFileBase(r: QueryResponse, ext: string): string {
  const stub = r.run_id?.replace(/[^a-zA-Z0-9_-]/g, "_") || "result";
  return `ploverai_${stub}.${ext}`;
}

function escapeCell(s: string): string {
  return s.replace(/\|/g, "\\|").replace(/\n/g, " ");
}

function escapeHTML(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function jsonString(s: string): string {
  // JSON-quote a string for safe inline in YAML frontmatter.
  return JSON.stringify(s);
}

function downloadBlob(
  content: string,
  filename: string,
  mimeType: string,
): void {
  const blob = new Blob([content], { type: `${mimeType};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
