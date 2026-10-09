# reduction.py: the deterministic Stage 11 pre-step that turns a raw
# lookup TRAPI response into a ranked, flattened evidence table.
#
# spec under test:
#   - reduce_response ranks EVERY edge in message.knowledge_graph.edges
#     by (knowledge_level_rank, agent_type_rank, -n_sources,
#     -total_publications, -n_publications, edge_id) and keeps the
#     first top_k.
#   - the three corroboration figures are aggregated over the FULL edge
#     set per unordered (subject_curie, object_curie) pair and attached
#     to every edge of that pair. they order rows INSIDE a tier and
#     never across one.
#   - each kept edge becomes one flat row carrying the endpoints'
#     CURIEs / names / first category, the predicate, the two Biolink
#     provenance enums, the publication count, up to 3 publications
#     (PMIDs normalised and sorted first), and the primary knowledge
#     source.
#   - missing or unrecognised provenance enums become "not_provided"
#     and rank LAST, so a source that omits the field can never
#     outrank a curated assertion.
#   - render_table prints a "showing K of N" header plus one line per
#     kept row, and every edge_id and CURIEs are copyable verbatim.
#   - to_json is the artifact shape (reduced_data.json) and must be
#     JSON-serialisable.
#
# two synthetic responses:
#   _response()               — every tie-break edge sits on the SAME
#     entity pair, so the pair-level corroboration figures are equal
#     across them and each remaining tie-break level is exercised in
#     isolation: e_ka_* differ only in agent_type, e_pred_* only in
#     publication count, e_tie_a / e_tie_b only in edge_id.
#   _corroboration_response() — every edge is knowledge_assertion /
#     manual_agent / 0 own publications, so kl, agent_type and own
#     publication count are all tied and ONLY corroboration can order
#     the rows. this reproduces the real "alzhiemers genes" shape.

from __future__ import annotations

import json
from typing import Any

from pipeline.code.reduction import (
    ReducedData,
    reduce_response,
    render_table,
    to_json,
)


def _edge(
    subject: str,
    predicate: str,
    obj: str,
    *,
    knowledge_level: str | None = None,
    agent_type: str | None = None,
    publications: Any = None,
    primary_source: str | None = "infores:drugcentral",
) -> dict[str, Any]:
    # a TRAPI edge stores provenance as a LIST of attribute objects, so
    # "missing" means the entry is simply absent from that list.
    attributes: list[dict[str, Any]] = []
    if knowledge_level is not None:
        attributes.append({
            "attribute_type_id": "biolink:knowledge_level",
            "value": knowledge_level,
        })
    if agent_type is not None:
        attributes.append({
            "attribute_type_id": "biolink:agent_type",
            "value": agent_type,
        })
    if publications is not None:
        attributes.append({
            "attribute_type_id": "biolink:publications",
            "value": publications,
        })
    sources: list[dict[str, Any]] = [
        {"resource_id": "infores:rtx-kg2", "resource_role": "aggregator_knowledge_source"},
    ]
    if primary_source is not None:
        sources.insert(0, {
            "resource_id": primary_source,
            "resource_role": "primary_knowledge_source",
        })
    return {
        "subject": subject,
        "object": obj,
        "predicate": predicate,
        "attributes": attributes,
        "sources": sources,
    }


