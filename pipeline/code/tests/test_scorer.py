# tests for code/scorer.py — the offline benchmark scorer.
#
# spec under test:
#   - score_cell produces exactly one row per (run, model, gold
#     question) folder and NEVER raises, however little of the cell
#     was written before the run died.
#   - a metric is None when its stage never ran (no artifact to read),
#     False when the stage ran and the answer is no. the one exception
#     is entity_extract_ok, which is a plain bool because "no
#     nameres.json" and "empty mention" mean the same thing.
#   - answer_match is exact CURIE equality over the union of
#     answers[].curie and canonical_curies, so a pick whose raw id
#     differs from gold still matches through its canonical form.
#   - verified_hit_rate divides by the number of picked ENTITIES, not
#     the number of CURIE strings, so canonicalization cannot inflate
#     the denominator.
#   - citation resolution: an edge id counts as supported if it is in
#     reduced_data rows or in answer.json supporting_edge_ids; a PMID
#     counts as supported only if it is in a reduced row's
#     publications. pre-reduction cells have no reduced_data.json, so
#     the edge pool falls back to supporting_edge_ids and every cited
#     PMID is unsupported.
#   - discover_cells walks RUN_*/<model>/<condition>/<q_id>/ and skips
#     adhoc and any q_id that is not in the gold set.
#   - summarize computes each rate over the cells where the metric is
#     not None and reports the excluded count alongside it.
#
# every fixture builds a synthetic cell tree under pytest's tmp_path;
# nothing here touches code/outputs/ or the network.

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from pipeline.code.scorer import (
    CellPath,
    GoldQuestion,
    answer_match,
    build_gold_index,
    citation_audit,
    discover_cells,
    entity_extract_ok,
    model_id_from_dir,
    nameres_top1_match,
    parse_citations,
    query_executed,
    predicate_match,
    score_cell,
    summarize,
    trapi_predicates,
    unique_stamp,
    valid_trapi,
    verified_hit_counts,
    write_rows_csv,
    write_summary_csv,
)
from pipeline.code.config import load_config


GOLD_Q1 = GoldQuestion(
    q_id="q1",
    nl_question="What drugs treat type 2 diabetes mellitus?",
    pinned_curie="MONDO:0005148",
    predicate="biolink:treats",
    verified_curies=frozenset({"CHEBI:6801", "CHEBI:5384"}),
)


def _meta(
    *,
    status: str = "ok",
    outcome: str | None = "answered",
    n_results: int = 126,
    answers_n_picked: int = 2,
    elapsed_s: float = 51.288,
) -> dict[str, Any]:
    return {
        "q_id": "q1",
        "status": status,
        "outcome": outcome,
        "outcome_reason": None,
        "n_results": n_results,
        "answers_n_picked": answers_n_picked,
        "error": None,
        "elapsed_s": elapsed_s,
    }


def _cost() -> dict[str, Any]:
    return {
        "stages": [{"stage": "explain", "input_tokens": 100, "output_tokens": 20}],
        "totals": {"input_tokens": 36907, "output_tokens": 4921, "total_usd": 0.046135},
    }


def _reduced(edge_id: str = "46524754", publications: list[str] | None = None) -> dict[str, Any]:
    return {
        "top_k": 300,
        "total_results": 126,
        "rows": [{
            "edge_id": edge_id,
            "subject_curie": "CHEBI:6801",
            "predicate": "biolink:treats",
            "object_curie": "MONDO:0005148",
            "publications": publications if publications is not None else ["PMID:14684759"],
        }],
    }


def _answer(
    curies: list[str],
    canonical: list[str] | None = None,
    supporting: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "answers": [
            {
                "curie": c,
                "label": c.lower(),
                "supporting_edge_ids": supporting if supporting is not None else ["46524754"],
            }
            for c in curies
        ],
        "evidence_tier": "knowledge_assertion",
        "rationale": "synthetic",
        "canonical_curies": canonical if canonical is not None else list(curies),
    }


