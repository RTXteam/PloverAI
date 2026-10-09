# reduction.py — the Stage 11 pre-step of the lookup condition. a
# one-hop Tier 0 lookup answers a broad
# question with everything it has (one pilot question came back with
# 286,544 results), so handing the raw TRAPI body to the LLM means the
# model reads an arbitrary prefix of a megabyte-sized document —
# whatever happened to serialise first, not whatever is best supported.
# this module turns ANY response into a ranked, tabular evidence view
# of bounded size. it runs on every question, not only the big ones,
# so the reduction is part of the method and not a size-triggered
# accident.
#
# two deterministic steps:
#   B — rank every edge in message.knowledge_graph.edges by evidence
#       strength: knowledge_level, then agent_type, then how much
#       INDEPENDENT CORROBORATION the response holds for the same
#       entity pair, then the edge's own publication count, then
#       edge_id (so the order is reproducible), and keep the top K.
#   C — flatten each kept edge into one flat row, then render one line
#       per row for the prompt.
#
# no IO and no logging here: pipeline.py writes the artifact and logs
# the counts. that keeps every function in this file pure and unit-
# testable against a synthetic TRAPI message.

from __future__ import annotations

# re: stdlib. one regex, used to normalise publication identifiers to
# the PMID:<digits> form the Stage 15 citation rules expect.
import re

# dataclasses: stdlib. ReducedData is frozen for the same reason the
# config objects are — built once, only ever read.
from dataclasses import dataclass

# typing.Any: stdlib. a TRAPI message is nested free-form JSON whose
# shape is defined by the TRAPI 1.5 spec, not by our own types.
from typing import Any


# Biolink KnowledgeLevelEnum, strongest first. Biolink defines the
# values but does not rank them — this ordering is the heuristic
# documented in code/README.md §Evidence, and it is the primary sort
# key here so the LLM reads curated assertions before text mining.
KNOWLEDGE_LEVEL_RANK: dict[str, int] = {
    "knowledge_assertion": 0,
    "logical_entailment": 1,
    "prediction": 2,
    "statistical_association": 3,
    "observation": 4,
    "not_provided": 5,
}

# Biolink AgentTypeEnum, strongest first. same direction as above: a
# human curator beats a human-validated machine beats a machine.
AGENT_TYPE_RANK: dict[str, int] = {
    "manual_agent": 0,
    "manual_validation_of_automated_agent": 1,
    "automated_agent": 2,
    "data_analysis_pipeline": 3,
    "computational_model": 4,
    "text_mining_agent": 5,
    "image_processing_agent": 6,
    "not_provided": 7,
}

# the value we write when the source did not record the field. it is
# also the rank bucket any unrecognised enum value falls into, so an
# enum value added upstream can never outrank a curated assertion.
NOT_PROVIDED = "not_provided"

# recorded verbatim into reduced_data.json so a reader of the artifact
# knows how the rows were ordered without reading this file.
RANKING_KEY = (
    "(knowledge_level_rank, agent_type_rank, -n_sources, "
    "-total_publications, -n_publications, edge_id)"
)

# an edge from SemMedDB can carry 30+ PMIDs. three is enough for the
# explainer to cite and keeps a row near the ~80-token budget; the
# full list stays in reasoner_response.json.
MAX_PUBLICATIONS_PER_ROW = 3

# entity names in the graph run to hundreds of characters ("2 ML vitamin A
# 50000 UNT/ML Injection [Aquasol A]" is a short one). the CURIE, not
# the name, is what the LLM has to copy, so the name is only a label.
MAX_NAME_CHARS = 60

