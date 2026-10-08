# experimental conditions beyond the plain ARAX run:
#
#   oracle modes — hand the pipeline the gold answer for a stage so the
#     stages after it are measured in isolation:
#       entity: the gold pinned entity + answer category replace
#               Stages 2-7 (entity extraction and resolution);
#       query:  additionally the gold query graph replaces Stage 8.
#   stage swaps — run one LLM stage on a different model than the rest,
#     to find out at which decision the model actually matters.
#
# spec under test:
#   - oracle_entity reads (curie, label, [category], answer_category)
#     from a curated q*.json record and from a converted Translator
#     record alike, and raises ValueError when the record carries no
#     pinned entity (an ad-hoc question has no gold to hand over).
#   - oracle_query_graph returns validation.query_graph (curated) or
#     gold_query_graph (Translator) wrapped as a TRAPI message, and
#     raises ValueError when neither exists.
#   - parse_stage_models turns ["answer_pick=m1"] into {"answer_pick":
#     "m1"}; unknown stage names, a missing "=", and a stage given
#     twice are rejected with SystemExit at the CLI boundary.
#   - condition_name is "arax" (or "lookup") for the plain run,
#     "<reasoner>_oracle_entity" / "<reasoner>_oracle_query" for oracle
#     runs, and appends "__<stage>=<id>" per swapped stage in pipeline
#     order, so every condition gets its own folder next to the plain run.

from __future__ import annotations

from typing import Any

import pytest

from pipeline.code.pipeline import (
    LLM_STAGES,
    categories_known_to_kg,
    oracle_entity,
    oracle_query_graph,
    stage8_decline,
)
from pipeline.code.runner import condition_name, parse_stage_models

CURATED: dict[str, Any] = {
    "question_id": "q1",
    "pinned_entity": {"curie": "MONDO:0005148", "label": "type 2 diabetes mellitus",
                      "category": "biolink:Disease"},
    "answer_category": "biolink:Drug",
    "validation": {"query_graph": {"nodes": {"n0": {}}, "edges": {"e0": {}}}},
}
TRANSLATOR: dict[str, Any] = {
    "question_id": "tr01",
    "pinned_entity": {"curie": "NCBIGene:1565", "label": "CYP2D6",
                      "category": "biolink:Gene", "asset_curie": "NCBIGene:1565"},
    "answer_category": "biolink:ChemicalEntity",
    "gold_query_graph": {"nodes": {"n0": {"ids": ["NCBIGene:1565"]}}, "edges": {}},
}


def test_oracle_entity_reads_curated_and_translator_records() -> None:
    assert oracle_entity(CURATED) == (
        "MONDO:0005148", "type 2 diabetes mellitus", ["biolink:Disease"], "biolink:Drug",
    )
    assert oracle_entity(TRANSLATOR) == (
        "NCBIGene:1565", "CYP2D6", ["biolink:Gene"], "biolink:ChemicalEntity",
    )


def test_oracle_entity_rejects_records_without_gold() -> None:
    with pytest.raises(ValueError, match="pinned entity"):
        oracle_entity({"id": "adhoc", "nl_question": "x"})


def test_oracle_query_graph_wraps_the_gold_graph() -> None:
    assert oracle_query_graph(CURATED) == {
        "message": {"query_graph": CURATED["validation"]["query_graph"]},
    }
    assert oracle_query_graph(TRANSLATOR) == {
        "message": {"query_graph": TRANSLATOR["gold_query_graph"]},
    }
    with pytest.raises(ValueError, match="query graph"):
        oracle_query_graph({"id": "adhoc"})


def test_llm_stages_are_the_six_llm_calls_in_order() -> None:
    assert LLM_STAGES == (
        "scope_check", "entity_extract", "candidate_pick",
        "trapi_build", "answer_pick", "explain",
    )


def test_parse_stage_models() -> None:
    assert parse_stage_models(None) == {}
    assert parse_stage_models(["answer_pick=m1", "trapi_build=m3"]) == {
        "answer_pick": "m1", "trapi_build": "m3",
    }
    for bad in (["answer_pick"], ["summary=m1"], ["answer_pick=m1", "answer_pick=m2"], ["answer_pick="]):
        with pytest.raises(SystemExit):
            parse_stage_models(bad)


def test_condition_name() -> None:
    # ARAX is the default reasoner.
    assert condition_name(None, {}) == "arax"
    assert condition_name("entity", {}) == "arax_oracle_entity"
    assert condition_name("query", {}) == "arax_oracle_query"
    # swaps are listed in pipeline order whatever order they were given.
    assert condition_name(None, {"explain": "m2", "trapi_build": "m1"}) == (
        "arax__trapi_build=m1__explain=m2"
    )
    assert condition_name("query", {"answer_pick": "m1"}) == "arax_oracle_query__answer_pick=m1"


# ---- pinned categories shown to Stage 8
#
# NodeNorm answers in a newer Biolink than the graph and the validator, so
# the pinned entity's category list can hold categories the validator
# rejects (smoke 2026-09-28: grok-4.7 copied all 16 NodeNorm categories
# into n0, including biolink:GeneOrGeneProductOrGeneFamily -> invalid).
# spec: keep only categories the KG build carries, in NodeNorm's order;
# if none survive, or the KG list is unavailable, keep the original.


def test_categories_known_to_kg() -> None:
    nodenorm = ["biolink:Gene", "biolink:GeneOrGeneProductOrGeneFamily", "biolink:Protein"]
    kg = ["biolink:Protein", "biolink:Gene", "biolink:Disease"]
    assert categories_known_to_kg(nodenorm, kg) == ["biolink:Gene", "biolink:Protein"]
    assert categories_known_to_kg(["biolink:Unknown"], kg) == ["biolink:Unknown"]
    assert categories_known_to_kg(nodenorm, []) == nodenorm
    assert categories_known_to_kg(nodenorm, None) == nodenorm


# ---- Stage 8 may decline
#
# SYS_TRAPI_BUILD tells the model to return {"error": "..."} when no
# predicate fits. that reply is not a query graph: it must end the run
# as a decline, never reach the validator (which passed it) or a reasoner
# (PloverDB answered HTTP 500; smoke 2026-09-28, glm-5.3-flash).
# spec: stage8_decline returns the stated reason for an object with an
# "error" and no "message"; None for anything that carries a message.


def test_stage8_decline() -> None:
    assert stage8_decline({"error": "no valid predicates for this category pair"}) == (
        "no valid predicates for this category pair"
    )
    assert stage8_decline({"error": ""}) == "(no reason given)"
    assert stage8_decline({"message": {"query_graph": {}}}) is None
    assert stage8_decline({"message": {}, "error": "x"}) is None


def test_condition_name_for_lookup() -> None:
    # the one-hop Tier 0 lookup is a different system from ARAX, so it
    # gets its own folders, never the old KG2 grounded/ ones.
    assert condition_name(None, {}, reasoner="lookup") == "lookup"
    assert condition_name("query", {}, reasoner="lookup") == "lookup_oracle_query"
    assert condition_name(None, {"answer_pick": "m1"}, reasoner="lookup") == "lookup__answer_pick=m1"
    assert condition_name(None, {}, reasoner="arax") == "arax"
