# Biolink edge qualifiers: the direction / aspect a fact asserts
# ("imatinib DECREASES the ACTIVITY of ABL1"), carried by TRAPI 1.5 in
# edge.qualifiers and matched by the graph through
# edge.qualifier_constraints on the query graph.
#
# spec under test:
#   - edge_qualifiers returns every well-formed {qualifier_type_id,
#     qualifier_value} pair as a (type_id, value) tuple, sorted, so the
#     same set always yields the same signature. malformed entries are
#     skipped; an edge without qualifiers yields [].
#   - render_qualifiers joins them as "type_id=value, ..." with full
#     Biolink ids, so Stage 8 can copy them verbatim into
#     qualifier_constraints. an empty list renders as "".
#   - reduce_response carries a row["qualifiers"] string (None when the
#     edge has none) and render_table appends " qual=[...]" to exactly
#     those rows, before corr=. unqualified rows render as before.
#   - tally_probe_edges keeps the old by_predicate counts (count /
#     forward / reverse) and adds qualified_by_predicate: predicate ->
#     signature -> count, listing only predicates that have at least
#     one qualified edge.
#   - _render_qualified_lines lists signatures by descending count
#     (ties by signature text) and caps the list, reporting how many
#     signatures were left out.

from __future__ import annotations

from typing import Any

from pipeline.code.pipeline import _render_qualified_lines
from pipeline.code.retriever_client import tally_probe_edges
from pipeline.code.reduction import (
    edge_qualifiers,
    reduce_response,
    render_qualifiers,
    render_table,
)

DECREASED = [
    {"qualifier_type_id": "biolink:qualified_predicate", "qualifier_value": "biolink:causes"},
    {"qualifier_type_id": "biolink:object_direction_qualifier", "qualifier_value": "decreased"},
    {"qualifier_type_id": "biolink:object_aspect_qualifier", "qualifier_value": "activity"},
]
INCREASED = [
    {"qualifier_type_id": "biolink:object_aspect_qualifier", "qualifier_value": "activity"},
    {"qualifier_type_id": "biolink:object_direction_qualifier", "qualifier_value": "increased"},
    {"qualifier_type_id": "biolink:qualified_predicate", "qualifier_value": "biolink:causes"},
]
DECREASED_SIGNATURE = (
    "biolink:object_aspect_qualifier=activity, "
    "biolink:object_direction_qualifier=decreased, "
    "biolink:qualified_predicate=biolink:causes"
)
INCREASED_SIGNATURE = (
    "biolink:object_aspect_qualifier=activity, "
    "biolink:object_direction_qualifier=increased, "
    "biolink:qualified_predicate=biolink:causes"
)


def _edge(subject: str, predicate: str, obj: str, qualifiers: Any = None) -> dict[str, Any]:
    edge: dict[str, Any] = {
        "subject": subject,
        "object": obj,
        "predicate": predicate,
        "attributes": [
            {"attribute_type_id": "biolink:knowledge_level", "value": "knowledge_assertion"},
            {"attribute_type_id": "biolink:agent_type", "value": "manual_agent"},
        ],
        "sources": [{"resource_id": "infores:chembl", "resource_role": "primary_knowledge_source"}],
    }
    if qualifiers is not None:
        edge["qualifiers"] = qualifiers
    return edge


def test_edge_qualifiers_sorts_pairs_by_type_id() -> None:
    assert edge_qualifiers(_edge("CHEBI:1", "biolink:affects", "NCBIGene:1", DECREASED)) == [
        ("biolink:object_aspect_qualifier", "activity"),
        ("biolink:object_direction_qualifier", "decreased"),
        ("biolink:qualified_predicate", "biolink:causes"),
    ]


def test_edge_qualifiers_is_empty_without_qualifiers() -> None:
    assert edge_qualifiers(_edge("CHEBI:1", "biolink:treats", "MONDO:1")) == []
    assert edge_qualifiers(_edge("CHEBI:1", "biolink:treats", "MONDO:1", None)) == []
    assert edge_qualifiers(_edge("CHEBI:1", "biolink:treats", "MONDO:1", [])) == []


def test_edge_qualifiers_skips_malformed_entries() -> None:
    messy = [
        "biolink:object_direction_qualifier",
        {"qualifier_type_id": "biolink:object_direction_qualifier"},
        {"qualifier_value": "decreased"},
        {"qualifier_type_id": 7, "qualifier_value": "decreased"},
        {"qualifier_type_id": "biolink:object_direction_qualifier", "qualifier_value": ""},
        {"qualifier_type_id": "biolink:object_direction_qualifier", "qualifier_value": "decreased"},
    ]
    assert edge_qualifiers(_edge("CHEBI:1", "biolink:affects", "NCBIGene:1", messy)) == [
        ("biolink:object_direction_qualifier", "decreased"),
    ]


def test_render_qualifiers_uses_full_biolink_ids() -> None:
    pairs = edge_qualifiers(_edge("CHEBI:1", "biolink:affects", "NCBIGene:1", DECREASED))
    assert render_qualifiers(pairs) == DECREASED_SIGNATURE
    assert render_qualifiers([]) == ""