def _response() -> dict[str, Any]:
    # 4 nodes, 8 edges, 6 results. the edge ids are deliberately NOT in
    # ranked order in the dict so a test that passes cannot be passing
    # by accident of insertion order. every ranking edge sits on the
    # CHEBI:3897 / MONDO:0008383 pair, so all six share one set of
    # corroboration figures and the order below is decided purely by
    # knowledge_level, agent_type, own publication count, edge_id.
    return {
        "message": {
            "results": [{"node_bindings": {}} for _ in range(6)],
            "knowledge_graph": {
                "nodes": {
                    "CHEBI:3897": {
                        "name": "Cortisone acetate",
                        "categories": ["biolink:SmallMolecule", "biolink:Drug"],
                    },
                    "MONDO:0008383": {
                        "name": "rheumatoid arthritis",
                        "categories": ["biolink:Disease"],
                    },
                    "CHEBI:6801": {
                        "name": "metformin",
                        "categories": ["biolink:SmallMolecule"],
                    },
                    "CHEBI:0000000": {
                        "name": (
                            "a chemical entity with a preposterously long "
                            "label that the renderer has to cut down"
                        ),
                        "categories": ["biolink:ChemicalEntity"],
                    },
                },
                "edges": {
                    # weakest: no provenance attributes at all
                    "e_missing": _edge(
                        "CHEBI:0000000", "biolink:related_to", "MONDO:0008383",
                        primary_source=None,
                    ),
                    # prediction tier, 2 pubs
                    "e_pred_2": _edge(
                        "CHEBI:3897", "biolink:treats", "MONDO:0008383",
                        knowledge_level="prediction",
                        agent_type="text_mining_agent",
                        publications=["PMID:200", "PMID:201"],
                    ),
                    # knowledge_assertion + manual_agent: the strongest row
                    "e_ka_manual": _edge(
                        "CHEBI:3897", "biolink:applied_to_treat", "MONDO:0008383",
                        knowledge_level="knowledge_assertion",
                        agent_type="manual_agent",
                        publications=["PMID:123", "PMID:456", "PMID:789", "PMID:999"],
                    ),
                    # same tier, weaker agent
                    "e_ka_auto": _edge(
                        "CHEBI:3897", "biolink:applied_to_treat", "MONDO:0008383",
                        knowledge_level="knowledge_assertion",
                        agent_type="automated_agent",
                        publications="PMID:555",
                    ),
                    # prediction tier, 5 pubs — outranks e_pred_2
                    "e_pred_5": _edge(
                        "CHEBI:3897", "biolink:treats", "MONDO:0008383",
                        knowledge_level="prediction",
                        agent_type="text_mining_agent",
                        publications=[
                            "PMID:300", "PMID:301", "PMID:302", "PMID:303", "PMID:304",
                        ],
                    ),
                    # identical on every key except the edge_id
                    "e_tie_b": _edge(
                        "CHEBI:3897", "biolink:related_to", "MONDO:0008383",
                        knowledge_level="observation",
                        agent_type="manual_agent",
                    ),
                    "e_tie_a": _edge(
                        "CHEBI:3897", "biolink:related_to", "MONDO:0008383",
                        knowledge_level="observation",
                        agent_type="manual_agent",
                    ),
                    # mixed publication formats
                    "e_mixed_pubs": _edge(
                        "CHEBI:3897", "biolink:related_to", "CHEBI:6801",
                        knowledge_level="logical_entailment",
                        agent_type="manual_agent",
                        publications=[
                            "doi:10.1000/xyz", "pmid 42", "PMID:7", "ISBN:0000",
                        ],
                    ),
                },
            },
        },
    }


def _reduced(top_k: int = 100) -> ReducedData:
    return reduce_response(_response(), top_k=top_k)


def _ids(reduced: ReducedData) -> list[str]:
    return [str(row["edge_id"]) for row in reduced.rows]


def _row(reduced: ReducedData, edge_id: str) -> dict[str, Any]:
    return next(row for row in reduced.rows if row["edge_id"] == edge_id)


# ---- ranking ----

def test_orders_by_knowledge_level_first() -> None:
    # knowledge_assertion < logical_entailment < prediction < observation
    # < not_provided, regardless of how many publications the weaker
    # edges carry.
    ids = _ids(_reduced())
    assert ids == [
        "e_ka_manual",   # knowledge_assertion + manual_agent
        "e_ka_auto",     # knowledge_assertion + automated_agent
        "e_mixed_pubs",  # logical_entailment
        "e_pred_5",      # prediction, 5 pubs
        "e_pred_2",      # prediction, 2 pubs
        "e_tie_a",       # observation, tie broken by edge_id
        "e_tie_b",
        "e_missing",     # not_provided
    ]


def test_agent_type_breaks_knowledge_level_ties() -> None:
    # both edges are knowledge_assertion; manual_agent (rank 0) must
    # come before automated_agent (rank 3) even though it is NOT the
    # one with fewer publications.
    ids = _ids(_reduced())
    assert ids.index("e_ka_manual") < ids.index("e_ka_auto")


