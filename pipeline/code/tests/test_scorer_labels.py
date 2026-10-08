# scorer.py, labelled-answer extension: the converted NCATS Translator
# questions carry expert labels (TopAnswer / Acceptable / NeverShow /
# BadButForgivable ...) and, for affects questions, a direction.
#
# spec under test:
#   - labelled_curies: the union of asset_curie, canonical_curie and
#     every equivalent_curie of the answers whose label is in `labels`.
#     this is what lets an offline string comparison match a pick made
#     under any member of the answer's NodeNorm clique.
#   - build_gold_index on a Translator record: verified_curies = the
#     TopAnswer + Acceptable ids, nevershow_curies = the NeverShow ids,
#     gold_direction from the record's object_direction_qualifier, and
#     split / phrasing / group_id carried through. curated q*.json
#     records keep their old meaning (nevershow empty, no direction).
#   - nevershow_count: how many picked ENTITIES hit a NeverShow id;
#     None when the answer file is absent OR the question has no
#     NeverShow labels (a 0 there would dilute the rate).
#   - direction_match: None when the gold has no direction or there is
#     no query; otherwise True iff e0's qualifier_constraints carry an
#     object_direction_qualifier equal to the gold one. a query with no
#     direction at all is False: it did not ask the question.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.code.scorer import (
    GoldQuestion,
    build_gold_index,
    direction_match,
    discover_cells,
    labelled_curies,
    nevershow_count,
    score_cell,
    summarize,
)
from pipeline.code.config import load_config

LABELLED = [
    {"expected_output": "TopAnswer", "asset_curie": "PUBCHEM.COMPOUND:1",
     "canonical_curie": "CHEBI:1", "equivalent_curies": ["CHEBI:1", "DRUGBANK:DB1"]},
    {"expected_output": "Acceptable", "asset_curie": "CHEBI:2",
     "canonical_curie": "CHEBI:2", "equivalent_curies": []},
    {"expected_output": "NeverShow", "asset_curie": "UMLS:C3",
     "canonical_curie": "CHEBI:3", "equivalent_curies": ["UMLS:C3", "CHEBI:3"]},
    {"expected_output": "BadButForgivable", "asset_curie": "CHEBI:4",
     "canonical_curie": "CHEBI:4", "equivalent_curies": []},
]


def _query(direction: str | None) -> dict[str, Any]:
    edge: dict[str, Any] = {"subject": "n1", "object": "n0", "predicates": ["biolink:affects"]}
    if direction is not None:
        edge["qualifier_constraints"] = [{"qualifier_set": [
            {"qualifier_type_id": "biolink:qualified_predicate", "qualifier_value": "biolink:causes"},
            {"qualifier_type_id": "biolink:object_direction_qualifier", "qualifier_value": direction},
        ]}]
    return {"message": {"query_graph": {"nodes": {}, "edges": {"e0": edge}}}}


def _answer(*curies: str) -> dict[str, Any]:
    return {
        "answers": [{"curie": c} for c in curies],
        "canonical_curies": list(curies),
    }


def test_labelled_curies_unions_every_clique_member() -> None:
    assert labelled_curies(LABELLED, frozenset({"TopAnswer", "Acceptable"})) == frozenset({
        "PUBCHEM.COMPOUND:1", "CHEBI:1", "DRUGBANK:DB1", "CHEBI:2",
    })
    assert labelled_curies(LABELLED, frozenset({"NeverShow"})) == frozenset({"UMLS:C3", "CHEBI:3"})
    assert labelled_curies([], frozenset({"NeverShow"})) == frozenset()


def test_nevershow_count_counts_entities() -> None:
    nevershow = frozenset({"UMLS:C3", "CHEBI:3"})
    assert nevershow_count(_answer("CHEBI:3", "CHEBI:1", "CHEBI:9"), nevershow) == 1
    assert nevershow_count(_answer("CHEBI:1"), nevershow) == 0
    assert nevershow_count(None, nevershow) is None
    assert nevershow_count(_answer("CHEBI:3"), frozenset()) is None


def test_direction_match() -> None:
    assert direction_match(_query("decreased"), "decreased") is True
    assert direction_match(_query("increased"), "decreased") is False
    assert direction_match(_query(None), "decreased") is False
    assert direction_match(_query("decreased"), None) is None
    assert direction_match(None, "decreased") is None


def test_gold_question_defaults_keep_curated_records_unchanged() -> None:
    gold = GoldQuestion(
        q_id="q1", nl_question="x", pinned_curie="MONDO:1",
        predicate="biolink:treats", verified_curies=frozenset({"CHEBI:1"}),
    )
    assert gold.nevershow_curies == frozenset()
    assert gold.gold_direction is None
    assert gold.split is None


def test_build_gold_index_reads_translator_records() -> None:
    # offline: reads the committed tr*.json files only.
    gold = build_gold_index(load_config())
    assert gold["q1"].split == "dev"
    assert gold["q1"].nevershow_curies == frozenset()
    translator = [g for g in gold.values() if g.group_id is not None]
    assert translator, "converted Translator questions should be indexed"
    first = translator[0]
    assert first.split == "test"
    assert first.phrasing in ("template", "paraphrase")
    assert first.group_id is not None and first.q_id.startswith(first.group_id)
    directed = [g for g in translator if g.gold_direction is not None]
    assert {g.gold_direction for g in directed} == {"decreased", "increased"}


