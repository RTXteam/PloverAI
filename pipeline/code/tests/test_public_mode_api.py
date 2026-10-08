# spec for the public site wiring in api.py. no network, no LLM: the
# lifespan (which calls Retriever and ARAX) is not run, and every case
# below must be decided before any pipeline work starts.
#
#   - /api/v1/models lists only cfg.public.models when public mode is on,
#     and every model when it is off.
#   - /api/v1/runs returns the same shared history with public mode on
#     as off: every visitor sees every run.
#   - a question for a model outside cfg.public.models gets 403, and a
#     reasoner other than ARAX gets 403.
#   - a question the guard refuses gets the guard's status, reason and
#     Retry-After header, on both the plain and the streaming endpoint.

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from pipeline.code.api import app
from pipeline.code.config import load_config
from pipeline.code.public_guard import PublicGuard

KEY = "test-key"
HEADERS = {"X-API-Key": KEY}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PLOVERAI_API_KEY", KEY)
    app.state.cfg = load_config()
    app.state.logger = logging.getLogger("test_public_mode_api")
    app.state.guard = None
    yield TestClient(app)
    app.state.guard = None


def turn_public_on(questions_per_day: int = 300) -> None:
    limits = replace(app.state.cfg.public, questions_per_day=questions_per_day)
    app.state.guard = PublicGuard(limits)


def test_models_off_public_lists_every_model(client: TestClient) -> None:
    listed = [m["id"] for m in client.get("/api/v1/models", headers=HEADERS).json()["models"]]
    assert listed == [m.id for m in app.state.cfg.models]


def test_models_on_public_lists_only_allowed(client: TestClient) -> None:
    turn_public_on()
    listed = [m["id"] for m in client.get("/api/v1/models", headers=HEADERS).json()["models"]]
    assert listed == ["m8"]


def test_history_is_shared_on_public(client: TestClient) -> None:
    private = client.get("/api/v1/runs", headers=HEADERS)
    turn_public_on()
    public = client.get("/api/v1/runs", headers=HEADERS)
    assert public.status_code == 200
    assert public.json() == private.json()


def test_expensive_model_is_refused(client: TestClient) -> None:
    turn_public_on()
    response = client.post(
        "/api/v1/query", headers=HEADERS, json={"question": "q", "model": "m1"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "model 'm1' is not available on the public site"}


def test_lookup_reasoner_is_refused(client: TestClient) -> None:
    turn_public_on()
    response = client.post(
        "/api/v1/query",
        headers=HEADERS,
        json={"question": "q", "model": "m8", "reasoner": "lookup"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "the public site answers with ARAX only"}


@pytest.mark.parametrize("path", ["/api/v1/query", "/api/v1/query/stream"])
def test_guard_refusal_reaches_the_caller(client: TestClient, path: str) -> None:
    turn_public_on(questions_per_day=0)
    response = client.post(path, headers=HEADERS, json={"question": "q", "model": "m8"})
    assert response.status_code == 503
    assert response.json() == {
        "detail": "the public site's daily limit of 0 questions is reached",
    }
    assert 1 <= int(response.headers["Retry-After"]) <= 86400