def test_publication_count_breaks_agent_type_ties() -> None:
    # identical knowledge_level, agent_type and pair corroboration
    # (both sit on the CHEBI:3897 pair), so the edge's OWN publication
    # count decides: 5 pubs beats 2 pubs.
    reduced = _reduced()
    assert _row(reduced, "e_pred_5")["n_sources"] == _row(reduced, "e_pred_2")["n_sources"]
    assert (
        _row(reduced, "e_pred_5")["total_publications"]
        == _row(reduced, "e_pred_2")["total_publications"]
    )
    ids = _ids(reduced)
    assert ids.index("e_pred_5") < ids.index("e_pred_2")


def test_edge_id_breaks_remaining_ties_for_a_stable_order() -> None:
    # e_tie_a and e_tie_b agree on every other key INCLUDING the pair
    # corroboration, so the order is decided by the edge_id and is
    # reproducible across runs.
    ids = _ids(_reduced())
    assert ids.index("e_tie_a") < ids.index("e_tie_b")
    assert _ids(_reduced()) == ids


def test_missing_attributes_become_not_provided_and_rank_last() -> None:
    reduced = _reduced()
    row = _row(reduced, "e_missing")
    assert row["knowledge_level"] == "not_provided"
    assert row["agent_type"] == "not_provided"
    assert row["n_publications"] == 0
    assert row["publications"] == []
    assert row["primary_knowledge_source"] is None
    assert _ids(reduced)[-1] == "e_missing"


# ---- top_k cutoff and stats ----

def test_top_k_cuts_the_weakest_rows_and_stats_describe_the_whole_response() -> None:
    reduced = _reduced(top_k=3)
    assert _ids(reduced) == ["e_ka_manual", "e_ka_auto", "e_mixed_pubs"]
    assert reduced.kept == 3
    assert reduced.top_k == 3
    # stats are over the WHOLE response, not the kept slice
    assert reduced.total_edges == 8
    assert reduced.total_results == 6
    assert reduced.knowledge_level_counts == {
        "knowledge_assertion": 2,
        "logical_entailment": 1,
        "prediction": 2,
        "observation": 2,
        "not_provided": 1,
    }


def test_top_k_larger_than_the_response_keeps_everything() -> None:
    reduced = _reduced(top_k=1000)
    assert reduced.kept == 8
    assert reduced.total_edges == 8


def test_empty_response_reduces_to_an_empty_table() -> None:
    reduced = reduce_response({}, top_k=300)
    assert reduced.rows == []
    assert reduced.kept == 0
    assert reduced.total_edges == 0
    assert reduced.total_results == 0
    assert reduced.knowledge_level_counts == {}


# ---- row contents ----

def test_row_flattens_endpoints_predicate_and_provenance() -> None:
    row = _row(_reduced(), "e_ka_manual")
    assert row["subject_curie"] == "CHEBI:3897"
    assert row["subject_name"] == "Cortisone acetate"
    # first category only
    assert row["subject_category"] == "biolink:SmallMolecule"
    assert row["predicate"] == "biolink:applied_to_treat"
    assert row["object_curie"] == "MONDO:0008383"
    assert row["object_name"] == "rheumatoid arthritis"
    assert row["object_category"] == "biolink:Disease"
    assert row["knowledge_level"] == "knowledge_assertion"
    assert row["agent_type"] == "manual_agent"
    assert row["primary_knowledge_source"] == "infores:drugcentral"


def test_publication_count_is_the_total_while_the_list_is_capped_at_three() -> None:
    row = _row(_reduced(), "e_ka_manual")
    assert row["n_publications"] == 4
    assert row["publications"] == ["PMID:123", "PMID:456", "PMID:789"]


def test_publications_given_as_a_bare_string_are_accepted() -> None:
    row = _row(_reduced(), "e_ka_auto")
    assert row["n_publications"] == 1
    assert row["publications"] == ["PMID:555"]


def test_pmids_are_normalised_and_sorted_before_other_identifiers() -> None:
    # "pmid 42" normalises to PMID:42; the DOI and ISBN keep their own
    # form and follow the PMIDs.
    row = _row(_reduced(), "e_mixed_pubs")
    assert row["n_publications"] == 4
    assert row["publications"] == ["PMID:42", "PMID:7", "doi:10.1000/xyz"]