# accepts PMID:123, pmid 123, PMID_123. anything else (DOI, ISBN, a
# bare integer we cannot prove is a PMID) is kept verbatim and sorted
# after the PMIDs.
_PMID_PATTERN = re.compile(r"^pmid[:_\-\s]*(\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class ReducedData:
    # rows are already sorted strongest-first and already cut to top_k.
    # the stats describe the WHOLE response so a reader can see what
    # was dropped, not only what survived.
    rows: list[dict[str, Any]]
    total_results: int
    total_edges: int
    kept: int
    top_k: int
    ranking_key: str
    knowledge_level_counts: dict[str, int]


def reduce_response(response: dict[str, Any], *, top_k: int) -> ReducedData:
    message = response.get("message") or {}
    knowledge_graph = message.get("knowledge_graph") or {}
    nodes: dict[str, Any] = knowledge_graph.get("nodes") or {}
    edges: dict[str, Any] = knowledge_graph.get("edges") or {}

    rows = [
        _edge_row(str(edge_id), edge, nodes)
        for edge_id, edge in edges.items()
        if isinstance(edge, dict)
    ]

    # corroboration is a property of the entity PAIR, not of a single
    # edge: how many independent sources, edges and publications the
    # WHOLE response holds for the same two entities. it is aggregated
    # over every edge (before the cut) and attached to each edge of the
    # pair, so an edge that is one of several independent statements
    # about the same pair sorts above an isolated one. it only ever
    # breaks ties INSIDE a tier — see _rank_key.
    #
    # without it, a block of edges that agree on knowledge_level,
    # agent_type and publication count is ordered by edge_id, which is
    # an arbitrary insertion order: on "alzhiemers genes" the 22
    # knowledge_assertion / manual_agent / 0-publication edges from
    # infores:ncit tied completely and the table led with NOS3, CTSB
    # and HLA-A while APOE (4 corroborating edges across 3 sources)
    # sat at rank 8.
    corroboration = _corroboration_index(rows)
    for row in rows:
        row.update(corroboration[_pair_key(row)])

    rows.sort(key=_rank_key)

    # counts are computed over every edge, before the cut, so the
    # artifact can answer "how much knowledge_assertion evidence did
    # the cut throw away?".
    counts: dict[str, int] = {}
    for row in rows:
        level = str(row["knowledge_level"])
        counts[level] = counts.get(level, 0) + 1
    ordered_counts = {
        level: counts[level]
        for level in sorted(counts, key=_knowledge_level_rank)
    }

    # a negative top_k would slice from the end of the list, which is
    # the opposite of what the name promises. clamp instead.
    kept_rows = rows[: max(top_k, 0)]

    return ReducedData(
        rows=kept_rows,
        total_results=len(message.get("results") or []),
        total_edges=len(rows),
        kept=len(kept_rows),
        top_k=top_k,
        ranking_key=RANKING_KEY,
        knowledge_level_counts=ordered_counts,
    )


def render_table(reduced: ReducedData) -> str:
    # the header has to carry three things the LLM cannot infer from
    # the rows: that it is seeing a ranked subset (and how big the
    # whole was), how to read a row, and which ladder produced the
    # order. everything after the blank line is one edge per line.
    header = (
        f"Ranked evidence table: showing {reduced.kept} of {reduced.total_edges} edges "
        f"({reduced.total_results} results), ranked by evidence strength: "
        f"knowledge_level, then agent_type, then corroboration for the same entity pair "
        f"(distinct sources, then total publications), then this edge's own "
        f"publication count. Rows with identical knowledge_level, agent_type and corr "
        f"are tied: their order among themselves is by edge id and carries no information.\n"
        f"Each row is one edge, strongest first: "
        f"edge_id | subject_name (SUBJECT_CURIE) --predicate--> object_name (OBJECT_CURIE) | "
        f"kl=<knowledge_level> agent=<agent_type> pubs=<count> [up to "
        f"{MAX_PUBLICATIONS_PER_ROW} PMIDs] src=<primary_knowledge_source> "
        f"qual=[<qualifier_type_id>=<value>, ...] "
        f"corr=<sources>src/<edges>e/<publications>p; "
        f"names are truncated to {MAX_NAME_CHARS} characters, and the PMID list, src "
        f"and qual are omitted when the edge does not carry them.\n"
        f"qual= lists the edge's Biolink qualifiers, which state the direction or aspect "
        f"of the relation (e.g. object_direction_qualifier=decreased with "
        f"object_aspect_qualifier=activity means the subject decreases the object's "
        f"activity); a row without qual= states no direction at all.\n"
        f"corr= describes the WHOLE subject/object pair across the full response, not "
        f"just this row: how many distinct primary knowledge sources, how many edges, and "
        f"how many publications connect those same two entities. A higher corr means more "
        f"independent corroboration, so prefer answers whose rows corroborate each other.\n"
        f"knowledge_level order used for the ranking (strongest first): "
        f"{' > '.join(KNOWLEDGE_LEVEL_RANK)}.\n"
        f"agent_type order used for the ranking (strongest first): "
        f"{' > '.join(AGENT_TYPE_RANK)}."
    )
    return "\n".join([header, "", *(_render_row(row) for row in reduced.rows)])


def edge_qualifiers(edge: dict[str, Any]) -> list[tuple[str, str]]:
    # TRAPI 1.5 stores an edge's qualifiers as a LIST of
    # {qualifier_type_id, qualifier_value} objects. they carry the part
    # of the statement the predicate alone cannot: "affects" plus
    # object_direction_qualifier=decreased is "decreases". sorted so the
    # same set always renders the same way, whatever order the source
    # emitted it in.
    pairs: list[tuple[str, str]] = []
    for qualifier in edge.get("qualifiers") or []:
        if not isinstance(qualifier, dict):
            continue
        type_id = qualifier.get("qualifier_type_id")
        value = qualifier.get("qualifier_value")
        if isinstance(type_id, str) and type_id and isinstance(value, str) and value:
            pairs.append((type_id, value))
    return sorted(pairs)


def render_qualifiers(pairs: list[tuple[str, str]]) -> str:
    # full Biolink ids, not shortened ones: Stage 8 copies these into
    # qualifier_constraints and Stage 11 compares them to the question,
    # so the text must be exactly what TRAPI expects.
    return ", ".join(f"{type_id}={value}" for type_id, value in pairs)


def to_json(reduced: ReducedData) -> dict[str, Any]:
    return {
        "ranking_key": reduced.ranking_key,
        "top_k": reduced.top_k,
        "total_results": reduced.total_results,
        "total_edges": reduced.total_edges,
        "kept": reduced.kept,
        "knowledge_level_counts": reduced.knowledge_level_counts,
        "rows": reduced.rows,
    }


# ---------- internal helpers ----------

def _edge_row(edge_id: str, edge: dict[str, Any], nodes: dict[str, Any]) -> dict[str, Any]:
    attributes = _attribute_map(edge)
    subject_curie = str(edge.get("subject") or "")
    object_curie = str(edge.get("object") or "")
    subject_name, subject_category = _node_fields(nodes, subject_curie)
    object_name, object_category = _node_fields(nodes, object_curie)
    publications = _normalise_publications(attributes.get("biolink:publications"))
    qualifiers = edge_qualifiers(edge)
    return {
        "edge_id": edge_id,
        "subject_curie": subject_curie,
        "subject_name": subject_name,
        "subject_category": subject_category,
        "predicate": str(edge.get("predicate") or ""),
        "object_curie": object_curie,
        "object_name": object_name,
        "object_category": object_category,
        "knowledge_level": _enum_value(attributes.get("biolink:knowledge_level")),
        "agent_type": _enum_value(attributes.get("biolink:agent_type")),
        # the count is over ALL publications; only the first few are
        # carried in the row, so the two fields disagree on purpose.
        "n_publications": len(publications),
        "publications": publications[:MAX_PUBLICATIONS_PER_ROW],
        "primary_knowledge_source": _primary_knowledge_source(edge),
        # None, not "", for an unqualified edge: most edges carry
        # no qualifiers (582 of 614 affects edges into CYP2D6), and the
        # artifact should say "absent" rather than "empty string".
        "qualifiers": render_qualifiers(qualifiers) if qualifiers else None,
    }


def _attribute_map(edge: dict[str, Any]) -> dict[str, Any]:
    # TRAPI stores edge metadata as a LIST of {attribute_type_id, value}
    # objects, not a dict. first occurrence wins, so a source that
    # repeats an attribute cannot flip the ranking non-deterministically.
    attributes: dict[str, Any] = {}
    for attribute in edge.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        type_id = attribute.get("attribute_type_id")
        if isinstance(type_id, str) and type_id not in attributes:
            attributes[type_id] = attribute.get("value")
    return attributes


def _node_fields(nodes: dict[str, Any], curie: str) -> tuple[str | None, str | None]:
    node = nodes.get(curie)
    if not isinstance(node, dict):
        return None, None
    name = node.get("name")
    categories = node.get("categories")
    first_category = categories[0] if isinstance(categories, list) and categories else None
    return (
        name if isinstance(name, str) and name else None,
        first_category if isinstance(first_category, str) and first_category else None,
    )


def _enum_value(raw: Any) -> str:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return NOT_PROVIDED


def _normalise_publications(raw: Any) -> list[str]:
    # the attribute value is a list on most edges but a bare string on
    # some sources, so accept both. PMIDs are sorted to the front
    # because they are the only identifiers Stage 15 can turn into a
    # clickable citation.
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [value for value in raw if isinstance(value, str)]
    else:
        return []
    pmids: list[str] = []
    others: list[str] = []
    for value in values:
        text = value.strip()
        if not text:
            continue
        match = _PMID_PATTERN.match(text)
        if match:
            pmids.append(f"PMID:{match.group(1)}")
        else:
            others.append(text)
    return pmids + others


def _primary_knowledge_source(edge: dict[str, Any]) -> str | None:
    for source in edge.get("sources") or []:
        if not isinstance(source, dict):
            continue
        if source.get("resource_role") == "primary_knowledge_source":
            resource_id = source.get("resource_id")
            if isinstance(resource_id, str) and resource_id:
                return resource_id
    return None


def _pair_key(row: dict[str, Any]) -> tuple[str, str]:
    # unordered: "APOE treats X" and "X caused_by APOE" are two
    # statements about the SAME pair, so direction must not split them
    # into two corroboration buckets.
    subject = str(row["subject_curie"])
    obj = str(row["object_curie"])
    return (subject, obj) if subject <= obj else (obj, subject)


def _corroboration_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, int]]:
    counts: dict[tuple[str, str], dict[str, int]] = {}
    sources: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        pair = _pair_key(row)
        bucket = counts.setdefault(pair, {
            "n_corroborating_edges": 0,
            "n_sources": 0,
            "total_publications": 0,
        })
        bucket["n_corroborating_edges"] += 1
        bucket["total_publications"] += int(row["n_publications"])
        source = row["primary_knowledge_source"]
        if source:
            sources.setdefault(pair, set()).add(str(source))
    # distinct non-null primary sources. an edge whose source did not
    # record itself contributes an edge and its publications but not a
    # source — we cannot claim independence we cannot name.
    for pair, bucket in counts.items():
        bucket["n_sources"] = len(sources.get(pair, set()))
    return counts


