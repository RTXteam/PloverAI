// turns an ARAX reasoning graph (pipeline/code/arax_reasoning.py) into
// the links the figure draws: one link per pair of nodes that sit next
// to each other on some reasoning path.
//
// several facts often join the same two nodes (a clinical trial, an FDA
// report, a curated assertion, a literature co-occurrence...). they share
// one link. its width says how many facts there are, its line style the
// strongest kind of evidence among them, and its arrow and label the most
// specific statement among those strongest facts. the side panel lists
// every fact.

import type { ReasoningFact, ReasoningGraph } from "@/lib/api";

// how strong a fact is, from Biolink's knowledge_level.
export type Strength = "curated" | "inferred" | "statistical" | "weak";

export function strengthOf(level: string | null): Strength {
  if (level === "knowledge_assertion") return "curated";
  if (level === "logical_entailment" || level === "prediction") return "inferred";
  if (level === "statistical_association" || level === "observation") return "statistical";
  return "weak";
}

export const STRENGTH_ORDER: Strength[] = ["curated", "inferred", "statistical", "weak"];

// predicates that say something specific label a link first; catch-all
// ones last. anything unlisted sits in between.
const SPECIFIC = [
  "treats",
  "in_clinical_trials_for",
  "applied_to_treat",
  "preventative_for_condition",
  "ameliorates_condition",
  "subclass_of",
  "causes",
  "contributes_to",
  "gene_associated_with_condition",
  "physically_interacts_with",
  "regulates",
  "affects",
  "interacts_with",
];
const GENERIC = [
  "treats_or_applied_or_studied_to_treat",
  "associated_with",
  "correlated_with",
  "occurs_together_in_literature_with",
  "related_to",
];

function specificity(predicate: string): number {
  const bare = predicate.replace(/^biolink:/, "");
  const specific = SPECIFIC.indexOf(bare);
  if (specific >= 0) return specific;
  const generic = GENERIC.indexOf(bare);
  return generic >= 0 ? 100 + generic : 50;
}

export function humanPredicate(predicate: string): string {
  return predicate.replace(/^biolink:/, "").replaceAll("_", " ");
}

export type LinkSpec = {
  id: string;
  // subject and object of the link's representative fact: the arrow
  // points from source to target. every fact keeps its own direction in
  // the side panel.
  source: string;
  target: string;
  facts: ReasoningFact[];
  answers: string[];
  strength: Strength;
  // the representative fact's predicate, without the biolink: prefix.
  predicate: string;
};

export function buildLinks(graph: ReasoningGraph): LinkSpec[] {
  const factsByPair = new Map<string, ReasoningFact[]>();
  for (const fact of graph.edges) {
    const key = pairKey(fact.source, fact.target);
    const list = factsByPair.get(key) ?? [];
    list.push(fact);
    factsByPair.set(key, list);
  }
  const byPair = new Map<string, LinkSpec>();
  for (const node of graph.nodes) {
    if (node.role !== "answer") continue;
    for (const path of graph.paths[node.id] ?? []) {
      for (let i = 0; i + 1 < path.length; i++) {
        const key = pairKey(path[i], path[i + 1]);
        if (byPair.has(key)) continue;
        const facts = factsByPair.get(key) ?? [];
        if (facts.length === 0) continue;
        const strength = STRENGTH_ORDER.find((s) => facts.some((f) => strengthOf(f.knowledge_level) === s)) ?? "weak";
        const representative = facts
          .filter((f) => strengthOf(f.knowledge_level) === strength)
          .reduce((best, f) => (specificity(f.predicate) < specificity(best.predicate) ? f : best));
        byPair.set(key, {
          id: `link:${key}`,
          source: representative.source,
          target: representative.target,
          facts,
          answers: [...new Set(facts.flatMap((f) => f.answers))],
          strength,
          predicate: representative.predicate.replace(/^biolink:/, ""),
        });
      }
    }
  }
  return [...byPair.values()];
}

export function pairKey(a: string, b: string): string {
  return a < b ? `${a}|${b}` : `${b}|${a}`;
}

// "in clinical trials for ×2" lines for a link's tooltip, most specific first.
export function predicateSummary(facts: ReasoningFact[]): string[] {
  const counts = new Map<string, number>();
  for (const fact of facts) counts.set(fact.predicate, (counts.get(fact.predicate) ?? 0) + 1);
  return [...counts.entries()]
    .sort(([a], [b]) => specificity(a) - specificity(b))
    .map(([predicate, n]) => `${humanPredicate(predicate)}${n > 1 ? ` ×${n}` : ""}`);
}