def _write_cell(
    outputs_root: Path,
    *,
    run: str = "RUN_2026-09-01T16-49-43Z",
    model_dir: str = "m6_google_gemini-3.7-flash",
    q_id: str = "q1",
    json_files: dict[str, Any] | None = None,
    explanation: str | None = None,
) -> CellPath:
    cell_dir = outputs_root / run / model_dir / "arax" / q_id
    cell_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (json_files or {}).items():
        (cell_dir / name).write_text(json.dumps(payload, indent=2))
    if explanation is not None:
        (cell_dir / "explanation.md").write_text(explanation)
    return CellPath(
        run_id=run.removeprefix("RUN_"),
        model_id=model_id_from_dir(model_dir),
        model_dir=model_dir,
        q_id=q_id,
        root=cell_dir,
    )


HAPPY_EXPLANATION = (
    "## Answer\n\nmetformin (CHEBI:6801) treats type 2 diabetes mellitus.\n\n"
    "## Evidence\n\n- **metformin (CHEBI:6801)** — approved therapy "
    "[edge:46524754] corroborated by literature [PMID:14684759].\n"
)


def _happy_files() -> dict[str, Any]:
    return {
        "meta.json": _meta(),
        "cost.json": _cost(),
        "nameres.json": {"mention": "type 2 diabetes mellitus", "chosen_curie": "MONDO:0005148"},
        "nodenorm.json": {"pinned": {"canonical_curie": "MONDO:0005148"}},
        "validation.json": {"passed": True, "errors": {}, "warnings": {}},
        "trapi_query.json": {
            "message": {"query_graph": {
                "nodes": {"n0": {"categories": ["biolink:Drug"]}},
                "edges": {"e0": {
                    "subject": "n0", "object": "n1", "predicates": ["biolink:treats"],
                }},
            }},
        },
        "reduced_data.json": _reduced(),
        "answer.json": _answer(["CHEBI:6801"]),
    }


# ------------------------------------------------------------ happy path


def test_happy_cell_scores_every_metric_true(tmp_path: Path) -> None:
    cell = _write_cell(tmp_path, json_files=_happy_files(), explanation=HAPPY_EXPLANATION)
    row = score_cell(cell, GOLD_Q1)

    assert row.run_id == "2026-09-01T16-49-43Z"
    assert row.model_id == "m6"
    assert row.model_dir == "m6_google_gemini-3.7-flash"
    assert row.q_id == "q1"
    assert row.status == "ok"
    assert row.outcome == "answered"
    assert row.elapsed_s == 51.288
    assert row.cost_usd == 0.046135
    assert row.input_tokens == 36907
    assert row.output_tokens == 4921
    assert row.entity_extract_ok is True
    assert row.nameres_top1_match is True
    assert row.valid_trapi is True
    assert row.executed is True
    assert row.predicate_match is True
    assert row.predicate_used == "biolink:treats"
    assert row.answer_match is True
    assert row.verified_hits == 1
    assert row.n_picked == 1
    assert row.verified_hit_rate == 1.0
    # two distinct tokens cited (one edge, one PMID), both resolvable.
    assert (row.citations_cited, row.citations_resolved) == (2, 2)
    assert row.unsupported_citation_count == 0


# ------------------------------------------------------------- wrong entity


def test_wrong_entity_cell(tmp_path: Path) -> None:
    # the pipeline pinned type 1 diabetes instead of type 2 and then
    # queried it happily: every downstream stage ran, so only the two
    # gold-comparing metrics go False. this is the case the per-stage
    # breakdown exists to separate from a crash.
    files = _happy_files()
    files["nodenorm.json"] = {"pinned": {"canonical_curie": "MONDO:0005147"}}
    files["answer.json"] = _answer(["CHEBI:1234", "CHEBI:5678"])
    cell = _write_cell(tmp_path, model_dir="m5_anthropic_claude-haiku-4.5", json_files=files)
    row = score_cell(cell, GOLD_Q1)

    assert row.model_id == "m5"
    assert row.nameres_top1_match is False
    assert row.valid_trapi is True
    assert row.executed is True
    assert row.predicate_match is True
    assert row.answer_match is False
    assert (row.verified_hits, row.n_picked, row.verified_hit_rate) == (0, 2, 0.0)