# ---- rendering ----

def test_render_table_header_states_the_cut_and_the_ladders() -> None:
    table = render_table(_reduced(top_k=3))
    header = table.splitlines()[0]
    assert "showing 3 of 8 edges" in header
    assert "(6 results)" in header
    assert "knowledge_level, then agent_type, then corroboration" in header
    assert "knowledge_assertion > logical_entailment > prediction" in table
    assert "manual_agent > manual_validation_of_automated_agent" in table


def test_render_table_contains_every_kept_edge_id_and_curie() -> None:
    reduced = _reduced(top_k=5)
    table = render_table(reduced)
    for row in reduced.rows:
        assert str(row["edge_id"]) in table
        assert str(row["subject_curie"]) in table
        assert str(row["object_curie"]) in table
    # cut rows must NOT leak into the prompt
    assert "e_missing" not in table


def test_render_table_row_shape_is_copyable() -> None:
    table = render_table(_reduced(top_k=1))
    row_line = table.splitlines()[-1]
    assert row_line == (
        "e_ka_manual | Cortisone acetate (CHEBI:3897) "
        "--biolink:applied_to_treat--> rheumatoid arthritis (MONDO:0008383) | "
        "kl=knowledge_assertion agent=manual_agent pubs=4 "
        "[PMID:123, PMID:456, PMID:789] src=infores:drugcentral corr=1src/6e/12p"
    )


def test_render_table_omits_empty_publication_list_and_absent_source() -> None:
    table = render_table(_reduced())
    missing_line = next(
        line for line in table.splitlines() if line.startswith("e_missing ")
    )
    assert "pubs=0" in missing_line
    assert "[" not in missing_line
    # no primary_knowledge_source at all, so no src= token — but corr=
    # is always rendered, and reports 0 distinct sources for the pair
    assert " src=" not in missing_line
    assert missing_line.endswith(" corr=0src/1e/0p")


def test_render_table_truncates_long_entity_names() -> None:
    table = render_table(_reduced())
    missing_line = next(
        line for line in table.splitlines() if line.startswith("e_missing ")
    )
    truncated = "a chemical entity with a preposterously long label that t..."
    assert len(truncated) == 60
    assert truncated in missing_line
    # the CURIE is never truncated — the LLM has to copy it verbatim
    assert "(CHEBI:0000000)" in missing_line


# ---- artifact shape ----

def test_to_json_round_trips_through_json_dumps() -> None:
    reduced = _reduced(top_k=4)
    payload = to_json(reduced)
    restored: dict[str, Any] = json.loads(json.dumps(payload, ensure_ascii=False))
    assert restored["kept"] == 4
    assert restored["top_k"] == 4
    assert restored["total_edges"] == 8
    assert restored["total_results"] == 6
    assert restored["ranking_key"] == (
        "(knowledge_level_rank, agent_type_rank, -n_sources, "
        "-total_publications, -n_publications, edge_id)"
    )
    assert restored["knowledge_level_counts"]["knowledge_assertion"] == 2
    assert [r["edge_id"] for r in restored["rows"]] == [
        "e_ka_manual", "e_ka_auto", "e_mixed_pubs", "e_pred_5",
    ]


# ---- corroboration (the within-tier tie-break) ----

