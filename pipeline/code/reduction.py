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
# json: stdlib. used only to estimate a chunk's token budget by measuring
# the serialised character count of its sub-body.
import json
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

# text-mining provenance. SemMedDB and similar text-mined sources are the
# most abundant edges in KG2c but the noisiest; we DEMOTE every text-mined
# edge below every curated edge (source_tier is the LEADING sort key) so a
# well-cited text-mined edge can no longer dominate a curated one. an edge
# is text-mined when its agent_type is text_mining_agent OR its primary
# knowledge source is a known text-mining infores.
_TEXT_MINING_AGENT = "text_mining_agent"
_TEXT_MINING_SOURCES: frozenset[str] = frozenset({
    "infores:semmeddb",
    "infores:text-mining-provider-targeted",
})

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
    sort_key: tuple[int, int, int, int, str]
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


def _primary_knowledge_source(edge: dict[str, Any]) -> str | None:
    # TRAPI edges declare provenance via `sources` (a list of
    # {resource_id, resource_role}); the primary source is the entry whose
    # role is "primary_knowledge_source". returns None if absent.
    sources = edge.get("sources")
    if not isinstance(sources, list):
        return None
    for source in sources:
        if isinstance(source, dict) and source.get("resource_role") == "primary_knowledge_source":
            resource_id = source.get("resource_id")
            return resource_id if isinstance(resource_id, str) else None
    return None


