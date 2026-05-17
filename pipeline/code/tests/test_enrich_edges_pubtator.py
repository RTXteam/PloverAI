# _enrich_edges_with_pubtator: add per-edge PubTator co-mention
# verification to the answer_graph_view's edges.
#
# pure function. takes:
#   - edges: list of dicts from _build_answer_graph_view (each has
#     source, target, supporting_publications)
#   - equivalent_curies: dict[curie -> list of equivalent CURIEs from
#     NodeNorm], used to bridge MeSH<->MONDO/CHEBI namespaces
#   - pubtator_annotations: dict[pmid -> set of CURIEs PubTator
#     annotated in that PMID]
#
# returns a NEW list of edges (same length, same order) with each edge
# augmented with a "pubtator_verified" block.
#
# spec for the pubtator_verified block:
#   - co_mention_pmids:    PMIDs where the source's OR an equivalent CURIE AND the target's
#                          OR an equivalent CURIE both appear in PubTator annotations
#   - subject_only_pmids:  PMIDs where only the source side is annotated
#   - object_only_pmids:   PMIDs where only the target side is annotated
#   - missing_pmids:       PMIDs not in pubtator_annotations dict at all
#                          (PubTator unaware of this PMID)
#   - co_mention_rate:     |co_mention_pmids| / (total - missing).
#                          undefined when total == missing → 0.0
#   - verified:            True iff |co_mention_pmids| >= 1
#
# edges with NO supporting_publications get a pubtator_verified block
# explicitly set to None (not omitted, not empty) — callers downstream
# can distinguish "no evidence to check" from "checked, found nothing".

from pipeline.code.pipeline import _enrich_edges_with_pubtator


# ---- shared fixtures ----

# the edge under test: metformin (CHEBI:6801) treats T2DM (MONDO:0005148),
# cited by 3 PMIDs. mirrors what _build_answer_graph_view produces.
def _edges_basic():
    return [{
        "id": "edge1",
        "source": "CHEBI:6801",          # metformin
        "target": "MONDO:0005148",        # type 2 diabetes
        "predicate": "biolink:treats",
        "knowledge_level": "knowledge_assertion",
        "primary_knowledge_source": "infores:drugcentral",
        "supporting_publications": ["PMID:100", "PMID:200", "PMID:300"],
        "supporting_text_snippets": [],
    }]


# equivalent CURIE map mirrors what NodeNorm returns for the two endpoints.
# the MeSH variants are what PubTator actually annotates with.
def _equiv_basic():
    return {
        "CHEBI:6801": ["CHEBI:6801", "MESH:D008687", "UMLS:C0025598"],
        "MONDO:0005148": ["MONDO:0005148", "MESH:D003924", "UMLS:C0011860"],
    }


# ---- happy path ----

def test_edge_with_one_co_mention_pmid_is_verified():
    # PMID:100 mentions both (via MeSH equivalents).
    # PMID:200 only mentions the drug. PMID:300 unknown to PubTator.
    ann = {
        "PMID:100": {"MESH:D008687", "MESH:D003924"},   # both → co_mention
        "PMID:200": {"MESH:D008687"},                    # drug only
        # PMID:300 deliberately absent → missing
    }
    out = _enrich_edges_with_pubtator(_edges_basic(), _equiv_basic(), ann)
    v = out[0]["pubtator_verified"]
    assert v["co_mention_pmids"] == ["PMID:100"]
    assert v["subject_only_pmids"] == ["PMID:200"]
    assert v["object_only_pmids"] == []
    assert v["missing_pmids"] == ["PMID:300"]
    # co_mention_rate = 1 / (3 - 1 missing) = 1 / 2 = 0.5
    assert v["co_mention_rate"] == 0.5
    assert v["verified"] is True


def test_edge_with_zero_co_mentions_is_not_verified():
    # all PMIDs annotated but only one side appears in each → verified=False
    ann = {
        "PMID:100": {"MESH:D008687"},                # drug only
        "PMID:200": {"MESH:D003924"},                # disease only
        "PMID:300": {"MESH:OTHER"},                  # neither
    }
    out = _enrich_edges_with_pubtator(_edges_basic(), _equiv_basic(), ann)
    v = out[0]["pubtator_verified"]
    assert v["co_mention_pmids"] == []
    assert v["verified"] is False
    assert v["co_mention_rate"] == 0.0


