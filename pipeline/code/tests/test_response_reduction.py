# Strategy B response reduction.
#
# the function takes a TRAPI 1.5 PloverDB body and returns a reduced
# body containing only the top-N results per predicate group, ranked
# by (knowledge_level, agent_type, n_publications, edge_id). full
# spec: docs/specs/response-reduction-strategy-b.md.
#
# everything here is a pure-function test against fabricated TRAPI
# fragments. no network, no fixtures from disk. the fragments use the
# minimum TRAPI 1.5 shape needed to exercise one behaviour per test.

import logging

import pytest

from code.reduction import (
    ReductionMetadata,
    ReductionResult,
    reduce_plover_response,
)


# ---------- helpers: minimal TRAPI 1.5 fixture builders ----------

def _silent_logger() -> logging.Logger:
    # tests never assert on log output; route everything to a NullHandler
    # so test runs stay clean and pytest's caplog can still intercept
    # when an individual test wants to.
    log = logging.getLogger("test_reduction")
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


def _attr(type_id: str, value: object) -> dict[str, object]:
    return {"attribute_type_id": type_id, "value": value}


def _edge(
    *,
    subject: str = "CHEBI:6801",
    object_: str = "MONDO:0005148",
    predicate: str = "biolink:treats",
    knowledge_level: str | None = "knowledge_assertion",
    agent_type: str | None = "manual_agent",
    pubs: list[str] | None = None,
    support_graphs: list[str] | None = None,
) -> dict[str, object]:
    # None for kl/at means "attribute deliberately absent" (tests
    # the missing-attribute degradation path). empty-list pubs means
    # "attribute present but value=[]" (still 0 publications).
    attrs: list[dict[str, object]] = []
    if knowledge_level is not None:
        attrs.append(_attr("biolink:knowledge_level", knowledge_level))
    if agent_type is not None:
        attrs.append(_attr("biolink:agent_type", agent_type))
    if pubs is not None:
        attrs.append(_attr("biolink:publications", pubs))
    if support_graphs is not None:
        attrs.append(_attr("biolink:support_graphs", support_graphs))
    return {
        "subject": subject,
        "object": object_,
        "predicate": predicate,
        "attributes": attrs,
    }


def _result(
    edge_id: str,
    subj_id: str,
    obj_id: str,
    *,
    subj_qg: str = "n0",
    obj_qg: str = "n1",
    edge_qg: str = "e0",
    resource_id: str = "infores:rtx-kg2",
) -> dict[str, object]:
    # TRAPI 1.5 puts edge_bindings under analyses[i].edge_bindings,
    # not directly on the result. one analysis, one binding, one
    # edge — matches the one-hop pipeline contract.
    return {
        "node_bindings": {
            subj_qg: [{"id": subj_id}],
            obj_qg: [{"id": obj_id}],
        },
        "analyses": [
            {
                "resource_id": resource_id,
                "edge_bindings": {edge_qg: [{"id": edge_id}]},
            },
        ],
    }


def _body(
    *,
    edges: dict[str, dict[str, object]] | None = None,
    nodes: dict[str, dict[str, object]] | None = None,
    results: list[dict[str, object]] | None = None,
    aux_graphs: dict[str, object] | None = None,
    query_graph: dict[str, object] | None = None,
    extra_top_level: dict[str, object] | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "query_graph": query_graph or {"nodes": {}, "edges": {}},
        "knowledge_graph": {
            "nodes": nodes or {},
            "edges": edges or {},
        },
        "results": results or [],
        "auxiliary_graphs": aux_graphs or {},
    }
    body: dict[str, object] = {"message": message}
    if extra_top_level:
        body.update(extra_top_level)
    return body


def _reduce(body: dict[str, object], top_n: int = 10) -> ReductionResult:
    return reduce_plover_response(
        body,
        top_n_per_predicate=top_n,
        logger=_silent_logger(),
    )


# ============================================================
# happy-path tests
# ============================================================

def test_no_op_when_results_empty():
    body = _body(results=[], edges={}, nodes={})
    out = _reduce(body)
    assert out.metadata.strategy_applied == "B"
    assert out.metadata.original_result_count == 0
    assert out.metadata.reduced_result_count == 0
    assert out.reduced_body["message"]["results"] == []


