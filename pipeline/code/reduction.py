# reduction.py — Strategy B response reduction for the PloverDB body
# fed into Stage 11 (answer_pick). full spec:
# docs/specs/response-reduction-strategy-b.md.
#
# the function operates on TRAPI 1.5 results (not on loose edges) so
# the result→edge bindings stay intact: dropping a result removes its
# edge cleanly, where dropping a loose edge would silently invalidate
# every result that referenced it.
#
# pure function, no IO. tested in tests/test_response_reduction.py.

from __future__ import annotations

# dataclasses: stdlib. frozen dataclasses give us a typed, hashable
# return shape so callers (Stage 11, the artifact writer, the
# faithfulness evaluator) can rely on field names not silently
# changing.
from dataclasses import dataclass
# logging: stdlib. one INFO line at exit summarising the reduction,
# one WARNING per malformed result.
import logging
# typing.Any: stdlib. used for the TRAPI message blocks, which are
# nested dicts whose shape is defined by the TRAPI spec rather than
# by us.
from typing import Any


# ---- ranking tables (lower rank = stronger / kept first) ----

# `biolink:knowledge_level` rank. spec §4.5. anything not in this
# table — including the attribute being absent entirely — falls
# through to rank 6.
_KL_RANK: dict[str, int] = {
    "knowledge_assertion":    0,
    "logical_entailment":     1,
    "prediction":             2,
    "statistical_association": 3,
    "observation":            4,
    "not_provided":           5,
}
_KL_RANK_UNKNOWN = 6
# rank used when the kl attribute is absent entirely (degrades to
# "not_provided", per spec §4.5).
_KL_RANK_ABSENT = 5

# `biolink:agent_type` rank. spec §4.5. same scheme as above.
_AT_RANK: dict[str, int] = {
    "manual_agent":       0,
    "automated_agent":    1,
    "text_mining_agent":  2,
    "computational_model": 3,
    "not_provided":       4,
}
_AT_RANK_UNKNOWN = 5
_AT_RANK_ABSENT = 4

# the strategy label that lands in the artifact. constant in v1; would
# change here if we ever introduced a Strategy-C or hybrid variant.
_STRATEGY_NAME = "B"

# TRAPI attribute_type_id constants. centralised so a typo fails at
# this file rather than silently mis-attributing every edge.
_ATTR_KNOWLEDGE_LEVEL = "biolink:knowledge_level"
_ATTR_AGENT_TYPE = "biolink:agent_type"
_ATTR_PUBLICATIONS = "biolink:publications"
_ATTR_SUPPORT_GRAPHS = "biolink:support_graphs"


@dataclass(frozen=True)
class ReductionMetadata:
    # bookkeeping for the artifact + downstream faithfulness eval.
    # all counts refer to TRAPI 1.5 `results`, `knowledge_graph.edges`,
    # and `knowledge_graph.nodes` respectively.
    strategy_applied: str
    top_n_per_predicate: int
    original_result_count: int
    original_edge_count: int
    original_node_count: int
    reduced_result_count: int
    reduced_edge_count: int
    reduced_node_count: int
    predicate_groups: list[str]
    edges_kept_per_group: dict[str, int]
    edges_dropped_per_group: dict[str, int]


@dataclass(frozen=True)
class ReductionResult:
    reduced_body: dict[str, Any]
    metadata: ReductionMetadata


# ---- internal: per-result enrichment ----

@dataclass(frozen=True)
class _Enriched:
    # everything reduce_plover_response needs to know about one result
    # after it has been validated and scored. kept private to the
    # module — the only public types are ReductionResult / Metadata.
    #
    # bound_edge_ids holds EVERY edge id the result binds (across all
    # analyses + binding keys), in insertion order. representative_edge_id
    # is the single one with the strongest sort_key — used for predicate
    # grouping and as the sort key for the result itself. when the
    # result survives the top-N, ALL bound edges are retained in the
    # reduced knowledge_graph (so a result with strong + weak source
    # corroboration keeps both — the strong elevates the result, the
    # weak goes along for the ride as additional provenance).
    bound_edge_ids: tuple[str, ...]
    representative_edge_id: str
    primary_predicate: str
    sort_key: tuple[int, int, int, str]
    original: dict[str, Any]
    node_ids_in_bindings: tuple[str, ...]


def _read_attr(edge: dict[str, Any], type_id: str) -> Any:
    # walk the TRAPI attribute list looking for the first attribute
    # whose attribute_type_id matches. returns None if absent.
    # attributes is normally a list of {attribute_type_id, value, ...}
    # dicts; defensive against the whole block being missing.
    attrs = edge.get("attributes") or []
    if not isinstance(attrs, list):
        return None
    for a in attrs:
        if isinstance(a, dict) and a.get("attribute_type_id") == type_id:
            return a.get("value")
    return None


