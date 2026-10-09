// reads a one-hop TRAPI query graph (two nodes, one edge) into what the
// page shows as "interpreted as": which end is pinned to an entity,
// which end is the answer, the predicate, its qualifiers, and whether
// ARAX is asked to reason (knowledge_type "inferred").

export type QueryGraph = {
  nodes?: Record<string, { ids?: string[]; categories?: string[] }>;
  edges?: Record<
    string,
    {
      subject?: string;
      object?: string;
      predicates?: string[];
      knowledge_type?: string;
      qualifier_constraints?: { qualifier_set?: { qualifier_type_id?: string; qualifier_value?: string }[] }[];
    }
  >;
};

export type QueryEnd = {
  ids: string[];
  category: string | null;
  pinned: boolean;
};

export type QueryReading = {
  subject: QueryEnd;
  object: QueryEnd;
  predicate: string | null;
  qualifiers: string[];
  inferred: boolean;
};

const bare = (term: string) => term.replace(/^biolink:/, "");

// the TRAPI message ({message: {query_graph}}) or the query graph itself.
export function queryGraphOf(value: unknown): QueryGraph | null {
  if (!value || typeof value !== "object") return null;
  const message = (value as { message?: { query_graph?: unknown } }).message;
  const graph = message?.query_graph ?? value;
  return graph && typeof graph === "object" && "edges" in graph ? (graph as QueryGraph) : null;
}

export function readQuery(graph: QueryGraph | null): QueryReading | null {
  const edge = Object.values(graph?.edges ?? {})[0];
  if (!graph || !edge?.subject || !edge.object) return null;
  const end = (id: string): QueryEnd => {
    const node = graph.nodes?.[id] ?? {};
    const ids = node.ids ?? [];
    return { ids, category: node.categories?.[0] ? bare(node.categories[0]) : null, pinned: ids.length > 0 };
  };
  const qualifiers = (edge.qualifier_constraints ?? [])
    .flatMap((c) => c.qualifier_set ?? [])
    .map((q) => `${bare(q.qualifier_type_id ?? "").replaceAll("_", " ")}: ${bare(q.qualifier_value ?? "").replaceAll("_", " ")}`);
  return {
    subject: end(edge.subject),
    object: end(edge.object),
    predicate: edge.predicates?.[0] ? bare(edge.predicates[0]).replaceAll("_", " ") : null,
    qualifiers,
    inferred: edge.knowledge_type === "inferred",
  };
}

// one line, for comparing the LLM's query with the one sent to ARAX.
export function queryLine(reading: QueryReading): string {
  const side = (e: QueryEnd) => (e.pinned ? e.ids.join(", ") : `? ${e.category ?? "any"}`);
  return [
    side(reading.subject),
    reading.predicate ?? "related to",
    ...(reading.qualifiers.length > 0 ? [`[${reading.qualifiers.join("; ")}]`] : []),
    reading.inferred ? "(inferred)" : "",
    side(reading.object),
  ]
    .filter(Boolean)
    .join(" ");
}