def test_no_op_when_edges_empty(caplog):
    # results reference edges that don't exist; reduction should
    # return body unchanged and log a single WARNING.
    body = _body(
        results=[_result("edge1", "CHEBI:6801", "MONDO:0005148")],
        edges={},
        nodes={"CHEBI:6801": {}, "MONDO:0005148": {}},
    )
    with caplog.at_level(logging.WARNING, logger="test_reduction"):
        out = _reduce(body)
    assert out.metadata.strategy_applied == "B"
    # malformed-result dropping: 1 result in, 0 results out
    assert out.metadata.reduced_result_count == 0


def test_single_predicate_single_result_keeps_one():
    body = _body(
        results=[_result("edge1", "CHEBI:6801", "MONDO:0005148")],
        edges={"edge1": _edge()},
        nodes={"CHEBI:6801": {}, "MONDO:0005148": {}},
    )
    out = _reduce(body, top_n=10)
    assert out.metadata.reduced_result_count == 1
    assert out.metadata.reduced_edge_count == 1
    assert "edge1" in out.reduced_body["message"]["knowledge_graph"]["edges"]


def test_single_predicate_many_results_keeps_top_n():
    # 15 results, all on biolink:treats. top_n=10 keeps 10.
    edges = {f"edge{i}": _edge() for i in range(15)}
    results = [
        _result(f"edge{i}", "CHEBI:6801", "MONDO:0005148") for i in range(15)
    ]
    body = _body(
        results=results,
        edges=edges,
        nodes={"CHEBI:6801": {}, "MONDO:0005148": {}},
    )
    out = _reduce(body, top_n=10)
    assert out.metadata.original_result_count == 15
    assert out.metadata.reduced_result_count == 10
    assert out.metadata.edges_dropped_per_group["biolink:treats"] == 5


def test_multiple_predicates_each_group_capped():
    # 8 treats + 7 causes; top_n=5 → keeps 5 of each.
    edges = {}
    results = []
    for i in range(8):
        edges[f"t{i}"] = _edge(predicate="biolink:treats")
        results.append(_result(f"t{i}", "CHEBI:6801", "MONDO:0005148"))
    for i in range(7):
        edges[f"c{i}"] = _edge(predicate="biolink:causes")
        results.append(_result(f"c{i}", "CHEBI:6801", "MONDO:0005148"))
    body = _body(
        results=results,
        edges=edges,
        nodes={"CHEBI:6801": {}, "MONDO:0005148": {}},
    )
    out = _reduce(body, top_n=5)
    assert out.metadata.reduced_result_count == 10
    assert out.metadata.edges_kept_per_group["biolink:treats"] == 5
    assert out.metadata.edges_kept_per_group["biolink:causes"] == 5


def test_predicates_iterated_in_sorted_order():
    # predicate_groups in metadata must come out alphabetically so the
    # reduced body is byte-stable across runs (prompt caching).
    edges = {
        "z1": _edge(predicate="biolink:treats"),
        "a1": _edge(predicate="biolink:causes"),
        "m1": _edge(predicate="biolink:interacts_with"),
    }
    results = [
        _result("z1", "CHEBI:6801", "MONDO:0005148"),
        _result("a1", "CHEBI:6801", "MONDO:0005148"),
        _result("m1", "CHEBI:6801", "MONDO:0005148"),
    ]
    body = _body(
        results=results,
        edges=edges,
        nodes={"CHEBI:6801": {}, "MONDO:0005148": {}},
    )
    out = _reduce(body, top_n=10)
    assert out.metadata.predicate_groups == [
        "biolink:causes",
        "biolink:interacts_with",
        "biolink:treats",
    ]