def test_widened_predicate_does_not_match(tmp_path: Path) -> None:
    # asking for treats OR in_clinical_trials_for is not the gold
    # question, even though the gold predicate is in the list.
    files = _happy_files()
    files["trapi_query.json"]["message"]["query_graph"]["edges"]["e0"]["predicates"] = [
        "biolink:treats", "biolink:in_clinical_trials_for",
    ]
    cell = _write_cell(tmp_path, json_files=files)
    row = score_cell(cell, GOLD_Q1)

    assert row.predicate_match is False
    assert row.predicate_used == "biolink:treats, biolink:in_clinical_trials_for"


# --------------------------------------------------------------- failures


def test_failed_cell_with_meta_only(tmp_path: Path) -> None:
    # an HTTP 429 at Stage 1: meta.json is the only artifact on disk.
    # every stage-dependent metric is None, the row still exists, and
    # nothing raises.
    cell = _write_cell(
        tmp_path,
        run="RUN_2026-09-01T18-30-21Z",
        model_dir="m9_inclusionai_ling-3.0-flash",
        json_files={"meta.json": _meta(
            status="llm_error", outcome=None,
            n_results=-1, answers_n_picked=-1, elapsed_s=6.912,
        )},
    )
    row = score_cell(cell, GOLD_Q1)

    assert row.status == "llm_error"
    assert row.outcome is None
    assert row.elapsed_s == 6.912
    assert row.cost_usd is None
    assert row.input_tokens is None and row.output_tokens is None
    assert row.entity_extract_ok is False
    assert row.nameres_top1_match is None
    assert row.valid_trapi is None
    # -1 is the pipeline's "never got there" sentinel, so this is a
    # definite False, not an unknown.
    assert row.executed is False
    assert row.predicate_match is None
    assert row.predicate_used is None
    assert row.answer_match is None
    assert row.verified_hits is None and row.n_picked is None
    assert row.verified_hit_rate is None
    assert row.citations_cited is None
    assert row.unsupported_citation_count is None


def test_completely_empty_cell_still_yields_a_row(tmp_path: Path) -> None:
    cell = _write_cell(tmp_path, json_files={})
    row = score_cell(cell, GOLD_Q1)

    assert row.status is None
    assert row.executed is None
    assert row.entity_extract_ok is False
    assert row.answer_match is None


def test_missing_answer_json(tmp_path: Path) -> None:
    # a crashed cell that got as far as the reasoner: the query-side
    # metrics are real, everything answer-side is None.
    files = _happy_files()
    del files["answer.json"]
    files["meta.json"] = _meta(
        status="crashed", outcome=None, n_results=-1, answers_n_picked=-1,
    )
    cell = _write_cell(
        tmp_path,
        run="RUN_2026-09-01T18-16-07Z",
        model_dir="m10_deepseek_deepseek-v4-flash-0731",
        json_files=files,
    )
    row = score_cell(cell, GOLD_Q1)

    assert row.status == "crashed"
    assert row.model_id == "m10"
    assert row.nameres_top1_match is True
    assert row.valid_trapi is True
    assert row.predicate_match is True
    assert row.answer_match is None
    assert row.verified_hits is None
    assert row.n_picked is None
    assert row.verified_hit_rate is None


def test_unparseable_artifact_is_treated_as_absent(tmp_path: Path) -> None:
    cell = _write_cell(tmp_path, json_files=_happy_files())
    (cell.root / "validation.json").write_text("{ truncated json")
    row = score_cell(cell, GOLD_Q1)

    assert row.valid_trapi is None
    assert row.nameres_top1_match is True


# --------------------------------------------------------------- matching


def test_answer_match_via_canonical_when_raw_id_differs(tmp_path: Path) -> None:
    # Stage 11 picked DRUGBANK:DB00331; Stage 12 canonicalized it to
    # CHEBI:6801, which is what gold records. the raw id alone would
    # miss, the canonical form matches.
    files = _happy_files()
    files["answer.json"] = _answer(["DRUGBANK:DB00331"], canonical=["CHEBI:6801"])
    cell = _write_cell(tmp_path, json_files=files)
    row = score_cell(cell, GOLD_Q1)

    assert row.answer_match is True
    # one entity picked, not two, even though two CURIE strings name it.
    assert (row.verified_hits, row.n_picked, row.verified_hit_rate) == (1, 1, 1.0)


