# the two indexes built once from Retriever's /meta_knowledge_graph and
# shared by the FastAPI service and the CLI runner, so the benchmark and
# the UI run the same pipeline:
#
#   build_predicate_index: (subject_category, object_category) -> sorted,
#     de-duplicated predicates; edges missing any of the three are skipped.
#   build_category_set: every "biolink:" category named as a node key or
#     as an edge's subject / object, sorted; anything else is ignored.

from __future__ import annotations

from typing import Any

from pipeline.code.retriever_client import build_category_set, build_predicate_index

META_KG: dict[str, Any] = {
    "nodes": {"biolink:Disease": {}, "biolink:Cell": {}, "not-a-category": {}},
    "edges": [
        {"subject": "biolink:Drug", "object": "biolink:Disease", "predicate": "biolink:treats"},
        {"subject": "biolink:Drug", "object": "biolink:Disease", "predicate": "biolink:affects"},
        {"subject": "biolink:Drug", "object": "biolink:Disease", "predicate": "biolink:treats"},
        {"subject": "biolink:Gene", "object": "biolink:Disease", "predicate": None},
        {"subject": "biolink:Gene", "object": "biolink:Protein", "predicate": "biolink:related_to"},
    ],
}


def test_build_predicate_index_dedups_and_sorts() -> None:
    assert build_predicate_index(META_KG) == {
        ("biolink:Drug", "biolink:Disease"): ["biolink:affects", "biolink:treats"],
        ("biolink:Gene", "biolink:Protein"): ["biolink:related_to"],
    }


def test_build_category_set_reads_nodes_and_edges() -> None:
    # biolink:Cell exists only as a node key: an edge-only index (what
    # the runner used before) would have missed it.
    assert build_category_set(META_KG) == [
        "biolink:Cell", "biolink:Disease", "biolink:Drug", "biolink:Gene", "biolink:Protein",
    ]


def test_empty_meta_kg() -> None:
    assert build_predicate_index({}) == {}
    assert build_category_set({}) == []