def _knowledge_level_rank(level: str) -> int:
    return KNOWLEDGE_LEVEL_RANK.get(level, KNOWLEDGE_LEVEL_RANK[NOT_PROVIDED])


def _rank_key(row: dict[str, Any]) -> tuple[int, int, int, int, int, str]:
    # tier ALWAYS first. corroboration orders rows inside a tier and
    # never lifts one across a tier boundary: a text-mined edge with
    # 40 supporting publications must still sort below every curated
    # assertion, because volume of text is not curation.
    return (
        _knowledge_level_rank(str(row["knowledge_level"])),
        AGENT_TYPE_RANK.get(str(row["agent_type"]), AGENT_TYPE_RANK[NOT_PROVIDED]),
        -int(row["n_sources"]),
        -int(row["total_publications"]),
        -int(row["n_publications"]),
        str(row["edge_id"]),
    )


def _render_row(row: dict[str, Any]) -> str:
    subject = _render_endpoint(row["subject_name"], str(row["subject_curie"]))
    obj = _render_endpoint(row["object_name"], str(row["object_curie"]))
    line = (
        f"{row['edge_id']} | {subject} --{row['predicate']}--> {obj} | "
        f"kl={row['knowledge_level']} agent={row['agent_type']} "
        f"pubs={row['n_publications']}"
    )
    publications = row["publications"]
    if publications:
        line += " [" + ", ".join(str(p) for p in publications) + "]"
    source = row["primary_knowledge_source"]
    if source:
        line += f" src={source}"
    qualifiers = row["qualifiers"]
    if qualifiers:
        line += f" qual=[{qualifiers}]"
    # always rendered, including for an isolated edge (corr=1src/1e/0p):
    # "nothing else in this response says the same thing" is exactly
    # the signal the model needs when two rows are otherwise identical.
    line += (
        f" corr={row['n_sources']}src/"
        f"{row['n_corroborating_edges']}e/"
        f"{row['total_publications']}p"
    )
    return line


def _render_endpoint(name: Any, curie: str) -> str:
    # the CURIE is the part the LLM has to copy verbatim, so it is
    # always present even when the knowledge graph gave us no label.
    label = name if isinstance(name, str) and name else curie
    return f"{_truncate(label)} ({curie})"


def _truncate(text: str) -> str:
    if len(text) <= MAX_NAME_CHARS:
        return text
    return text[: MAX_NAME_CHARS - 3] + "..."