def test_answer_match_is_exact_id_equality(tmp_path: Path) -> None:
    # the drug/chemical conflation caveat, pinned as a test: an id from
    # a different vocabulary for the same molecule does NOT match
    # offline, because collapsing the clique would need a live
    # NodeNorm call and would make the score date-dependent.
    files = _happy_files()
    files["answer.json"] = _answer(["RXCUI:6809"], canonical=["RXCUI:6809"])
    cell = _write_cell(tmp_path, json_files=files)
    row = score_cell(cell, GOLD_Q1)

    assert row.answer_match is False


def test_verified_hit_rate_math(tmp_path: Path) -> None:
    # five picks, two of them in the gold subset.
    files = _happy_files()
    files["answer.json"] = _answer(
        ["CHEBI:6801", "CHEBI:5384", "CHEBI:8228", "CHEBI:50122", "CHEBI:2376"],
    )
    cell = _write_cell(tmp_path, json_files=files)
    row = score_cell(cell, GOLD_Q1)

    assert row.answer_match is True
    assert row.verified_hits == 2
    assert row.n_picked == 5
    assert row.verified_hit_rate == 0.4


def test_empty_pick_list_gives_zero_picks_and_no_rate(tmp_path: Path) -> None:
    files = _happy_files()
    files["answer.json"] = _answer([])
    cell = _write_cell(tmp_path, json_files=files)
    row = score_cell(cell, GOLD_Q1)

    assert row.answer_match is False
    assert (row.verified_hits, row.n_picked) == (0, 0)
    assert row.verified_hit_rate is None


# -------------------------------------------------------------- citations


def test_citation_resolution_flags_unsupported_pmid(tmp_path: Path) -> None:
    # four distinct tokens: one real edge, one invented edge, one real
    # PMID, one invented PMID. two resolve, two do not.
    explanation = (
        "## Evidence\n\n"
        "- **metformin (CHEBI:6801)** — [edge:46524754, edge:99999999] "
        "and [PMID:14684759, PMID:99999999].\n"
        "- repeated citation of the same edge [edge:46524754] counts once.\n"
    )
    files = _happy_files()
    cell = _write_cell(tmp_path, json_files=files, explanation=explanation)
    row = score_cell(cell, GOLD_Q1)

    assert row.citations_cited == 4
    assert row.citations_resolved == 2
    assert row.unsupported_citation_count == 2


def test_pre_reduction_cell_resolves_edges_from_answer_only(tmp_path: Path) -> None:
    # runs made before the Stage 11 reduction step have no
    # reduced_data.json. the edge pool falls back to answer.json's
    # supporting_edge_ids, and the PMID pool is empty, so every cited
    # PMID is unsupported.
    files = _happy_files()
    del files["reduced_data.json"]
    files["answer.json"] = _answer(["CHEBI:6801"], supporting=["46524754", "46640401"])
    explanation = (
        "- [edge:46524754] and [edge:46640401] "
        "with literature [PMID:14684759].\n"
    )
    cell = _write_cell(
        tmp_path, run="RUN_2026-09-01T14-33-07Z", json_files=files, explanation=explanation,
    )
    row = score_cell(cell, GOLD_Q1)

    assert row.citations_cited == 3
    assert row.citations_resolved == 2
    assert row.unsupported_citation_count == 1


def test_parse_citations_ignores_trailing_punctuation() -> None:
    edges, pmids = parse_citations(
        "see [edge:46524754]. also PMID:14684759, and PMID:14684759 again."
    )
    assert edges == frozenset({"46524754"})
    assert pmids == frozenset({"PMID:14684759"})


def test_citation_audit_is_none_without_an_explanation() -> None:
    assert citation_audit(None, _reduced(), _answer(["CHEBI:6801"])) == (None, None, None)


