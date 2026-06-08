# load_questions must normalise the gold files' `question_id` field to the
# `id` key the pipeline + runner read everywhere (q["id"]). the gold files
# key it as `question_id`; without normalisation the runner KeyErrors on
# `--questions q1` and the gold set cannot run. pure file read, no network.

from code.config import load_config, load_questions


def test_every_gold_question_exposes_id_matching_question_id():
    qs = load_questions(load_config())
    assert qs, "no gold questions loaded"
    for q in qs:
        assert q.get("id"), f"gold question missing id (question_id={q.get('question_id')})"
        assert q["id"] == q.get("question_id")
