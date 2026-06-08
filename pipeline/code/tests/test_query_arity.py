# Stage 9 one-hop arity gate.
#
# PloverDB accepts exactly one hop: 2 query-graph nodes (n0, n1) and 1
# edge (e0). Stage 8 has an LLM assemble that query graph, so a malformed
# shape (1 node, 3 nodes, a second edge for an illegal multi-hop) is a
# real hallucination risk. reasoner-validator checks Biolink term
# compliance, NOT arity — so this pure check is the only thing that
# structurally enforces the one-hop invariant before the query is sent.
#
# pure function, zero network. asserts the gate accepts the one legal
# shape and rejects every malformed arity, naming the actual counts so a
# failure is diagnosable.

from code.pipeline import _check_query_graph_arity


def _msg(n_nodes: int, n_edges: int) -> dict[str, object]:
    nodes = {
        f"n{i}": {"categories": ["biolink:NamedThing"]} for i in range(n_nodes)
    }
    edges = {
        f"e{i}": {"subject": "n0", "object": "n1", "predicates": ["biolink:related_to"]}
        for i in range(n_edges)
    }
    return {"message": {"query_graph": {"nodes": nodes, "edges": edges}}}


def test_one_hop_passes():
    ok, reason = _check_query_graph_arity(_msg(2, 1))
    assert ok is True
    assert reason == ""


def test_single_node_rejected():
    # an LLM that emits only n0 (no answer node) — would query nothing
    ok, reason = _check_query_graph_arity(_msg(1, 1))
    assert ok is False
    assert "1 node" in reason


def test_three_nodes_rejected():
    ok, reason = _check_query_graph_arity(_msg(3, 1))
    assert ok is False
    assert "3 node" in reason


def test_two_edges_is_an_illegal_multi_hop():
    # the core invariant: a second edge means a 2-hop traversal, which
    # PloverDB does not accept and the project forbids
    ok, reason = _check_query_graph_arity(_msg(2, 2))
    assert ok is False
    assert "2 edge" in reason


def test_zero_edges_rejected():
    ok, reason = _check_query_graph_arity(_msg(2, 0))
    assert ok is False
    assert "0 edge" in reason


def test_missing_query_graph_is_rejected_not_crashed():
    # a malformed message must fail the gate gracefully, never raise
    ok, reason = _check_query_graph_arity({"message": {}})
    assert ok is False
    assert reason
    ok_empty, reason_empty = _check_query_graph_arity({})
    assert ok_empty is False
    assert reason_empty