def test_attributes_preserved_for_retained_edges():
    # full attribute round-trip: pubs, kl, at, and one unknown attr all
    # survive the reduction verbatim on a retained edge.
    custom_attr = _attr("biolink:supporting_text", {"PMID:1": {"sentence": "x"}})
    edge_in = {
        "subject": "CHEBI:6801",
        "object": "MONDO:0005148",
        "predicate": "biolink:treats",
        "attributes": [
            _attr("biolink:knowledge_level", "knowledge_assertion"),
            _attr("biolink:agent_type", "manual_agent"),
            _attr("biolink:publications", ["PMID:1", "PMID:2"]),
            custom_attr,
        ],
    }
    body = _body(
        results=[_result("e1", "CHEBI:6801", "MONDO:0005148")],
        edges={"e1": edge_in},
        nodes={"CHEBI:6801": {}, "MONDO:0005148": {}},
    )
    out = _reduce(body, top_n=10)
    edge_out = out.reduced_body["message"]["knowledge_graph"]["edges"]["e1"]
    assert edge_out["attributes"] == edge_in["attributes"]


# ============================================================
# ranking tests
# ============================================================

def test_knowledge_assertion_beats_prediction():
    # both edges share predicate + agent_type + n_pubs; kl decides.
    body = _body(
        results=[
            _result("strong", "S", "O"),
            _result("weak", "S", "O"),
        ],
        edges={
            "strong": _edge(knowledge_level="knowledge_assertion"),
            "weak": _edge(knowledge_level="prediction"),
        },
        nodes={"S": {}, "O": {}},
    )
    out = _reduce(body, top_n=1)
    kept = list(out.reduced_body["message"]["knowledge_graph"]["edges"].keys())
    assert kept == ["strong"]


def test_manual_agent_beats_text_mining_agent_within_same_kl():
    body = _body(
        results=[
            _result("manual", "S", "O"),
            _result("nlp", "S", "O"),
        ],
        edges={
            "manual": _edge(agent_type="manual_agent"),
            "nlp": _edge(agent_type="text_mining_agent"),
        },
        nodes={"S": {}, "O": {}},
    )
    out = _reduce(body, top_n=1)
    kept = list(out.reduced_body["message"]["knowledge_graph"]["edges"].keys())
    assert kept == ["manual"]


def test_more_publications_beats_fewer_within_same_kl_and_at():
    # same kl + at; n_pubs is the third sort key, descending. the
    # edge with more PMIDs wins.
    body = _body(
        results=[
            _result("well_cited", "S", "O"),
            _result("under_cited", "S", "O"),
        ],
        edges={
            "well_cited": _edge(pubs=["PMID:1", "PMID:2", "PMID:3"]),
            "under_cited": _edge(pubs=["PMID:9"]),
        },
        nodes={"S": {}, "O": {}},
    )
    out = _reduce(body, top_n=1)
    kept = list(out.reduced_body["message"]["knowledge_graph"]["edges"].keys())
    assert kept == ["well_cited"]


def test_edge_id_alphabetical_breaks_total_ties():
    # identical kl, at, n_pubs; edge id is the final tiebreaker. "a"
    # comes before "b" alphabetically.
    body = _body(
        results=[
            _result("b_edge", "S", "O"),
            _result("a_edge", "S", "O"),
        ],
        edges={
            "b_edge": _edge(pubs=["PMID:1"]),
            "a_edge": _edge(pubs=["PMID:1"]),
        },
        nodes={"S": {}, "O": {}},
    )
    out = _reduce(body, top_n=1)
    kept = list(out.reduced_body["message"]["knowledge_graph"]["edges"].keys())
    assert kept == ["a_edge"]


def test_missing_kl_attribute_treated_as_not_provided():
    # edge with kl explicitly absent should rank below kl="observation"
    # (rank 5 vs rank 4). top_n=1 keeps observation.
    body = _body(
        results=[
            _result("absent_kl", "S", "O"),
            _result("observation", "S", "O"),
        ],
        edges={
            "absent_kl": _edge(knowledge_level=None),
            "observation": _edge(knowledge_level="observation"),
        },
        nodes={"S": {}, "O": {}},
    )
    out = _reduce(body, top_n=1)
    kept = list(out.reduced_body["message"]["knowledge_graph"]["edges"].keys())
    assert kept == ["observation"]