# --------------------------------------------------- individual predicates


def test_metric_functions_on_missing_inputs() -> None:
    assert entity_extract_ok(None) is False
    assert entity_extract_ok({"mention": "   "}) is False
    assert entity_extract_ok({"mention": "metformin"}) is True
    assert nameres_top1_match(None, "MONDO:0005148") is None
    assert nameres_top1_match({"pinned": {}}, "MONDO:0005148") is None
    assert valid_trapi(None) is None
    assert valid_trapi({"passed": False}) is False
    assert query_executed({"n_results": 0}) is True
    # runs made before the Tier 0 move wrote the count as plover_n_results.
    assert query_executed({"plover_n_results": 0}) is True
    assert query_executed({}) is None
    assert trapi_predicates({"message": {"query_graph": {"edges": {}}}}) is None
    assert predicate_match(None, "biolink:treats") is None
    assert answer_match(None, frozenset({"CHEBI:6801"})) is None
    assert verified_hit_counts(None, frozenset()) == (None, None, None)


# -------------------------------------------------------------- discovery


def test_discover_cells_skips_adhoc_and_non_gold(tmp_path: Path) -> None:
    _write_cell(tmp_path, q_id="q1", json_files={"meta.json": _meta()})
    _write_cell(tmp_path, q_id="adhoc", json_files={"meta.json": _meta()})
    _write_cell(tmp_path, q_id="q7", json_files={"meta.json": _meta()})
    _write_cell(
        tmp_path, run="RUN_2026-09-01T18-31-06Z",
        model_dir="m9_inclusionai_ling-3.0-flash", q_id="q1",
        json_files={"meta.json": _meta()},
    )
    # an older layout that wrote artifacts straight under the model
    # folder must not be picked up as a cell.
    (tmp_path / "RUN_2026-04-29T09-57-59Z" / "m5_anthropic_claude-haiku-4.5" / "q1").mkdir(
        parents=True,
    )

    found = discover_cells(tmp_path, frozenset({"q1", "q7"}))
    assert [(c.run_id, c.model_id, c.q_id) for c in found] == [
        ("2026-09-01T16-49-43Z", "m6", "q1"),
        ("2026-09-01T16-49-43Z", "m6", "q7"),
        ("2026-09-01T18-31-06Z", "m9", "q1"),
    ]

    restricted = discover_cells(
        tmp_path, frozenset({"q1", "q7"}), ["RUN_2026-09-01T18-31-06Z"],
    )
    assert [c.model_id for c in restricted] == ["m9"]

    assert discover_cells(tmp_path / "nope", frozenset({"q1"})) == []


def test_model_id_from_dir() -> None:
    assert model_id_from_dir("m6_google_gemini-3.7-flash") == "m6"
    assert model_id_from_dir("m10_deepseek_deepseek-v4-flash-0731") == "m10"
    assert model_id_from_dir("m12_z-ai_glm-5.3-flash") == "m12"
    assert model_id_from_dir("weird") == "weird"


def test_build_gold_index_reads_the_committed_gold_set() -> None:
    # offline: load_config + load_questions only read local files.
    gold = build_gold_index(load_config())
    assert "q1" in gold
    assert gold["q1"].pinned_curie == "MONDO:0005148"
    assert gold["q1"].predicate == "biolink:treats"
    assert "CHEBI:6801" in gold["q1"].verified_curies


# ------------------------------------------------------------ aggregation


