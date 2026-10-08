// how a fact of the ARAX reasoning graph reads to a person: its
// statement, its source, its evidence level in plain words, a short tag
// for the citation chip, and the links a reader checks it with. shared
// by the citation cards, the evidence list and the Markdown / PDF
// exports, so every place says the same thing about a fact.

import type { FactEvidence, ReasoningFact, ReasoningGraph } from "@/lib/api";
import { humanPredicate } from "@/lib/reasoningLinks";

// mirrors KNOWLEDGE_LEVEL_WORDS / AGENT_TYPE_WORDS in
// pipeline/code/arax_reasoning.py.
const LEVEL_WORDS: Record<string, string> = {
  knowledge_assertion: "curated assertion",
  logical_entailment: "inferred from an ontology",
  prediction: "prediction",
  statistical_association: "statistical association",
  observation: "observation",
  not_provided: "evidence level not recorded",
};
const AGENT_WORDS: Record<string, string> = {
  manual_agent: "made by a curator",
  manual_validation_of_automated_agent: "automated, checked by a curator",
  automated_agent: "made by software",
  data_analysis_pipeline: "made by an analysis pipeline",
  computational_model: "made by a computational model",
  text_mining_agent: "text-mined",
  not_provided: "agent not recorded",
};

export function levelText(fact: ReasoningFact): string {
  const level = fact.knowledge_level ?? "not_provided";
  const agent = fact.agent_type ?? "not_provided";
  return `${LEVEL_WORDS[level] ?? level.replaceAll("_", " ")}, ${AGENT_WORDS[agent] ?? agent.replaceAll("_", " ")}`;
}

export function sourceText(fact: ReasoningFact): string {
  const source = fact.source_label ?? (fact.primary_source ?? "source not recorded").replace(/^infores:/, "");
  const via = fact.evidence?.via ?? [];
  return via.length > 0 ? `${source}, from ${via.join(", ")}` : source;
}

export function statement(fact: ReasoningFact, labels: Map<string, string>): {
  subject: string;
  predicate: string;
  object: string;
} {
  return {
    subject: labels.get(fact.source) ?? fact.source,
    predicate: humanPredicate(fact.predicate),
    object: labels.get(fact.target) ?? fact.target,
  };
}

const EMPTY: FactEvidence = {
  approval: null,
  max_phase: null,
  fda_approvals: [],
  labels: [],
  trials: [],
  n_trials: 0,
  pmids: [],
  n_pmids: 0,
  ngd: null,
  via: [],
  record_url: null,
};

export function evidenceOf(fact: ReasoningFact): FactEvidence {
  return fact.evidence ?? EMPTY;
}

// the few words a citation chip carries after its id: the strongest
// kind of evidence the fact rests on.
export function factTag(fact: ReasoningFact): string {
  const e = evidenceOf(fact);
  if (e.approval) return e.approval.startsWith("not") ? e.approval : "approved";
  if (e.n_trials > 0) {
    return `${e.n_trials} trial${e.n_trials === 1 ? "" : "s"}${e.max_phase ? `, phase ${e.max_phase}` : ""}`;
  }
  if (e.labels.length > 0) return "FDA label";
  if (e.ngd !== null) return "co-mention";
  if (e.n_pmids > 0) return `${e.n_pmids} paper${e.n_pmids === 1 ? "" : "s"}`;
  const source = fact.source_label ?? (fact.primary_source ?? "").replace(/^infores:/, "");
  return source.replace(/\s*\(.*\)$/, "") || "fact";
}

// the evidence as short readable items, strongest first, for the
// evidence list and the exports.
export function evidenceItems(fact: ReasoningFact): string[] {
  const e = evidenceOf(fact);
  const items: string[] = [];
  if (e.approval) items.push(e.approval);
  if (e.fda_approvals.length > 0) items.push(`FDA application ${e.fda_approvals.slice(0, 3).join(", ")}`);
  if (e.labels.length > 0) items.push(`${e.labels.length} FDA drug label${e.labels.length === 1 ? "" : "s"}`);
  if (e.n_trials > 0) {
    items.push(
      `${e.n_trials} registered trial${e.n_trials === 1 ? "" : "s"}` +
        (e.max_phase ? `, most advanced phase ${e.max_phase}` : ""),
    );
  } else if (e.max_phase) {
    items.push(`most advanced research phase ${e.max_phase}`);
  }
  if (e.n_pmids > 0) items.push(`${e.n_pmids} PubMed article${e.n_pmids === 1 ? "" : "s"}`);
  if (e.ngd !== null) items.push(`PubMed co-mention distance ${e.ngd.toFixed(2)}`);
  return items;
}

export function pubmedUrl(pmid: string): string {
  return `https://pubmed.ncbi.nlm.nih.gov/${pmid.replace(/^PMID:/i, "")}/`;
}

// Drugs@FDA takes the six-digit application number without its NDA /
// ANDA / BLA prefix.
export function fdaApplicationUrl(application: string): string {
  const number = application.replace(/^[A-Za-z]+/, "");
  return `https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo=${number}`;
}

// the reasoning path a fact sits on (the first that contains it), as
// its node ids, plus which step of it the fact supports.
export function pathOfFact(
  graph: ReasoningGraph,
  factId: string,
): { answer: string; nodes: string[]; step: number } | null {
  for (const [answer, paths] of Object.entries(graph.path_facts)) {
    for (let p = 0; p < paths.length; p++) {
      const step = paths[p].findIndex((facts) => facts.includes(factId));
      if (step >= 0) return { answer, nodes: graph.paths[answer]?.[p] ?? [], step };
    }
  }
  return null;
}

// "F3" -> ["F3"]; "F1–F4" (any dash) -> ["F1", "F2", "F3", "F4"];
// "F1|F3|F9" (a folded group, lib/linkify.ts) -> ["F1", "F3", "F9"].
export const FACT_CITATION = /^F(\d+)(?:\s*[-–—]\s*F?(\d+)|(?:\|F\d+)+)?$/;

export function citedFactIds(content: string): string[] {
  if (content.includes("|")) return content.split("|");
  const match = content.match(FACT_CITATION);
  if (!match) return [];
  const first = Number(match[1]);
  const last = match[2] ? Number(match[2]) : first;
  if (last < first || last - first > 60) return [`F${first}`];
  return Array.from({ length: last - first + 1 }, (_, i) => `F${first + i}`);
}