# ---- conditions: oracle modes and stage swaps live in sibling folders
#
# spec: discover_cells walks EVERY condition folder under a model
# folder (grounded/, oracle_query/, grounded__answer_pick=m1/ ...),
# records the folder name as the cell's condition, and summarize keeps
# one summary row per (model, condition) so an oracle run is never
# averaged into the plain run.


def _cell(root: Path, condition: str, q_id: str = "q1") -> None:
    cell = root / "RUN_2026-09-28T00-00-00Z" / "m8_openai_gpt-6-luna" / condition / q_id
    cell.mkdir(parents=True)
    (cell / "meta.json").write_text(json.dumps({"status": "ok", "outcome": "answered",
                                                "n_results": 3, "elapsed_s": 1.0}))


def test_discover_cells_reads_every_condition_folder(tmp_path: Path) -> None:
    for condition in ("grounded", "oracle_query", "grounded__answer_pick=m1"):
        _cell(tmp_path, condition)
    found = discover_cells(tmp_path, frozenset({"q1"}))
    assert sorted(c.condition for c in found) == [
        "grounded", "grounded__answer_pick=m1", "oracle_query",
    ]
    gold = GoldQuestion(q_id="q1", nl_question="x", pinned_curie="MONDO:1",
                        predicate="biolink:treats", verified_curies=frozenset())
    summaries = summarize([score_cell(c, gold) for c in found])
    assert [(s.model_id, s.condition, s.n_cells) for s in summaries] == [
        ("m8", "grounded", 1),
        ("m8", "grounded__answer_pick=m1", 1),
        ("m8", "oracle_query", 1),
    ]


# ---- abstention: gold records with expected_behavior = "abstain"
#
# spec: q14 / q15 ask questions whose correct one-hop query returns
# nothing in KG2, so the right behaviour was to decline. for such a
# record GoldQuestion.expects_abstain is True and a cell gets
#   abstained = True   when the pipeline ran and declined
#                      (outcome no_results or no_answer_picked),
#   abstained = False  when it answered anyway,
#   abstained = None   when it never got to decide (crash, llm_error,
#                      entity failure: outcome None);
# answer_match / verified_hits / n_picked / verified_hit_rate are None,
# because there is no gold answer to match. every other question has
# abstained = None.


def _abstain_cell(root: Path, q_id: str, outcome: str | None, status: str = "ok") -> Path:
    cell = root / "RUN_2026-09-28T00-00-00Z" / "m8_openai_gpt-6-luna" / "grounded" / q_id
    cell.mkdir(parents=True)
    (cell / "meta.json").write_text(json.dumps({
        "status": status, "outcome": outcome, "n_results": 0, "elapsed_s": 1.0,
    }))
    return cell


def test_abstention_is_scored_only_where_gold_expects_it(tmp_path: Path) -> None:
    abstain_gold = GoldQuestion(
        q_id="q14", nl_question="x", pinned_curie="NCBIGene:59272",
        predicate="biolink:expressed_in", verified_curies=frozenset(),
        expects_abstain=True,
    )
    plain_gold = GoldQuestion(
        q_id="q1", nl_question="x", pinned_curie="MONDO:1",
        predicate="biolink:treats", verified_curies=frozenset({"CHEBI:1"}),
    )
    for outcome, expected in (("no_results", True), ("no_answer_picked", True),
                              ("answered", False), (None, None)):
        _abstain_cell(tmp_path / str(outcome), "q14", outcome)
        cell = discover_cells(tmp_path / str(outcome), frozenset({"q14"}))[0]
        row = score_cell(cell, abstain_gold)
        assert row.abstained is expected
        assert row.answer_match is None
        assert row.verified_hits is None
    _abstain_cell(tmp_path / "plain", "q1", "no_results")
    plain = score_cell(discover_cells(tmp_path / "plain", frozenset({"q1"}))[0], plain_gold)
    assert plain.abstained is None


def test_build_gold_index_marks_abstain_records() -> None:
    gold = build_gold_index(load_config())
    assert {q for q, g in gold.items() if g.expects_abstain} == {"q14", "q15", "q18", "q19"}
    assert not gold["q1"].expects_abstain


# ---- questions with no positive labels
#
# about a fifth of the converted Translator questions carry only
# NeverShow (or BadButForgivable) labels. there is nothing to hit, so
# "matched none" is not a miss: answer_match, verified_hits and
# verified_hit_rate are None, while n_picked and nevershow_picked
# still describe what the model did.


def test_no_positive_labels_means_no_answer_match(tmp_path: Path) -> None:
    gold = GoldQuestion(
        q_id="tr99t", nl_question="x", pinned_curie="MONDO:1",
        predicate="biolink:treats", verified_curies=frozenset(),
        nevershow_curies=frozenset({"CHEBI:3"}),
    )
    cell_dir = tmp_path / "RUN_2026-09-28T00-00-00Z" / "m8_openai_gpt-6-luna" / "grounded" / "tr99t"
    cell_dir.mkdir(parents=True)
    (cell_dir / "meta.json").write_text(json.dumps({
        "status": "ok", "outcome": "answered", "n_results": 4, "elapsed_s": 1.0,
    }))
    (cell_dir / "answer.json").write_text(json.dumps({
        "answers": [{"curie": "CHEBI:3"}, {"curie": "CHEBI:9"}],
        "canonical_curies": ["CHEBI:3", "CHEBI:9"],
    }))
    row = score_cell(discover_cells(tmp_path, frozenset({"tr99t"}))[0], gold)
    assert row.answer_match is None
    assert row.verified_hits is None
    assert row.verified_hit_rate is None
    assert row.n_picked == 2
    assert row.nevershow_picked == 1
