# runner.service_versions: every run records which build of each
# external service it talked to. ARAX (1.6.2, Biolink 4.2.5), Retriever
# (Biolink 4.3.2) and the identifier services (Babel 2026jul22, Biolink
# 4.4.3 on 2026-09-29) are versioned independently, so a result is only
# reproducible if the run says which versions produced it.
#
# spec under test:
#   - one entry per service (arax and retriever /openapi.json, reduced to
#     their version facts; nodenorm and nameres /status) holding the URL
#     plus the service's own reply;
#   - bulky operational keys (databases, recent_queries, solr) dropped;
#   - a service that fails to answer is recorded as {"url", "error"}
#     and never raises: a missing version must not abort a benchmark.

from __future__ import annotations

from typing import Any

from pipeline.code.config import Endpoints
from pipeline.code.runner import service_versions

ENDPOINTS = Endpoints(
    arax="https://arax.example", retriever="https://retriever.example",
    openrouter="https://or.example", nameres="https://nameres.example",
    nodenorm="https://nodenorm.example", pubtator="https://pubtator.example",
)


def test_service_versions_records_each_service() -> None:
    replies: dict[str, Any] = {
        "https://arax.example/openapi.json": {
            "openapi": "3.0.1",
            "info": {
                "title": "ARAX", "version": "1.6.2",
                "x-trapi": {"version": "1.6.0", "operations": ["lookup"]},
                "x-translator": {"infores": "infores:arax", "biolink-version": "4.2.5"},
            },
            "paths": {"/query": {}},
        },
        "https://retriever.example/openapi.json": {
            "openapi": "3.0.3",
            "info": {
                "title": "Retriever", "version": "0.0.1",
                "x-trapi": {"version": "1.6.0"},
                "x-translator": {"infores": "infores:retriever", "biolink_version": "4.3.2"},
            },
        },
        "https://nodenorm.example/status": {
            "babel_version": "2026jul22", "databases": {"big": 1},
        },
    }

    def get_json(url: str) -> dict[str, Any]:
        if url not in replies:
            raise ConnectionError("unreachable")
        body: dict[str, Any] = replies[url]
        return body

    versions = service_versions(get_json, ENDPOINTS)
    assert versions == {
        "arax": {"url": "https://arax.example/openapi.json", "title": "ARAX", "version": "1.6.2",
                 "trapi_version": "1.6.0", "biolink_version": "4.2.5", "infores": "infores:arax"},
        "retriever": {"url": "https://retriever.example/openapi.json", "title": "Retriever",
                      "version": "0.0.1", "trapi_version": "1.6.0", "biolink_version": "4.3.2",
                      "infores": "infores:retriever"},
        "nodenorm": {"url": "https://nodenorm.example/status", "babel_version": "2026jul22"},
        "nameres": {"url": "https://nameres.example/status",
                    "error": "ConnectionError: unreachable"},
    }
