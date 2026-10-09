# spec for the web UI's example questions (pipeline/examples.yaml and
# GET /api/v1/questions):
#   - there are 20, each a distinct, non-empty natural-language question
#     ending in "?".
#   - the API serves them in file order as {id: "e1".., nl_question},
#     with no relation, Biolink type or entity field beside them.
#   - they are separate from the benchmark: the gold set still loads its
#     19 frozen questions.

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from pipeline.code.api import app
from pipeline.code.config import load_config, load_examples, load_questions

KEY = "test-key"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PLOVERAI_API_KEY", KEY)
    app.state.cfg = load_config()
    app.state.logger = logging.getLogger("test_examples")
    app.state.guard = None
    yield TestClient(app)


def test_twenty_distinct_questions() -> None:
    examples = load_examples(load_config())
    assert len(examples) == 20
    assert len(set(examples)) == 20
    assert all(q.endswith("?") for q in examples)


def test_api_serves_them_in_order_and_nothing_else(client: TestClient) -> None:
    body = client.get("/api/v1/questions", headers={"X-API-Key": KEY}).json()
    examples = load_examples(app.state.cfg)
    assert body["questions"] == [{"id": f"e{i}", "nl_question": q} for i, q in enumerate(examples, start=1)]


def test_the_gold_set_is_untouched() -> None:
    assert len(load_questions(load_config())) == 19