def _qualified_response() -> dict[str, Any]:
    return {
        "message": {
            "results": [{"node_bindings": {}} for _ in range(2)],
            "knowledge_graph": {
                "nodes": {
                    "CHEBI:45783": {"name": "imatinib", "categories": ["biolink:SmallMolecule"]},
                    "NCBIGene:25": {"name": "ABL1", "categories": ["biolink:Gene"]},
                    "NCBIGene:3815": {"name": "KIT", "categories": ["biolink:Gene"]},
                },
                "edges": {
                    "e_qual": _edge("CHEBI:45783", "biolink:affects", "NCBIGene:25", DECREASED),
                    "e_plain": _edge("CHEBI:45783", "biolink:affects", "NCBIGene:3815"),
                },
            },
        },
    }


def test_reduce_response_carries_the_qualifier_string_per_row() -> None:
    reduced = reduce_response(_qualified_response(), top_k=10)
    by_id = {row["edge_id"]: row for row in reduced.rows}
    assert by_id["e_qual"]["qualifiers"] == DECREASED_SIGNATURE
    assert by_id["e_plain"]["qualifiers"] is None


def test_render_table_shows_qual_only_on_qualified_rows() -> None:
    table = render_table(reduce_response(_qualified_response(), top_k=10))
    lines = {line.split(" | ")[0]: line for line in table.splitlines() if " | " in line}
    assert f" qual=[{DECREASED_SIGNATURE}] corr=" in lines["e_qual"]
    assert " qual=" not in lines["e_plain"]
    # the header explains the new field, otherwise the model has no way
    # to know that a row without qual= states no direction at all.
    header_block = table.split("\n\n")[0]
    assert "qual=[<qualifier_type_id>=<value>, ...]" in header_block
    assert "a row without qual= states no direction" in header_block


def test_tally_probe_edges_counts_directions_and_qualified_signatures() -> None:
    pinned = "NCBIGene:1565"
    edges = {
        "a": _edge("CHEBI:1", "biolink:affects", pinned, DECREASED),
        "b": _edge("CHEBI:2", "biolink:affects", pinned, DECREASED),
        "c": _edge("CHEBI:3", "biolink:affects", pinned, INCREASED),
        "d": _edge("CHEBI:4", "biolink:affects", pinned),
        "e": _edge(pinned, "biolink:affects", "CHEBI:5"),
        "f": _edge("CHEBI:6", "biolink:physically_interacts_with", pinned),
        # an edge touching a descendant of the pinned CURIE counts but
        # carries no direction, same as before qualifiers existed.
        "g": _edge("CHEBI:7", "biolink:affects", "NCBIGene:9999"),
    }
    by_predicate, qualified_by_predicate = tally_probe_edges(edges, pinned)
    assert by_predicate == {
        "biolink:affects": {"count": 6, "forward": 1, "reverse": 4},
        "biolink:physically_interacts_with": {"count": 1, "forward": 0, "reverse": 1},
    }
    assert qualified_by_predicate == {
        "biolink:affects": {DECREASED_SIGNATURE: 2, INCREASED_SIGNATURE: 1},
    }


def test_tally_probe_edges_skips_restatements_backed_by_support_graphs() -> None:
    # Retriever restates "drug treats subtype" as "disease treats drug",
    # oriented like the probe's query edge, with a support graph; only
    # the subtype's own fact (no direction, it is a descendant) and the
    # direct fact about the pinned disease count.
    pinned = "MONDO:0008383"
    restated = {
        "subject": pinned, "predicate": "biolink:treats", "object": "CHEBI:1",
        "attributes": [{"attribute_type_id": "biolink:support_graphs", "value": ["aux_1"]}],
    }
    edges = {
        "direct": _edge("CHEBI:2", "biolink:treats", pinned),
        "subtype": _edge("CHEBI:1", "biolink:treats", "MONDO:0000001"),
        "restated": restated,
    }
    by_predicate, _ = tally_probe_edges(edges, pinned)
    assert by_predicate == {"biolink:treats": {"count": 2, "forward": 0, "reverse": 1}}


def test_tally_probe_edges_skips_edges_without_predicate() -> None:
    by_predicate, qualified_by_predicate = tally_probe_edges(
        {"x": {"subject": "A", "object": "B"}}, "A",
    )
    assert by_predicate == {}
    assert qualified_by_predicate == {}


def test_render_qualified_lines_orders_by_count_and_caps() -> None:
    qualified = {
        "sig_b": 5,
        "sig_a": 5,
        "sig_c": 30,
        "sig_d": 1,
    }
    assert _render_qualified_lines(qualified, limit=3) == [
        "      qualified: 30 edges with sig_c",
        "      qualified: 5 edges with sig_a",
        "      qualified: 5 edges with sig_b",
        "      (1 more qualifier combination not shown)",
    ]


def test_render_qualified_lines_is_empty_without_qualified_edges() -> None:
    assert _render_qualified_lines({}, limit=3) == []