def _source_tier(edge: dict[str, Any]) -> int:
    # 0 = curated, 1 = text-mined (demoted). leading key in the sort so
    # text-mined edges sink below ALL curated edges within a predicate
    # group, regardless of publication count.
    if _read_attr(edge, _ATTR_AGENT_TYPE) == _TEXT_MINING_AGENT:
        return 1
    return 1 if _primary_knowledge_source(edge) in _TEXT_MINING_SOURCES else 0


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
        #   1. source_tier           (0 curated, 1 text-mined → text-mined last)
        #   2. knowledge_level rank  (smaller = stronger)
        #   3. -n_publications       (more pubs = first; continuous evidence signal)
        #   4. agent_type rank       (smaller = stronger; binary provenance label)
        #   5. edge_id alphabetical  (full determinism tie-break)
        #
        # source_tier is the LEADING key: SemMedDB-style text-mined edges
        # are abundant in KG2c but noisy, so every text-mined edge sinks
        # below every curated edge regardless of pub count — a well-cited
        # text-mined edge can no longer dominate a curated one. within a
        # tier, n_pubs is promoted ahead of agent_type because publication
        # count is a continuous corroboration signal whereas agent_type is
        # a binary provenance label.
        #
        # multi-binding handling: a single result can bind multiple
        # kg edges (one per source asserting the same fact). we score
        # the result by the STRONGEST (min sort_key) of its bound
        # edges. that lets multi-source corroboration help: one
        # high-quality source elevates the whole result, even if the
        # other bindings are weak. all bound edges are retained in
        # the reduced kg_edges if the result survives the top-N.
        def edge_sort_key(eid: str) -> tuple[int, int, int, int, str]:
            e = kg_edges[eid]
            return (
                _source_tier(e),
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


@dataclass(frozen=True)
class _ParsedBody:
    # the TRAPI sub-blocks pulled out of a PloverDB body once, so the
    # single-shot reducer and the chunker parse identically.
    message: dict[str, Any]
    raw_results: list[Any]
    kg_nodes: dict[str, Any]
    kg_edges: dict[str, dict[str, Any]]
    aux_graphs: dict[str, Any]
    query_graph: dict[str, Any]
    original_result_count: int
    original_edge_count: int
    original_node_count: int


def _parse_body(plover_body: dict[str, Any]) -> _ParsedBody:
    # defensive extraction: any block may be missing or the wrong type in a
    # malformed message, so each falls back to an empty container.
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
    return _ParsedBody(
        message=message,
        raw_results=raw_results,
        kg_nodes=kg_nodes_in,
        kg_edges=kg_edges_in,
        aux_graphs=aux_graphs_in,
        query_graph=query_graph,
        original_result_count=len(raw_results),
        original_edge_count=len(kg_edges_in),
        original_node_count=len(kg_nodes_in),
    )


def _sorted_groups(
    raw_results: list[Any],
    kg_edges: dict[str, dict[str, Any]],
    logger: logging.Logger,
) -> tuple[dict[str, list[_Enriched]], list[str], list[_Enriched]]:
    # enrich + group by predicate + sort each group by sort_key (strongest
    # first), WITHOUT truncating. this is the un-fused "sort" half: the
    # single-shot reducer then takes top-N per group, while the chunker
    # partitions the flat ordered list by token budget. predicate groups
    # are iterated alphabetically so the byte ordering is stable.
    enriched = _enrich_results(raw_results, kg_edges, logger)
    groups: dict[str, list[_Enriched]] = {}
    for row in enriched:
        groups.setdefault(row.primary_predicate, []).append(row)
    predicate_groups_sorted = sorted(groups.keys())
    ordered_rows: list[_Enriched] = []
    for predicate in predicate_groups_sorted:
        groups[predicate] = sorted(groups[predicate], key=lambda r: r.sort_key)
        ordered_rows.extend(groups[predicate])
    return groups, predicate_groups_sorted, ordered_rows


def _rebuild_body(
    rows: list[_Enriched],
    parsed: _ParsedBody,
    plover_body: dict[str, Any],
) -> dict[str, Any]:
    # rebuild a valid TRAPI body containing ONLY the nodes/edges/aux the
    # given result rows reference. ALL bound edges of each row are retained
    # (multi-source corroboration kept). keys emitted in sorted order so the
    # body is byte-stable. shared by the single-shot reducer (top-N rows)
    # and the chunker (per-chunk rows) — so a chunk reads exactly like a
    # full reduced body.
    retained_edge_ids: set[str] = set()
    for row in rows:
        retained_edge_ids.update(row.bound_edge_ids)
    retained_node_ids: set[str] = set()
    for row in rows:
        retained_node_ids.update(row.node_ids_in_bindings)
    retained_aux_ids: set[str] = set()
    for eid in retained_edge_ids:
        retained_aux_ids.update(_support_graph_ids(parsed.kg_edges[eid]))
    reduced_kg_nodes = {
        nid: parsed.kg_nodes[nid]
        for nid in sorted(retained_node_ids)
        if nid in parsed.kg_nodes
    }
    reduced_kg_edges = {
        eid: parsed.kg_edges[eid] for eid in sorted(retained_edge_ids)
    }
    reduced_aux_graphs = {
        gid: parsed.aux_graphs[gid]
        for gid in sorted(retained_aux_ids)
        if gid in parsed.aux_graphs
    }
    reduced_message: dict[str, Any] = {
        **parsed.message,
        "query_graph": parsed.query_graph,
        "knowledge_graph": {
            "nodes": reduced_kg_nodes,
            "edges": reduced_kg_edges,
        },
        "results": [row.original for row in rows],
        "auxiliary_graphs": reduced_aux_graphs,
    }
    return {**plover_body, "message": reduced_message}


def reduce_plover_response(
    plover_body: dict[str, Any],
    *,
    top_n_per_predicate: int,
    logger: logging.Logger,
) -> ReductionResult:
    # single-shot entry point. see docs/specs/response-reduction-strategy-b.md.
    # pure function: does not mutate plover_body, write to disk, or call any
    # network service. sort + truncate are now un-fused (_sorted_groups does
    # the sort; this function does the top-N truncation) so the chunker can
    # consume the same sorted ranking without truncation.

    # boundary guards: programmer-error cases. callers MUST pass a dict body
    # and a positive top-N; anything else is a bug worth surfacing at the
    # call site, not a runtime degradation we silently paper over.
    if not isinstance(plover_body, dict):
        raise TypeError(
            f"plover_body must be a dict (got {type(plover_body).__name__})"
        )
    if top_n_per_predicate < 1:
        raise ValueError(
            f"top_n_per_predicate must be >= 1 (got {top_n_per_predicate!r})"
        )

    parsed = _parse_body(plover_body)

    # no-op fast paths. either case means "nothing to score, return the body
    # unchanged and record matching counts". strategy is still recorded as
    # "B" so the artifact shape stays uniform across runs.
    if not parsed.raw_results or not parsed.kg_edges:
        if parsed.raw_results and not parsed.kg_edges:
            # results exist but reference no kg.edges — TRAPI-malformed at
            # the source. one WARNING is enough; per-result would be noisy.
            logger.warning(
                "reduction  results present but knowledge_graph.edges "
                "is empty; returning body unchanged"
            )
        metadata = ReductionMetadata(
            strategy_applied=_STRATEGY_NAME,
            top_n_per_predicate=top_n_per_predicate,
            original_result_count=parsed.original_result_count,
            original_edge_count=parsed.original_edge_count,
            original_node_count=parsed.original_node_count,
            reduced_result_count=0,
            reduced_edge_count=0,
            reduced_node_count=0,
            predicate_groups=[],
            edges_kept_per_group={},
            edges_dropped_per_group={},
        )
        # echo the body verbatim. callers MUST treat the returned dict as
        # read-only; we don't deepcopy because the function does not mutate.
        return ReductionResult(reduced_body=plover_body, metadata=metadata)

    groups, predicate_groups_sorted, _ = _sorted_groups(
        parsed.raw_results, parsed.kg_edges, logger,
    )

    # truncate top-N per (already-sorted) predicate group.
    kept_rows: list[_Enriched] = []
    edges_kept_per_group: dict[str, int] = {}
    edges_dropped_per_group: dict[str, int] = {}
    for predicate in predicate_groups_sorted:
        group = groups[predicate]
        retained = group[: top_n_per_predicate]
        kept_rows.extend(retained)
        edges_kept_per_group[predicate] = len(retained)
        edges_dropped_per_group[predicate] = max(0, len(group) - len(retained))

    reduced_body = _rebuild_body(kept_rows, parsed, plover_body)
    reduced_kg = reduced_body["message"]["knowledge_graph"]
    reduced_result_count = len(reduced_body["message"]["results"])
    reduced_edge_count = len(reduced_kg["edges"])
    reduced_node_count = len(reduced_kg["nodes"])

    metadata = ReductionMetadata(
        strategy_applied=_STRATEGY_NAME,
        top_n_per_predicate=top_n_per_predicate,
        original_result_count=parsed.original_result_count,
        original_edge_count=parsed.original_edge_count,
        original_node_count=parsed.original_node_count,
        reduced_result_count=reduced_result_count,
        reduced_edge_count=reduced_edge_count,
        reduced_node_count=reduced_node_count,
        predicate_groups=predicate_groups_sorted,
        edges_kept_per_group=edges_kept_per_group,
        edges_dropped_per_group=edges_dropped_per_group,
    )

    logger.info(
        f"[bold cyan]→ reduction[/]  strategy={_STRATEGY_NAME}  "
        f"predicates={len(predicate_groups_sorted)}  "
        f"results={parsed.original_result_count}→{reduced_result_count}  "
        f"edges={parsed.original_edge_count}→{reduced_edge_count}  "
        f"nodes={parsed.original_node_count}→{reduced_node_count}  "
        f"top_n={top_n_per_predicate}"
    )

    return ReductionResult(reduced_body=reduced_body, metadata=metadata)


_CHARS_PER_TOKEN = 4  # rough JSON+English heuristic for the chunk-budget estimate


@dataclass(frozen=True)
class ChunkSet:
    # the result of partitioning the full sorted (untruncated) ranking into
    # token-budgeted chunks. each chunk is a valid TRAPI sub-body the LLM
    # reads exactly like a full reduced body. chunk 0 holds the strongest
    # evidence (curated first, text-mined last) so an early-stopping reader
    # sees the best answers first.
    chunks: list[dict[str, Any]]
    n_chunks: int
    total_rows: int                 # sorted result rows available
    chunked_rows: int               # rows actually placed into chunks
    truncated_at_max_chunks: bool   # True if max_chunks cut off the tail


def _row_weight(row: _Enriched, parsed: _ParsedBody) -> int:
    # rough char-count proxy for a row's contribution to a chunk body: its
    # result plus its bound edges plus its bound nodes. shared nodes across
    # rows in the same chunk are double-counted, which makes the estimate an
    # UPPER bound, so chunks land at or under budget (safe).
    weight = len(json.dumps(row.original, ensure_ascii=False))
    for eid in row.bound_edge_ids:
        edge = parsed.kg_edges.get(eid)
        if edge is not None:
            weight += len(json.dumps(edge, ensure_ascii=False))
    for nid in row.node_ids_in_bindings:
        node = parsed.kg_nodes.get(nid)
        if node is not None:
            weight += len(json.dumps(node, ensure_ascii=False))
    return weight


def chunk_plover_response(
    plover_body: dict[str, Any],
    *,
    chunk_token_budget: int,
    max_chunks: int,
    logger: logging.Logger,
) -> ChunkSet:
    # partition the full sorted-but-UNTRUNCATED ranking into chunks bounded
    # by chunk_token_budget. each chunk is a valid TRAPI sub-body (built by
    # _rebuild_body) containing only the nodes/edges/aux its results
    # reference. the iterative Stage-11 loop reads chunks in order until it
    # is confident, so nothing is truncated up front — only max_chunks caps
    # the total. pure function: no IO, no network.
    if not isinstance(plover_body, dict):
        raise TypeError(
            f"plover_body must be a dict (got {type(plover_body).__name__})"
        )
    if chunk_token_budget < 1:
        raise ValueError(
            f"chunk_token_budget must be >= 1 (got {chunk_token_budget!r})"
        )
    if max_chunks < 1:
        raise ValueError(f"max_chunks must be >= 1 (got {max_chunks!r})")

    parsed = _parse_body(plover_body)
    if not parsed.raw_results or not parsed.kg_edges:
        # nothing to chunk — hand back the body as a single chunk so the
        # caller's loop runs exactly once and terminates cleanly.
        return ChunkSet(
            chunks=[plover_body], n_chunks=1, total_rows=0,
            chunked_rows=0, truncated_at_max_chunks=False,
        )

    _, _, ordered_rows = _sorted_groups(parsed.raw_results, parsed.kg_edges, logger)

    char_budget = chunk_token_budget * _CHARS_PER_TOKEN
    chunk_rows: list[list[_Enriched]] = []
    current: list[_Enriched] = []
    current_weight = 0
    for row in ordered_rows:
        weight = _row_weight(row, parsed)
        # close the current chunk before it would exceed the budget, but
        # never emit an empty chunk (a single oversized row gets its own).
        if current and current_weight + weight > char_budget:
            chunk_rows.append(current)
            current = []
            current_weight = 0
            if len(chunk_rows) >= max_chunks:
                break
        current.append(row)
        current_weight += weight
    if current and len(chunk_rows) < max_chunks:
        chunk_rows.append(current)

    chunked = sum(len(rows) for rows in chunk_rows)
    truncated = chunked < len(ordered_rows)
    if truncated:
        logger.warning(
            f"chunking  max_chunks={max_chunks} reached; "
            f"{len(ordered_rows) - chunked} of {len(ordered_rows)} sorted "
            f"results were not chunked"
        )
    chunks = [_rebuild_body(rows, parsed, plover_body) for rows in chunk_rows]
    logger.info(
        f"[bold cyan]→ chunking[/]  results={len(ordered_rows)}  "
        f"chunks={len(chunks)}  budget={chunk_token_budget}tok"
    )
    return ChunkSet(
        chunks=chunks,
        n_chunks=len(chunks),
        total_rows=len(ordered_rows),
        chunked_rows=chunked,
        truncated_at_max_chunks=truncated,
    )