# every edge below is knowledge_assertion / manual_agent / 0 own
# publications, so kl, agent_type and own publication count are tied
# and ONLY the pair-level corroboration can order the assertion rows.
# the edge ids are the real ones from the "alzhiemers genes" run, where
# NOS3's assertion (1090819) sorted ahead of APOE's (1090826) purely
# because its id is smaller.
def _corroboration_response() -> dict[str, Any]:
    disease = "MONDO:0004975"
    edges: dict[str, Any] = {
        # APOE: one NCIt assertion corroborated by a text-mined edge
        # with 20 PMIDs and two DisGeNET predictions -> 3src/4e/22p
        "1090826": _edge(
            "NCBIGene:348", "biolink:gene_associated_with_condition", disease,
            knowledge_level="knowledge_assertion", agent_type="manual_agent",
            primary_source="infores:ncit",
        ),
        "44529472": _edge(
            "NCBIGene:348", "biolink:gene_associated_with_condition", disease,
            knowledge_level="prediction", agent_type="text_mining_agent",
            publications=[f"PMID:{n}" for n in range(1000, 1020)],
            primary_source="infores:diseases",
        ),
        "46140806": _edge(
            "NCBIGene:348", "biolink:gene_associated_with_condition", disease,
            knowledge_level="prediction", agent_type="computational_model",
            publications=["PMID:2001"], primary_source="infores:disgenet",
        ),
        # same source again: a repeated source is not a second opinion,
        # so n_sources must stay at 3 while the edge count goes to 4
        "46140964": _edge(
            "NCBIGene:348", "biolink:gene_associated_with_condition", disease,
            knowledge_level="prediction", agent_type="computational_model",
            publications=["PMID:2002"], primary_source="infores:disgenet",
        ),
        # NOS3: the lone assertion, nothing else says the same thing
        # -> 1src/1e/0p, and the SMALLEST edge id in the response
        "1090819": _edge(
            "NCBIGene:4846", "biolink:gene_associated_with_condition", disease,
            knowledge_level="knowledge_assertion", agent_type="manual_agent",
            primary_source="infores:ncit",
        ),
        # CTSB: 2 sources, 20 publications, and the LARGEST assertion id
        "1090899": _edge(
            "NCBIGene:1508", "biolink:gene_associated_with_condition", disease,
            knowledge_level="knowledge_assertion", agent_type="manual_agent",
            primary_source="infores:ncit",
        ),
        "44951054": _edge(
            "NCBIGene:1508", "biolink:gene_associated_with_condition", disease,
            knowledge_level="prediction", agent_type="text_mining_agent",
            publications=[f"PMID:{n}" for n in range(3000, 3020)],
            primary_source="infores:diseases",
        ),
        # MT3: also 2 sources, but only 5 publications
        "1090822": _edge(
            "NCBIGene:4507", "biolink:gene_associated_with_condition", disease,
            knowledge_level="knowledge_assertion", agent_type="manual_agent",
            primary_source="infores:ncit",
        ),
        "44951099": _edge(
            "NCBIGene:4507", "biolink:gene_associated_with_condition", disease,
            knowledge_level="prediction", agent_type="text_mining_agent",
            publications=[f"PMID:{n}" for n in range(4000, 4005)],
            primary_source="infores:diseases",
        ),
    }
    # a heavily corroborated PREDICTION: 5 distinct sources, 40 pubs.
    # it must still sort below every assertion in the response.
    for i in range(5):
        edges[f"5000000{i}"] = _edge(
            "CHEBI:9300", "biolink:gene_associated_with_condition", disease,
            knowledge_level="prediction", agent_type="text_mining_agent",
            publications=[f"PMID:{5000 + i * 8 + n}" for n in range(8)],
            primary_source=f"infores:source-{i}",
        )
    return {
        "message": {
            "results": [{"node_bindings": {}} for _ in range(9)],
            "knowledge_graph": {
                "nodes": {
                    disease: {"name": "Alzheimer disease", "categories": ["biolink:Disease"]},
                    "NCBIGene:348": {"name": "APOE", "categories": ["biolink:Gene"]},
                    "NCBIGene:4846": {"name": "NOS3", "categories": ["biolink:Gene"]},
                    "NCBIGene:1508": {"name": "CTSB", "categories": ["biolink:Gene"]},
                    "NCBIGene:4507": {"name": "MT3", "categories": ["biolink:Gene"]},
                    "CHEBI:9300": {"name": "noisy chemical", "categories": ["biolink:Drug"]},
                },
                "edges": edges,
            },
        },
    }


def _corroborated(top_k: int = 100) -> ReducedData:
    return reduce_response(_corroboration_response(), top_k=top_k)