def test_unknown_kl_value_ranks_last():
    # a kl value not in the known hierarchy (rank 6) ranks below the
    # explicit "not_provided" value (rank 5).
    body = _body(
        results=[
            _result("weird", "S", "O"),
            _result("not_provided", "S", "O"),
        ],
        edges={
            "weird": _edge(knowledge_level="quantum_truth"),
            "not_provided": _edge(knowledge_level="not_provided"),
        },
        nodes={"S": {}, "O": {}},
    )
    out = _reduce(body, top_n=1)
    kept = list(out.reduced_body["message"]["knowledge_graph"]["edges"].keys())
    assert kept == ["not_provided"]


# ============================================================
# graph rebuild tests
# ============================================================

def test_kg_nodes_filtered_to_referenced_only():
    # 4 nodes in the body; only 2 are referenced by surviving results;
    # the orphan 2 must be dropped from the reduced nodes block.
    body = _body(
        results=[_result("e1", "A", "B")],
        edges={"e1": _edge(subject="A", object_="B")},
        nodes={
            "A": {"name": "node A"},
            "B": {"name": "node B"},
            "ORPHAN1": {"name": "should be dropped"},
            "ORPHAN2": {"name": "also should be dropped"},
        },
    )
    out = _reduce(body, top_n=10)
    kept_nodes = out.reduced_body["message"]["knowledge_graph"]["nodes"]
    assert set(kept_nodes.keys()) == {"A", "B"}


def test_kg_edges_filtered_to_retained_only():
    # body has 4 edges in kg.edges; only 2 are referenced by results;
    # the other 2 (no result binding) must be dropped.
    edges = {
        "e_bound_1": _edge(predicate="biolink:treats"),
        "e_bound_2": _edge(predicate="biolink:causes"),
        "e_orphan_1": _edge(predicate="biolink:treats"),
        "e_orphan_2": _edge(predicate="biolink:causes"),
    }
    body = _body(
        results=[
            _result("e_bound_1", "S", "O"),
            _result("e_bound_2", "S", "O"),
        ],
        edges=edges,
        nodes={"S": {}, "O": {}},
    )
    out = _reduce(body, top_n=10)
    kept_edges = out.reduced_body["message"]["knowledge_graph"]["edges"]
    assert set(kept_edges.keys()) == {"e_bound_1", "e_bound_2"}


def test_auxiliary_graphs_filtered_to_referenced_only():
    # retained edge references support graph "ag1"; dropped edge
    # references "ag2". reduced auxiliary_graphs must contain only
    # "ag1".
    body = _body(
        results=[_result("retained", "S", "O")],
        edges={
            "retained": _edge(support_graphs=["ag1"]),
        },
        nodes={"S": {}, "O": {}},
        aux_graphs={
            "ag1": {"edges": ["x", "y"]},
            "ag2": {"edges": ["z"]},
        },
    )
    out = _reduce(body, top_n=10)
    kept_aux = out.reduced_body["message"]["auxiliary_graphs"]
    assert set(kept_aux.keys()) == {"ag1"}


def test_query_graph_passthrough_unchanged():
    qg = {
        "nodes": {"n0": {"ids": ["CHEBI:6801"]}, "n1": {"categories": ["biolink:Disease"]}},
        "edges": {"e0": {"subject": "n0", "object": "n1", "predicates": ["biolink:treats"]}},
    }
    body = _body(
        results=[_result("e1", "CHEBI:6801", "MONDO:0005148")],
        edges={"e1": _edge()},
        nodes={"CHEBI:6801": {}, "MONDO:0005148": {}},
        query_graph=qg,
    )
    out = _reduce(body, top_n=10)
    assert out.reduced_body["message"]["query_graph"] == qg


def test_top_level_fields_outside_message_preserved():
    # workflow / schema_version live at the top level of the body, not
    # inside .message — they must round-trip untouched.
    body = _body(
        results=[_result("e1", "S", "O")],
        edges={"e1": _edge()},
        nodes={"S": {}, "O": {}},
        extra_top_level={"workflow": [{"id": "lookup"}], "schema_version": "1.5.0"},
    )
    out = _reduce(body, top_n=10)
    assert out.reduced_body["workflow"] == [{"id": "lookup"}]
    assert out.reduced_body["schema_version"] == "1.5.0"


