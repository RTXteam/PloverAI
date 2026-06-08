# Stage 3 candidate rerank: graph-coverage tier.
#
# _rerank_nameres_candidates orders NameRes candidates by lexical/type
# tiers that BM25 alone won't deliver. this file pins the coverage tier:
# an optional per-candidate edge count (how many edges the candidate has
# to the answer category in the hosted graph) is folded in as the LOWEST
# tier — below type_match, above BM25. that ordering means:
#   - a candidate that actually has edges beats an equal one that doesn't
#     (so we stop pinning real-looking but data-less entities), but
#   - type_match still dominates coverage, so the seizure-case fix (an
#     HP PhenotypicFeature candidate beating a higher-BM25 MONDO Disease
#     for "seizures") survives even when the MONDO term has more edges.
#
# pure function, zero network. coverage is passed in (the probe that
# produces it is exercised elsewhere); here we pin only the ordering.

from code.pipeline import _rerank_nameres_candidates


def _cand(
    curie: str,
    label: str,
    types: list[str],
    score: float,
    synonyms: list[str] | None = None,
) -> dict[str, object]:
    return {
        "curie": curie,
        "label": label,
        "synonyms": synonyms or [],
        "types": types,
        "score": score,
    }


def test_coverage_breaks_ties_within_same_lexical_tier():
    # two candidates identical on every lexical/type tier and BM25; the
    # only difference is graph coverage. the one with edges must win.
    a = _cand("CURIE:A", "foo", ["biolink:Disease"], 10.0)
    b = _cand("CURIE:B", "foo", ["biolink:Disease"], 10.0)
    ordered = _rerank_nameres_candidates(
        [a, b], "foo", "biolink:Disease",
        coverage_by_curie={"CURIE:A": 0, "CURIE:B": 5},
    )
    assert [c["curie"] for c in ordered] == ["CURIE:B", "CURIE:A"]


def test_type_match_outranks_coverage():
    # seizure-case preservation: the HP candidate type-matches the
    # expected category but has ZERO edges; the MONDO candidate has a much
    # higher BM25 and 500 edges but the WRONG type. type_match is a higher
    # tier than coverage, so HP must still win despite having no edges.
    hp = _cand("HP:1", "phenotype label", ["biolink:PhenotypicFeature"], 10.0)
    mondo = _cand("MONDO:1", "disease label", ["biolink:Disease"], 200.0)
    ordered = _rerank_nameres_candidates(
        [mondo, hp], "seizures", "biolink:PhenotypicFeature",
        coverage_by_curie={"HP:1": 0, "MONDO:1": 500},
    )
    assert ordered[0]["curie"] == "HP:1"


def test_coverage_none_orders_by_bm25_not_edges():
    # when no coverage is supplied the tier is inert: candidates identical
    # on lexical/type tiers fall back to BM25 order, exactly as before the
    # coverage tier existed. (contrast: with coverage favouring B, B wins.)
    a = _cand("CURIE:A", "foo", ["biolink:Disease"], 20.0)
    b = _cand("CURIE:B", "foo", ["biolink:Disease"], 10.0)
    without = _rerank_nameres_candidates([a, b], "foo", "biolink:Disease")
    assert [c["curie"] for c in without] == ["CURIE:A", "CURIE:B"]
    with_cov = _rerank_nameres_candidates(
        [a, b], "foo", "biolink:Disease",
        coverage_by_curie={"CURIE:A": 0, "CURIE:B": 5},
    )
    assert [c["curie"] for c in with_cov] == ["CURIE:B", "CURIE:A"]