def test_all_pmids_missing_yields_zero_rate_unverified():
    # PubTator doesn't index any of the cited PMIDs. rate is 0 (we
    # divide by 0 non-missing → defined-as 0.0). verified is False.
    out = _enrich_edges_with_pubtator(_edges_basic(), _equiv_basic(), {})
    v = out[0]["pubtator_verified"]
    assert v["missing_pmids"] == ["PMID:100", "PMID:200", "PMID:300"]
    assert v["co_mention_rate"] == 0.0
    assert v["verified"] is False


# ---- equivalence-class bridging ----

def test_match_uses_equivalent_curies_not_just_canonical():
    # the edge endpoints are CHEBI/MONDO, but PubTator's annotations
    # are pure MeSH. without equivalent_identifiers from NodeNorm this
    # would never match. the test pins that the function does the
    # cross-namespace lookup correctly.
    ann = {"PMID:100": {"MESH:D008687", "MESH:D003924"}}  # ALL MeSH
    edges = [{
        "id": "edge1",
        "source": "CHEBI:6801",
        "target": "MONDO:0005148",
        "predicate": "biolink:treats",
        "supporting_publications": ["PMID:100"],
    }]
    out = _enrich_edges_with_pubtator(edges, _equiv_basic(), ann)
    assert out[0]["pubtator_verified"]["co_mention_pmids"] == ["PMID:100"]


def test_canonical_curie_alone_also_matches():
    # if PubTator's annotation set happens to use the canonical CHEBI
    # CURIE (rare but possible for chemicals), it should still match.
    # validates that the function searches the canonical AND the equivalents,
    # not just the equivalents.
    ann = {"PMID:100": {"CHEBI:6801", "MONDO:0005148"}}
    out = _enrich_edges_with_pubtator(_edges_basic(), _equiv_basic(), ann)
    assert out[0]["pubtator_verified"]["co_mention_pmids"] == ["PMID:100"]


# ---- edge cases ----

def test_edge_without_supporting_publications_gets_null_verified_block():
    # an edge with no PMIDs to verify — explicit None signals
    # "not applicable" downstream, distinguished from "checked, found none".
    edges = [{
        "id": "edge2",
        "source": "CHEBI:6801",
        "target": "MONDO:0005148",
        "predicate": "biolink:treats",
        "supporting_publications": [],
    }]
    out = _enrich_edges_with_pubtator(edges, _equiv_basic(), {})
    assert out[0]["pubtator_verified"] is None


def test_edge_without_equivalents_in_map_falls_back_to_canonical_only():
    # if NodeNorm didn't return equivalents for an endpoint (network
    # error, unresolvable CURIE), the function should still try matching
    # against the canonical CURIE alone — degrade gracefully.
    edges = [{
        "id": "edge1",
        "source": "CHEBI:6801",
        "target": "MONDO:0005148",
        "predicate": "biolink:treats",
        "supporting_publications": ["PMID:100"],
    }]
    # equivalents map is EMPTY — only canonical is the truth
    ann = {"PMID:100": {"CHEBI:6801", "MONDO:0005148"}}
    out = _enrich_edges_with_pubtator(edges, {}, ann)
    assert out[0]["pubtator_verified"]["co_mention_pmids"] == ["PMID:100"]


def test_original_edges_are_not_mutated():
    # function returns a NEW list; the input edges objects must be
    # unchanged after the call (no in-place pubtator_verified key on
    # the originals). this matters because the same edges object could
    # be cached / re-used by a downstream test or scoring run.
    original = _edges_basic()
    _ = _enrich_edges_with_pubtator(original, _equiv_basic(), {})
    assert "pubtator_verified" not in original[0]


# ---- multi-edge case ----

def test_multiple_edges_each_get_independent_verification():
    edges = [
        # edge A: full PMID overlap → verified
        {
            "id": "edge_A",
            "source": "CHEBI:6801", "target": "MONDO:0005148",
            "predicate": "biolink:treats",
            "supporting_publications": ["PMID:100"],
        },
        # edge B: no PMIDs → null block
        {
            "id": "edge_B",
            "source": "CHEBI:6801", "target": "MONDO:0005148",
            "predicate": "biolink:treats",
            "supporting_publications": [],
        },
    ]
    ann = {"PMID:100": {"MESH:D008687", "MESH:D003924"}}
    out = _enrich_edges_with_pubtator(edges, _equiv_basic(), ann)
    assert out[0]["pubtator_verified"]["verified"] is True
    assert out[1]["pubtator_verified"] is None
