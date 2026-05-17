# _pubtator_verified_edge_rate: aggregate the per-edge verification
# block into the eval-level summary metric.
#
# spec:
#   - iterates over view["edges"], counts:
#       verified: edges with pubtator_verified.verified == True
#       unverified: edges with pubtator_verified.verified == False
#       not_applicable: edges with pubtator_verified == None
#       (an edge with no pubtator_verified key at all is treated as
#        not_applicable too, but in normal flow every edge has the key)
#   - rate = verified / (verified + unverified). undefined when the
#     denominator is 0 → return rate = None (so callers can distinguish
#     "all NA" from "0%").
#
# this is the metric the paper's "evidence-quality" axis hangs on:
# of the picked answer edges, what fraction are independently
# verifiable by PubTator?

from code.pipeline import _pubtator_verified_edge_rate


def _view(edges):
    # minimal answer_graph_view shape — only edges matter for this metric
    return {"pinned_node": {}, "answer_nodes": [], "edges": edges}


# ---- typical cases ----

def test_simple_rate_calculation():
    # 2 verified, 1 unverified, 0 NA → rate = 2/3
    view = _view([
        {"pubtator_verified": {"verified": True}},
        {"pubtator_verified": {"verified": True}},
        {"pubtator_verified": {"verified": False}},
    ])
    out = _pubtator_verified_edge_rate(view)
    assert out["verified"] == 2
    assert out["unverified"] == 1
    assert out["not_applicable"] == 0
    assert out["total_edges"] == 3
    assert out["rate"] == 2/3


def test_all_verified_gives_rate_one():
    view = _view([
        {"pubtator_verified": {"verified": True}},
        {"pubtator_verified": {"verified": True}},
    ])
    out = _pubtator_verified_edge_rate(view)
    assert out["rate"] == 1.0


def test_all_unverified_gives_rate_zero():
    view = _view([
        {"pubtator_verified": {"verified": False}},
        {"pubtator_verified": {"verified": False}},
    ])
    out = _pubtator_verified_edge_rate(view)
    assert out["rate"] == 0.0
    assert out["verified"] == 0


# ---- not-applicable cases ----

def test_na_edges_excluded_from_denominator():
    # 1 verified, 0 unverified, 2 NA (no PMIDs cited). rate = 1 / 1 = 1.0
    # — NA edges do NOT drag the rate down, they're simply ineligible.
    view = _view([
        {"pubtator_verified": {"verified": True}},
        {"pubtator_verified": None},
        {"pubtator_verified": None},
    ])
    out = _pubtator_verified_edge_rate(view)
    assert out["verified"] == 1
    assert out["unverified"] == 0
    assert out["not_applicable"] == 2
    assert out["total_edges"] == 3
    assert out["rate"] == 1.0


def test_all_na_gives_rate_none():
    # no edges had any PMIDs to verify → rate is undefined. returning
    # None lets the caller distinguish "no verifiable evidence" from
    # "all verified" or "all unverified".
    view = _view([
        {"pubtator_verified": None},
        {"pubtator_verified": None},
    ])
    out = _pubtator_verified_edge_rate(view)
    assert out["rate"] is None
    assert out["not_applicable"] == 2


def test_no_edges_gives_rate_none():
    # the answer set had 0 edges (e.g. picked an answer node but no
    # connecting edge in the KG). rate is None — there is literally
    # nothing to score.
    out = _pubtator_verified_edge_rate(_view([]))
    assert out["rate"] is None
    assert out["total_edges"] == 0


# ---- defensive: missing pubtator_verified key ----

def test_edge_missing_pubtator_verified_key_is_treated_as_na():
    # in normal flow every edge has the key (set explicitly by
    # _enrich_edges_with_pubtator), but be defensive — old artifacts
    # replayed through a newer pipeline could lack it. treat as NA, not
    # as a failure / crash.
    view = _view([
        {"id": "edge_no_key"},   # no pubtator_verified field at all
        {"pubtator_verified": {"verified": True}},
    ])
    out = _pubtator_verified_edge_rate(view)
    assert out["not_applicable"] == 1
    assert out["verified"] == 1
    assert out["rate"] == 1.0  # 1 / (1 + 0) = 1.0; NA excluded
