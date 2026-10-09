# config.load_translator_questions: every tr*.json record written by
# translator_assets.py holds one question in two phrasings. the runner
# and scorer need one addressable record PER phrasing, because each
# phrasing is its own benchmark cell with its own run folder.
#
# spec under test:
#   - each file expands to one record per non-empty phrasing, template
#     first, with id "<question_id>t" / "<question_id>p", nl_question
#     set to that phrasing's text, phrasing set to its name, and
#     group_id set to the file's question_id; every other field is
#     carried over unchanged.
#   - a missing / null paraphrase yields only the template record.
#   - files are read in natural order (tr2 before tr10).
#   - a missing directory yields [] (the hand-curated set still loads).

from __future__ import annotations

import json
from pathlib import Path

from pipeline.code.config import load_translator_questions


def _write(directory: Path, question_id: str, paraphrase: str | None) -> None:
    record = {
        "question_id": question_id,
        "split": "test",
        "phrasings": {"template": f"template {question_id}", "paraphrase": paraphrase},
        "predicate": "biolink:treats",
    }
    (directory / f"{question_id}.json").write_text(json.dumps(record))


def test_each_file_expands_to_one_record_per_phrasing(tmp_path: Path) -> None:
    _write(tmp_path, "tr1", "paraphrase tr1")
    records = load_translator_questions(tmp_path)
    assert [r["id"] for r in records] == ["tr1t", "tr1p"]
    template, paraphrase = records
    assert template["nl_question"] == "template tr1"
    assert template["phrasing"] == "template"
    assert paraphrase["nl_question"] == "paraphrase tr1"
    assert paraphrase["phrasing"] == "paraphrase"
    assert template["group_id"] == paraphrase["group_id"] == "tr1"
    assert template["predicate"] == "biolink:treats"
    assert template["split"] == "test"


def test_missing_paraphrase_yields_template_only(tmp_path: Path) -> None:
    _write(tmp_path, "tr1", None)
    assert [r["id"] for r in load_translator_questions(tmp_path)] == ["tr1t"]


def test_files_load_in_natural_order(tmp_path: Path) -> None:
    for question_id in ("tr10", "tr2", "tr1"):
        _write(tmp_path, question_id, None)
    assert [r["id"] for r in load_translator_questions(tmp_path)] == ["tr1t", "tr2t", "tr10t"]


def test_missing_directory_yields_nothing(tmp_path: Path) -> None:
    assert load_translator_questions(tmp_path / "absent") == []