def test_corroboration_aggregates_over_the_whole_pair() -> None:
    reduced = _corroborated()
    apoe = _row(reduced, "1090826")
    # 4 edges from 3 DISTINCT sources (DisGeNET appears twice) and
    # 20 + 1 + 1 = 22 publications summed across the pair
    assert apoe["n_corroborating_edges"] == 4
    assert apoe["n_sources"] == 3
    assert apoe["total_publications"] == 22
    # the figures are attached to EVERY edge of the pair, not only the
    # assertion that will be ranked highest
    assert _row(reduced, "44529472")["n_sources"] == 3
    assert _row(reduced, "46140964")["total_publications"] == 22
    # the lone assertion gets the "nothing corroborates this" profile
    nos3 = _row(reduced, "1090819")
    assert (nos3["n_sources"], nos3["n_corroborating_edges"], nos3["total_publications"]) == (1, 1, 0)


def test_n_sources_breaks_a_within_tier_tie() -> None:
    # APOE and NOS3 agree on knowledge_level, agent_type and own
    # publication count. NOS3 has the smaller edge id and led the table
    # before this tie-break existed; 3 sources vs 1 now decides.
    ids = _ids(_corroborated())
    assert ids.index("1090826") < ids.index("1090819")
    assert ids[0] == "1090826"


def test_total_publications_breaks_a_tie_when_source_counts_match() -> None:
    # CTSB and MT3 both have 2 sources, so the summed publication count
    # (20 vs 5) decides — and it beats the edge id, which would have
    # put MT3 (1090822) ahead of CTSB (1090899).
    reduced = _corroborated()
    assert _row(reduced, "1090899")["n_sources"] == _row(reduced, "1090822")["n_sources"] == 2
    ids = _ids(reduced)
    assert ids.index("1090899") < ids.index("1090822")


def test_the_apoe_pattern_outranks_a_lone_assertion() -> None:
    # the whole point: the four assertion rows come out ordered by how
    # much independent corroboration the response holds for each gene,
    # not by the arbitrary order their ids were assigned in.
    reduced = _corroborated()
    assertions = [
        row for row in reduced.rows if row["knowledge_level"] == "knowledge_assertion"
    ]
    assert [row["subject_name"] for row in assertions] == ["APOE", "CTSB", "MT3", "NOS3"]


def test_corroboration_never_moves_an_edge_across_tiers() -> None:
    # the prediction pair has 5 sources and 40 publications — more
    # corroboration than any assertion here — and still sorts below
    # every one of them. volume of text is not curation.
    reduced = _corroborated()
    levels = [row["knowledge_level"] for row in reduced.rows]
    last_assertion = max(i for i, lv in enumerate(levels) if lv == "knowledge_assertion")
    first_prediction = min(i for i, lv in enumerate(levels) if lv == "prediction")
    assert last_assertion < first_prediction
    # the 5-source / 40-publication pair really is the most corroborated
    # thing in the response
    noisiest = _row(reduced, "50000000")
    assert (noisiest["n_sources"], noisiest["total_publications"]) == (5, 40)
    assert max(row["n_sources"] for row in reduced.rows) == 5
    # and the weakest assertion (1 source, 0 publications) still outranks it
    assert _row(reduced, "1090819")["n_sources"] == 1
    assert _ids(reduced).index("1090819") < _ids(reduced).index("50000000")


def test_to_json_carries_the_corroboration_fields() -> None:
    payload = to_json(_corroborated(top_k=1))
    restored: dict[str, Any] = json.loads(json.dumps(payload, ensure_ascii=False))
    assert restored["ranking_key"] == (
        "(knowledge_level_rank, agent_type_rank, -n_sources, "
        "-total_publications, -n_publications, edge_id)"
    )
    row = restored["rows"][0]
    assert row["edge_id"] == "1090826"
    assert row["n_sources"] == 3
    assert row["n_corroborating_edges"] == 4
    assert row["total_publications"] == 22


def test_render_table_shows_the_corr_suffix() -> None:
    table = render_table(_corroborated(top_k=2))
    assert "corr=<sources>src/<edges>e/<publications>p" in table
    assert "corr= describes the WHOLE subject/object pair" in table
    rows = table.splitlines()[-2:]
    assert rows[0].endswith("corr=3src/4e/22p")
    assert rows[0].startswith(
        "1090826 | APOE (NCBIGene:348) "
        "--biolink:gene_associated_with_condition--> Alzheimer disease (MONDO:0004975) | "
        "kl=knowledge_assertion agent=manual_agent pubs=0 src=infores:ncit"
    )