def _rank_knowledge_level(edge: dict[str, Any]) -> int:
    value = _read_attr(edge, _ATTR_KNOWLEDGE_LEVEL)
    if value is None:
        return _KL_RANK_ABSENT
    return _KL_RANK.get(str(value), _KL_RANK_UNKNOWN)


def _rank_agent_type(edge: dict[str, Any]) -> int:
    value = _read_attr(edge, _ATTR_AGENT_TYPE)
    if value is None:
        return _AT_RANK_ABSENT
    return _AT_RANK.get(str(value), _AT_RANK_UNKNOWN)


def _n_publications(edge: dict[str, Any]) -> int:
    value = _read_attr(edge, _ATTR_PUBLICATIONS)
    if isinstance(value, list):
        return len(value)
    return 0


def _all_edge_ids_in_result(result: dict[str, Any]) -> tuple[str, ...]:
    # TRAPI 1.5: edge_bindings live under analyses[i].edge_bindings as
    # {qg_edge_id: [{"id": kg_edge_id, ...}, ...]}. a single result can
    # bind MULTIPLE kg edges to the same qg edge — this happens when
    # several KG2c sources independently assert the same fact. we walk
    # the structure defensively and collect every kg edge id in
    # insertion order. an empty result is a TRAPI-malformedness signal:
    # the caller drops it with a WARNING.
    out: list[str] = []
    seen: set[str] = set()
    analyses = result.get("analyses")
    if not isinstance(analyses, list):
        return ()
    for analysis in analyses:
        if not isinstance(analysis, dict):
            continue
        edge_bindings = analysis.get("edge_bindings")
        if not isinstance(edge_bindings, dict):
            continue
        for binding_list in edge_bindings.values():
            if not isinstance(binding_list, list):
                continue
            for b in binding_list:
                if not isinstance(b, dict):
                    continue
                eid = b.get("id")
                if isinstance(eid, str) and eid and eid not in seen:
                    seen.add(eid)
                    out.append(eid)
    return tuple(out)


def _node_ids_in_bindings(result: dict[str, Any]) -> tuple[str, ...]:
    # collect every knowledge-graph node id mentioned by the result's
    # node_bindings, in insertion order. used to rebuild the reduced
    # knowledge_graph.nodes block once we know which results survived.
    out: list[str] = []
    seen: set[str] = set()
    node_bindings = result.get("node_bindings") or {}
    if not isinstance(node_bindings, dict):
        return ()
    for binding_list in node_bindings.values():
        if not isinstance(binding_list, list):
            continue
        for b in binding_list:
            if not isinstance(b, dict):
                continue
            nid = b.get("id")
            if isinstance(nid, str) and nid not in seen:
                seen.add(nid)
                out.append(nid)
    return tuple(out)


