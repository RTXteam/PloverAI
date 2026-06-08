# chunk_plover_response: the iterative-mode chunking primitive.
#
# partitions the full sorted-but-untruncated ranking into token-budgeted
# valid TRAPI sub-bodies. pins: each chunk is a closed sub-body (every edge
# its results reference is present), the union of chunks covers every
# result when not truncated, the strongest evidence (curated before
# text-mined) lands in chunk 0, and max_chunks caps + flags the tail. pure
# function, no network.

import logging
from typing import Any

from code.reduction import chunk_plover_response

_LOG = logging.getLogger("test_chunking")


def _attr(type_id: str, value: object) -> dict[str, object]:
    return {"attribute_type_id": type_id, "value": value}


def _edge(
    subject: str,
    object_: str,
    *,
    predicate: str = "biolink:treats",
    agent_type: str = "manual_agent",
) -> dict[str, object]:
    return {
        "subject": subject,
        "object": object_,
        "predicate": predicate,
        "attributes": [
            _attr("biolink:knowledge_level", "knowledge_assertion"),
            _attr("biolink:agent_type", agent_type),
        ],
    }


def _result(edge_id: str, subject: str, object_: str) -> dict[str, object]:
    return {
        "node_bindings": {"n0": [{"id": subject}], "n1": [{"id": object_}]},
        "analyses": [{"edge_bindings": {"e0": [{"id": edge_id}]}}],
    }


def _body(
    edges: dict[str, object], results: list[dict[str, object]], nodes: dict[str, object]
) -> dict[str, object]:
    return {
        "message": {
            "query_graph": {"nodes": {}, "edges": {}},
            "knowledge_graph": {"nodes": nodes, "edges": edges},
            "results": results,
            "auxiliary_graphs": {},
        }
    }


def _n_drug_body(n: int) -> dict[str, object]:
    # n results: drug_i --treats--> the same pinned disease.
    edges: dict[str, object] = {}
    results: list[dict[str, object]] = []
    nodes: dict[str, object] = {"MONDO:1": {"name": "disease"}}
    for i in range(n):
        drug = f"CHEBI:{i}"
        edges[f"e{i}"] = _edge(drug, "MONDO:1")
        results.append(_result(f"e{i}", drug, "MONDO:1"))
        nodes[drug] = {"name": f"drug{i}"}
    return _body(edges, results, nodes)


def _chunk_edge_ids(chunk: dict[str, Any]) -> set[str]:
    return set(chunk["message"]["knowledge_graph"]["edges"].keys())


def test_huge_budget_yields_single_chunk():
    out = chunk_plover_response(
        _n_drug_body(5), chunk_token_budget=100_000, max_chunks=50, logger=_LOG,
    )
    assert out.n_chunks == 1
    assert out.total_rows == 5
    assert out.chunked_rows == 5
    assert out.truncated_at_max_chunks is False
    assert len(_chunk_edge_ids(out.chunks[0])) == 5


def test_tiny_budget_splits_one_row_per_chunk_and_covers_all():
    # budget=10 tokens (40 chars) is below any single row's weight, so each
    # row gets its own chunk; the union must still cover every result edge.
    out = chunk_plover_response(
        _n_drug_body(5), chunk_token_budget=10, max_chunks=50, logger=_LOG,
    )
    assert out.n_chunks == 5
    covered: set[str] = set()
    for chunk in out.chunks:
        covered |= _chunk_edge_ids(chunk)
    assert covered == {f"e{i}" for i in range(5)}


def test_strongest_evidence_lands_in_first_chunk():
    # a curated edge and a text-mined edge under the same predicate; with a
    # tiny budget each is its own chunk. source_tier demotion means the
    # curated edge sorts first, so it must be in chunk 0.
    edges = {
        "curated": _edge("CHEBI:1", "MONDO:1", agent_type="manual_agent"),
        "textmined": _edge("CHEBI:2", "MONDO:1", agent_type="text_mining_agent"),
    }
    results = [_result("curated", "CHEBI:1", "MONDO:1"),
               _result("textmined", "CHEBI:2", "MONDO:1")]
    nodes = {"MONDO:1": {}, "CHEBI:1": {}, "CHEBI:2": {}}
    out = chunk_plover_response(
        _body(edges, results, nodes), chunk_token_budget=10, max_chunks=50, logger=_LOG,
    )
    assert out.n_chunks == 2
    assert "curated" in _chunk_edge_ids(out.chunks[0])
    assert "textmined" in _chunk_edge_ids(out.chunks[1])


def test_max_chunks_caps_and_flags_truncation():
    out = chunk_plover_response(
        _n_drug_body(5), chunk_token_budget=10, max_chunks=2, logger=_LOG,
    )
    assert out.n_chunks == 2
    assert out.total_rows == 5
    assert out.chunked_rows == 2
    assert out.truncated_at_max_chunks is True


def test_each_chunk_is_a_closed_subbody():
    # every edge a chunk's results bind must exist in that chunk's
    # knowledge_graph.edges (the LLM can read it standalone).
    out = chunk_plover_response(
        _n_drug_body(6), chunk_token_budget=10, max_chunks=50, logger=_LOG,
    )
    for chunk in out.chunks:
        msg = chunk["message"]
        edge_ids = set(msg["knowledge_graph"]["edges"].keys())
        for result in msg["results"]:
            for binding_list in result["analyses"][0]["edge_bindings"].values():
                for binding in binding_list:
                    assert binding["id"] in edge_ids


def test_noop_body_returns_single_chunk():
    out = chunk_plover_response(
        _body({}, [], {}), chunk_token_budget=1000, max_chunks=50, logger=_LOG,
    )
    assert out.n_chunks == 1
    assert out.total_rows == 0