# ============================================================
# malformed-input tests
# ============================================================

def test_result_with_missing_primary_edge_dropped_with_warning(caplog):
    # one valid result, one result pointing at a non-existent edge.
    # only the valid one survives; the orphan logs a WARNING.
    body = _body(
        results=[
            _result("valid", "S", "O"),
            _result("ghost_edge", "S", "O"),
        ],
        edges={"valid": _edge()},
        nodes={"S": {}, "O": {}},
    )
    with caplog.at_level(logging.WARNING, logger="test_reduction"):
        out = _reduce(body, top_n=10)
    assert out.metadata.reduced_result_count == 1
    kept = list(out.reduced_body["message"]["knowledge_graph"]["edges"].keys())
    assert kept == ["valid"]


def test_two_results_same_edge_both_scored_independently():
    # results sharing the same primary edge id are both ranked. when
    # top_n=2 both survive; the deduped edge appears exactly once in
    # the reduced edges dict.
    body = _body(
        results=[
            _result("shared", "S", "O", subj_qg="n0", obj_qg="n1"),
            _result("shared", "S", "O", subj_qg="n0", obj_qg="n1"),
        ],
        edges={"shared": _edge()},
        nodes={"S": {}, "O": {}},
    )
    out = _reduce(body, top_n=2)
    assert out.metadata.reduced_result_count == 2
    assert list(out.reduced_body["message"]["knowledge_graph"]["edges"].keys()) == [
        "shared"
    ]


def test_zero_top_n_raises_value_error():
    body = _body(
        results=[_result("e1", "S", "O")],
        edges={"e1": _edge()},
        nodes={"S": {}, "O": {}},
    )
    with pytest.raises(ValueError):
        _reduce(body, top_n=0)


def test_non_dict_body_raises_type_error():
    with pytest.raises(TypeError):
        reduce_plover_response(
            "definitely not a TRAPI body",
            top_n_per_predicate=10,
            logger=_silent_logger(),
        )


# ============================================================
# metadata tests
# ============================================================

def test_metadata_counts_consistent_before_and_after():
    # 12 results, all on biolink:treats, 12 unique edges, top_n=5.
    edges = {f"e{i}": _edge() for i in range(12)}
    results = [_result(f"e{i}", "S", "O") for i in range(12)]
    body = _body(
        results=results,
        edges=edges,
        nodes={"S": {}, "O": {}},
    )
    out = _reduce(body, top_n=5)
    m = out.metadata
    assert m.original_result_count == 12
    assert m.original_edge_count == 12
    assert m.reduced_result_count == 5
    assert m.reduced_edge_count == 5
    # nodes are S and O regardless of reduction depth
    assert m.original_node_count == 2
    assert m.reduced_node_count == 2


def test_edges_dropped_per_group_sums_to_total_dropped():
    # 8 treats + 7 causes, top_n=3. expect 5 dropped from treats,
    # 4 dropped from causes; total dropped = 9. reduced count = 6.
    edges = {}
    results = []
    for i in range(8):
        edges[f"t{i}"] = _edge(predicate="biolink:treats")
        results.append(_result(f"t{i}", "S", "O"))
    for i in range(7):
        edges[f"c{i}"] = _edge(predicate="biolink:causes")
        results.append(_result(f"c{i}", "S", "O"))
    body = _body(
        results=results,
        edges=edges,
        nodes={"S": {}, "O": {}},
    )
    out = _reduce(body, top_n=3)
    dropped = out.metadata.edges_dropped_per_group
    assert dropped["biolink:treats"] == 5
    assert dropped["biolink:causes"] == 4
    assert sum(dropped.values()) == 9
    assert out.metadata.reduced_result_count == 6


def test_strategy_field_always_B():
    # the strategy_applied field is a constant in v1; verify it on a
    # trivial input so a future regression that flips it to None or
    # "" or "unknown" is caught immediately.
    body = _body(results=[], edges={}, nodes={})
    out = _reduce(body)
    assert isinstance(out, ReductionResult)
    assert isinstance(out.metadata, ReductionMetadata)
    assert out.metadata.strategy_applied == "B"