def _support_graph_ids(edge: dict[str, Any]) -> list[str]:
    # TRAPI edges that participate in auxiliary graphs declare them via
    # the biolink:support_graphs attribute, whose value is a list of
    # auxiliary_graph ids. returns [] if the attribute is missing.
    value = _read_attr(edge, _ATTR_SUPPORT_GRAPHS)
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _enrich_results(
    results: list[dict[str, Any]],
    kg_edges: dict[str, dict[str, Any]],
    logger: logging.Logger,
) -> list[_Enriched]:
    # turn raw TRAPI results into _Enriched rows ready for grouping +
    # sorting. malformed results (missing edge binding, or edge id not
    # present in kg_edges) are dropped here with a WARNING. the
    # caller's reduced_result_count thus reflects only valid rows.
    enriched: list[_Enriched] = []
    for idx, result in enumerate(results):
        if not isinstance(result, dict):
            logger.warning(
                f"reduction  result idx={idx} is not a dict; dropped"
            )
            continue
        bound = _all_edge_ids_in_result(result)
        if not bound:
            logger.warning(
                f"reduction  result idx={idx} has no resolvable "
                f"edge bindings; dropped"
            )
            continue
        # filter to only bound edges that actually exist in kg_edges.
        # in a well-formed TRAPI message all bound edges are present,
        # but a malformed message can reference an edge id that isn't
        # in the kg_edges dict.
        present = tuple(eid for eid in bound if eid in kg_edges)
        if not present:
            logger.warning(
                f"reduction  result idx={idx} references edge ids "
                f"{bound!r} none of which exist in kg_edges; dropped"
            )
            continue
        # sort tuple ordering, ascending (smaller = stronger):
        #   1. knowledge_level rank  (smaller = stronger)
        #   2. -n_publications       (more pubs = first; continuous evidence signal)
        #   3. agent_type rank       (smaller = stronger; binary provenance label)
        #   4. edge_id alphabetical  (full determinism tie-break)
        #
        # n_pubs is promoted ahead of agent_type because publication
        # count is a continuous corroboration signal across sources,
        # whereas agent_type is a binary label about extraction
        # provenance. when knowledge_level ties across a whole
        # predicate group (as it does for KG2c gene-disease edges
        # where every edge is kl=prediction), letting agent_type
        # outrank n_pubs drops well-cited text_mining_agent edges
        # below single-PMID automated_agent edges. see the spec at
        # docs/specs/response-reduction-strategy-b.md §4.5.
        #
        # multi-binding handling: a single result can bind multiple
        # kg edges (one per source asserting the same fact). we score
        # the result by the STRONGEST (min sort_key) of its bound
        # edges. that lets multi-source corroboration help: one
        # high-quality source elevates the whole result, even if the
        # other bindings are weak. all bound edges are retained in
        # the reduced kg_edges if the result survives the top-N.
        def edge_sort_key(eid: str) -> tuple[int, int, int, str]:
            e = kg_edges[eid]
            return (
                _rank_knowledge_level(e),
                -_n_publications(e),
                _rank_agent_type(e),
                eid,
            )
        representative_eid = min(present, key=edge_sort_key)
        rep_edge = kg_edges[representative_eid]
        # predicate is the grouping key. missing predicates land in
        # "<unknown>" so a malformed TRAPI message degrades to "all
        # edges in one bucket" rather than crashing.
        predicate = rep_edge.get("predicate")
        if not isinstance(predicate, str) or not predicate:
            predicate = "<unknown>"
        enriched.append(
            _Enriched(
                bound_edge_ids=present,
                representative_edge_id=representative_eid,
                primary_predicate=predicate,
                sort_key=edge_sort_key(representative_eid),
                original=result,
                node_ids_in_bindings=_node_ids_in_bindings(result),
            )
        )
    return enriched