def test_summary_rates_exclude_none_and_count_them(tmp_path: Path) -> None:
    good = _write_cell(tmp_path, json_files=_happy_files(), explanation=HAPPY_EXPLANATION)
    dead = _write_cell(
        tmp_path, run="RUN_2026-09-01T18-30-21Z", q_id="q1",
        json_files={"meta.json": _meta(
            status="llm_error", outcome=None, n_results=-1, answers_n_picked=-1,
        )},
    )
    rows = [score_cell(good, GOLD_Q1), score_cell(dead, GOLD_Q1)]
    summaries = summarize(rows)

    assert len(summaries) == 1
    s = summaries[0]
    assert s.model_id == "m6"
    assert s.n_cells == 2
    assert s.answered_rate == 0.5
    assert s.entity_extract_ok_rate == 0.5
    # one cell had no nodenorm.json, so the rate is 1/1 with one None.
    assert s.nameres_top1_match_rate == 1.0
    assert s.nameres_top1_match_n_none == 1
    assert s.valid_trapi_rate == 1.0
    assert s.valid_trapi_n_none == 1
    # both cells have a meta.json, so query_executed is known for both.
    assert s.executed_rate == 0.5
    assert s.executed_n_none == 0
    assert s.answer_match_rate == 1.0
    assert s.answer_match_n_none == 1
    assert s.mean_verified_hit_rate == 1.0
    assert s.verified_hit_rate_n_none == 1
    assert s.total_citations_cited == 2
    assert s.total_unsupported_citations == 0
    assert s.total_cost_usd == 0.046135
    assert s.mean_cost_usd == 0.046135


def test_summary_sorts_models_numerically(tmp_path: Path) -> None:
    cells = [
        _write_cell(tmp_path, model_dir=name, json_files={"meta.json": _meta()})
        for name in ("m10_deepseek_deepseek-v4-flash-0731",
                     "m2_google_gemini-3.1-pro-preview",
                     "m9_inclusionai_ling-3.0-flash")
    ]
    summaries = summarize([score_cell(c, GOLD_Q1) for c in cells])
    assert [s.model_id for s in summaries] == ["m2", "m9", "m10"]


# ----------------------------------------------------------------- output


def test_csv_writers_render_none_as_blank_and_bools_lowercase(tmp_path: Path) -> None:
    good = _write_cell(tmp_path, json_files=_happy_files(), explanation=HAPPY_EXPLANATION)
    dead = _write_cell(
        tmp_path, run="RUN_2026-09-01T18-30-21Z",
        json_files={"meta.json": _meta(status="crashed", outcome=None)},
    )
    rows = [score_cell(good, GOLD_Q1), score_cell(dead, GOLD_Q1)]

    scores_path = tmp_path / "scores" / "scores_x.csv"
    summary_path = tmp_path / "scores" / "summary_x.csv"
    write_rows_csv(scores_path, rows)
    write_summary_csv(summary_path, summarize(rows))

    with scores_path.open(newline="") as handle:
        written = list(csv.DictReader(handle))
    # the identity columns lead; condition sits with them because an
    # oracle or stage-swap cell is a different experimental unit.
    assert list(written[0])[:5] == ["run_id", "model_id", "model_dir", "condition", "q_id"]
    assert written[0]["condition"] == "arax"
    assert written[0]["answer_match"] == "true"
    assert written[0]["verified_hit_rate"] == "1"
    assert written[1]["answer_match"] == ""
    assert written[1]["cost_usd"] == ""

    with summary_path.open(newline="") as handle:
        summary_rows = list(csv.DictReader(handle))
    assert summary_rows[0]["model_id"] == "m6"
    assert summary_rows[0]["n_cells"] == "2"


def test_unique_stamp_never_overwrites_an_existing_pair(tmp_path: Path) -> None:
    # two scorer invocations inside the same UTC second must not clobber
    # each other: an existing artifact is never overwritten.
    assert unique_stamp(tmp_path, "2026-09-01T19-16-13Z") == "2026-09-01T19-16-13Z"
    (tmp_path / "scores_2026-09-01T19-16-13Z.csv").write_text("")
    assert unique_stamp(tmp_path, "2026-09-01T19-16-13Z") == "2026-09-01T19-16-13Z_2"
    (tmp_path / "summary_2026-09-01T19-16-13Z_2.csv").write_text("")
    assert unique_stamp(tmp_path, "2026-09-01T19-16-13Z") == "2026-09-01T19-16-13Z_3"


def test_scoring_is_deterministic(tmp_path: Path) -> None:
    cell = _write_cell(tmp_path, json_files=_happy_files(), explanation=HAPPY_EXPLANATION)
    assert score_cell(cell, GOLD_Q1) == score_cell(cell, GOLD_Q1)