def reduce_plover_response(
    plover_body: dict[str, Any],
    *,
    top_n_per_predicate: int,
    logger: logging.Logger,
) -> ReductionResult:
    # entry point. see docs/specs/response-reduction-strategy-b.md.
    # this is a pure function: it does not mutate plover_body, does
    # not write to disk, and does not call any network service.

    # boundary guards: programmer-error cases. callers MUST pass a
    # dict body and a positive top-N; anything else is a bug worth
    # surfacing at the call site, not a runtime degradation we silently
    # paper over.
    if not isinstance(plover_body, dict):
        raise TypeError(
            f"plover_body must be a dict (got {type(plover_body).__name__})"
        )
    if top_n_per_predicate < 1:
        raise ValueError(
            f"top_n_per_predicate must be >= 1 (got {top_n_per_predicate!r})"
        )

    message = plover_body.get("message")
    if not isinstance(message, dict):
        message = {}
    raw_results = message.get("results") or []
    if not isinstance(raw_results, list):
        raw_results = []
    kg = message.get("knowledge_graph") or {}
    if not isinstance(kg, dict):
        kg = {}
    kg_nodes_in: dict[str, Any] = kg.get("nodes") or {}
    if not isinstance(kg_nodes_in, dict):
        kg_nodes_in = {}
    kg_edges_in: dict[str, dict[str, Any]] = kg.get("edges") or {}
    if not isinstance(kg_edges_in, dict):
        kg_edges_in = {}
    aux_graphs_in: dict[str, Any] = message.get("auxiliary_graphs") or {}
    if not isinstance(aux_graphs_in, dict):
        aux_graphs_in = {}
    query_graph = message.get("query_graph") or {"nodes": {}, "edges": {}}

    original_result_count = len(raw_results)
    original_edge_count = len(kg_edges_in)
    original_node_count = len(kg_nodes_in)

    # no-op fast paths. either case means "nothing to score, return
    # the body unchanged and record matching counts". strategy is
    # still recorded as "B" so the artifact shape stays uniform across
    # runs (some runs being labelled "B" and others "no-op" would
    # complicate the eval harness for no benefit).
    if not raw_results or not kg_edges_in:
        if raw_results and not kg_edges_in:
            # results exist but reference no kg.edges — TRAPI-malformed
            # at the source. one WARNING is enough; per-result warnings
            # would be noisy.
            logger.warning(
                "reduction  results present but knowledge_graph.edges "
                "is empty; returning body unchanged"
            )
        metadata = ReductionMetadata(
            strategy_applied=_STRATEGY_NAME,
            top_n_per_predicate=top_n_per_predicate,
            original_result_count=original_result_count,
            original_edge_count=original_edge_count,
            original_node_count=original_node_count,
            reduced_result_count=0 if not raw_results else 0,
            reduced_edge_count=0,
            reduced_node_count=0,
            predicate_groups=[],
            edges_kept_per_group={},
            edges_dropped_per_group={},
        )
        # echo the body verbatim. callers MUST treat the returned dict
        # as read-only; we don't deepcopy here because the function is
        # documented as not mutating its input.
        return ReductionResult(reduced_body=plover_body, metadata=metadata)

    # enrich + group
    enriched = _enrich_results(raw_results, kg_edges_in, logger)
    groups: dict[str, list[_Enriched]] = {}
    for row in enriched:
        groups.setdefault(row.primary_predicate, []).append(row)

    # sort each group by the 4-key tuple ascending (stronger first),
    # then take top-N. predicate_groups is iterated in alphabetical
    # order so the reduced_body's byte ordering is stable across runs
    # — important for OpenRouter prompt-cache hit rates.
    kept_rows: list[_Enriched] = []
    edges_kept_per_group: dict[str, int] = {}
    edges_dropped_per_group: dict[str, int] = {}
    predicate_groups_sorted: list[str] = sorted(groups.keys())
    for predicate in predicate_groups_sorted:
        group = sorted(groups[predicate], key=lambda r: r.sort_key)
        retained = group[: top_n_per_predicate]
        kept_rows.extend(retained)
        edges_kept_per_group[predicate] = len(retained)
        edges_dropped_per_group[predicate] = max(0, len(group) - len(retained))

    # rebuild knowledge_graph + auxiliary_graphs from the surviving
    # rows. all bound edges of each surviving result are retained (so a
    # result with multi-source corroboration keeps every source in the
    # reduced kg_edges, not just the one we scored by). edge ids are
    # deduped across results via set.
    retained_edge_ids: set[str] = set()
    for row in kept_rows:
        retained_edge_ids.update(row.bound_edge_ids)
    retained_node_ids: set[str] = set()
    for row in kept_rows:
        retained_node_ids.update(row.node_ids_in_bindings)
    retained_aux_ids: set[str] = set()
    for eid in retained_edge_ids:
        retained_aux_ids.update(_support_graph_ids(kg_edges_in[eid]))

    # keys are emitted in sorted order so the reduced body is byte-
    # stable across runs even when Python's dict insertion order would
    # have differed (e.g. across interpreter restarts with different
    # hash seeds — Python 3.7+ preserves order but the SOURCE order
    # came from PloverDB and is itself nondeterministic).
    reduced_kg_nodes = {
        nid: kg_nodes_in[nid]
        for nid in sorted(retained_node_ids)
        if nid in kg_nodes_in
    }
    reduced_kg_edges = {
        eid: kg_edges_in[eid] for eid in sorted(retained_edge_ids)
    }
    reduced_aux_graphs = {
        gid: aux_graphs_in[gid]
        for gid in sorted(retained_aux_ids)
        if gid in aux_graphs_in
    }
    # results keep their group-ordered insertion order (sorted groups,
    # each group sorted by sort_key) so the LLM sees the strongest
    # evidence FIRST inside each predicate block.
    reduced_results = [row.original for row in kept_rows]

    reduced_message: dict[str, Any] = {
        **message,
        "query_graph": query_graph,
        "knowledge_graph": {
            "nodes": reduced_kg_nodes,
            "edges": reduced_kg_edges,
        },
        "results": reduced_results,
        "auxiliary_graphs": reduced_aux_graphs,
    }
    reduced_body: dict[str, Any] = {**plover_body, "message": reduced_message}

    metadata = ReductionMetadata(
        strategy_applied=_STRATEGY_NAME,
        top_n_per_predicate=top_n_per_predicate,
        original_result_count=original_result_count,
        original_edge_count=original_edge_count,
        original_node_count=original_node_count,
        reduced_result_count=len(reduced_results),
        reduced_edge_count=len(reduced_kg_edges),
        reduced_node_count=len(reduced_kg_nodes),
        predicate_groups=predicate_groups_sorted,
        edges_kept_per_group=edges_kept_per_group,
        edges_dropped_per_group=edges_dropped_per_group,
    )

    logger.info(
        f"[bold cyan]→ reduction[/]  strategy={_STRATEGY_NAME}  "
        f"predicates={len(predicate_groups_sorted)}  "
        f"results={original_result_count}→{len(reduced_results)}  "
        f"edges={original_edge_count}→{len(reduced_kg_edges)}  "
        f"nodes={original_node_count}→{len(reduced_kg_nodes)}  "
        f"top_n={top_n_per_predicate}"
    )

    return ReductionResult(reduced_body=reduced_body, metadata=metadata)
