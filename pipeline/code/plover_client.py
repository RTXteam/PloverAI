# plover_client.py — POSTs TRAPI messages to PloverDB and returns the
# parsed response with timing and size info. this is the only place in
# the pipeline that knows the PloverDB URL or the query/response shape
# at the HTTP level. above this layer we hand around plain dicts.

from __future__ import annotations

# json: stdlib. used to estimate request body size in logs without
# re-serialising what httpx will already serialise.
import json

# logging: stdlib. logger injected by the runner, same as in the
# OpenRouter client, so all API calls show up in the same per-run log.
import logging

# time.perf_counter: stdlib. high-resolution latency timer.
import time

# dataclasses: stdlib. PloverReply is a frozen dataclass.
from dataclasses import dataclass

# typing.Any: stdlib. TRAPI messages are nested dicts whose detailed
# shape lives in the TRAPI 1.5 spec, not in our code.
from typing import Any

# httpx: third-party. same client library we use for OpenRouter — using
# one HTTP library across the codebase keeps timeout handling and
# error types consistent.
import httpx

# Config: our config module. provides the PloverDB query URL and the
# request timeout (PloverDB can be slow on broad queries).
from .config import Config


@dataclass(frozen=True)
class PloverReply:
    body: dict[str, Any]
    status_code: int
    latency_s: float
    response_bytes: int


class PloverError(RuntimeError):
    pass


class PloverClient:
    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        self._cfg = cfg
        self._log = logger
        # one httpx.Client reuses the TCP connection across questions.
        # we don't auto-retry: blind retries hide real backend trouble,
        # and a TRAPI query that times out once is suspicious anyway.
        self._http = httpx.Client(timeout=cfg.generation.request_timeout_s)

    def close(self) -> None:
        self._http.close()

    def fetch_meta_kg(self) -> dict[str, Any]:
        # GET /meta_knowledge_graph returns every valid
        # (subject_category, predicate, object_category) triple this
        # PloverDB build supports. roughly 6 MB JSON for KG2.10.2c — we
        # fetch it once at server / runner start-up, cache it, then
        # filter to the (s_cat, o_cat) pair Stage 1 cares about per
        # query. this is what lets Stage 1 PICK a predicate from a list
        # instead of inventing one (the predicate-hallucination fix).
        # we don't enforce a status code here beyond what httpx does;
        # if PloverDB is up enough to serve /meta_knowledge_graph,
        # /query is up too, and vice versa.
        url = f"{self._cfg.endpoints.ploverdb}/meta_knowledge_graph"
        self._log.info(f"[bold cyan]→ ploverdb[/]  GET {url}")
        t0 = time.perf_counter()
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as e:
            raise PloverError(f"network error fetching meta_KG: {e}") from e
        dt = time.perf_counter() - t0
        if resp.status_code != 200:
            raise PloverError(
                f"PloverDB meta_KG HTTP {resp.status_code}: {resp.text[:200]}"
            )
        body: dict[str, Any] = resp.json()
        edges = body.get("edges") or []
        cats = body.get("nodes") or {}
        self._log.info(
            f"[bold green]✓ ploverdb[/]  meta_KG  "
            f"categories={len(cats)}  triples={len(edges)}  "
            f"resp_bytes={len(resp.content)}  latency={dt:.2f}s"
        )
        return body

    def query(self, trapi_message: dict[str, Any]) -> PloverReply:
        # `trapi_message` must be the FULL TRAPI message
        # (i.e. {"message": {"query_graph": ...}}). we leave shape
        # checking to reasoner-validator earlier in the pipeline; this
        # client just sends bytes and reports what came back.
        # path is appended here so config.yaml carries only the BASE URL.
        url = f"{self._cfg.endpoints.ploverdb}/query"
        self._log.info(
            f"[bold cyan]→ ploverdb[/]  POST {url}  "
            f"req_bytes={len(json.dumps(trapi_message))}"
        )

        t0 = time.perf_counter()
        try:
            resp = self._http.post(url, json=trapi_message)
        except httpx.HTTPError as e:
            raise PloverError(f"network error calling PloverDB: {e}") from e
        dt = time.perf_counter() - t0

        if resp.status_code != 200:
            self._log.error(
                f"PloverDB returned {resp.status_code}: {resp.text[:400]}"
            )
            raise PloverError(
                f"PloverDB HTTP {resp.status_code}: {resp.text[:200]}"
            )

        body: dict[str, Any] = resp.json()

        # results / nodes / edges counts are useful in logs and cheap to compute.
        # if the response shape deviates we log -1 rather than crash, since
        # the response is already on disk by the time validation runs later.
        n_nodes = n_edges = n_results = -1
        try:
            kg = body["message"].get("knowledge_graph") or {}
            n_nodes = len(kg.get("nodes") or {})
            n_edges = len(kg.get("edges") or {})
            n_results = len(body["message"].get("results") or [])
        except KeyError:
            pass

        self._log.info(
            f"[bold green]✓ ploverdb[/]  results={n_results}  "
            f"nodes={n_nodes}  edges={n_edges}  "
            f"resp_bytes={len(resp.content)}  latency={dt:.2f}s"
        )

        return PloverReply(
            body=body,
            status_code=resp.status_code,
            latency_s=dt,
            response_bytes=len(resp.content),
        )

    def probe_predicates(
        self,
        pinned_curie: str,
        pinned_cat: str,
        answer_cat: str,
    ) -> PredicateProbe:
        # CURIE-specific predicate-distribution probe. fires ONE TRAPI
        # query with the pinned CURIE on one side and the answer category
        # on the other, NO predicate filter — PloverDB returns every
        # edge in KG2c that connects this exact CURIE to any node of the
        # answer category, in either direction. we then tally how many
        # edges each predicate has and which direction (pinned→answer or
        # answer→pinned) dominates.
        #
        # this exists because the meta_KG only tells us which (s,p,o)
        # triples are SCHEMA-valid — it doesn't say which ones are
        # actually populated for a given pinned entity. KG2c is sparse:
        # a predicate that's valid for (GrossAnat, Cell) may have 1M
        # edges across the whole KG but 0 from the brain specifically.
        # without this probe, Stage 8 has to guess from English semantics
        # which predicate KG2c actually populates — and gets it wrong
        # (e.g. "cells in the brain" → biolink:located_in → 0 results
        # when KG2c stores the same fact as biolink:has_part).
        #
        # cost: one extra ~100-500ms PloverDB call per question. cheap
        # relative to the LLM calls upstream and the savings from never
        # firing a Stage 10 query that's pre-doomed to return 0.
        msg = {
            "message": {
                "query_graph": {
                    "nodes": {
                        "n0": {"ids": [pinned_curie], "categories": [pinned_cat]},
                        "n1": {"categories": [answer_cat]},
                    },
                    "edges": {
                        # subject/object orientation here is largely
                        # cosmetic — PloverDB matches edges in both
                        # directions when no predicate is constrained,
                        # so we just pick one and tally directions from
                        # the returned edges' actual subject/object.
                        "e0": {"subject": "n0", "object": "n1"},
                    },
                },
            },
        }
        url = f"{self._cfg.endpoints.ploverdb}/query"
        self._log.info(
            f"[bold cyan]→ ploverdb[/]  PROBE  pinned={pinned_curie}  "
            f"answer_cat={answer_cat}"
        )
        t0 = time.perf_counter()
        try:
            resp = self._http.post(url, json=msg)
        except httpx.HTTPError as e:
            # probe failures are non-fatal — Stage 8 falls back to the
            # schema-only predicate list when probe is None.
            self._log.warning(f"predicate probe network error: {e}")
            return PredicateProbe(
                pinned_curie=pinned_curie,
                pinned_cat=pinned_cat,
                answer_cat=answer_cat,
                total_edges=0,
                by_predicate={},
                latency_s=time.perf_counter() - t0,
                error=f"network: {e}",
            )

        dt = time.perf_counter() - t0
        if resp.status_code != 200:
            self._log.warning(
                f"predicate probe HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return PredicateProbe(
                pinned_curie=pinned_curie,
                pinned_cat=pinned_cat,
                answer_cat=answer_cat,
                total_edges=0,
                by_predicate={},
                latency_s=dt,
                error=f"http_{resp.status_code}",
            )

        body: dict[str, Any] = resp.json()
        edges = (
            body.get("message", {}).get("knowledge_graph", {}).get("edges") or {}
        )
        # tally per-predicate counts and direction (does the edge go
        # pinned→answer or answer→pinned, as actually stored in KG2c?).
        by_pred: dict[str, dict[str, int]] = {}
        for edge in edges.values():
            pred = edge.get("predicate")
            subj = edge.get("subject")
            obj = edge.get("object")
            if not pred:
                continue
            stats = by_pred.setdefault(pred, {"count": 0, "forward": 0, "reverse": 0})
            stats["count"] += 1
            # "forward" = pinned is the SUBJECT of the stored edge
            # "reverse" = pinned is the OBJECT of the stored edge
            # the LLM should set subject/object on its query graph to
            # match the dominant direction, otherwise PloverDB's edge
            # matcher will still find it (orientation-agnostic) but a
            # schema-rejecting validator might refuse the query.
            if subj == pinned_curie:
                stats["forward"] += 1
            elif obj == pinned_curie:
                stats["reverse"] += 1
            # edges whose endpoint is a DESCENDANT of pinned_curie (Plover
            # auto-expands CURIE IDs to ontology descendants) won't match
            # subj==pinned_curie or obj==pinned_curie exactly. we leave
            # them in "count" but skip the forward/reverse tally so the
            # direction stats stay clean.

        self._log.info(
            f"[bold green]✓ ploverdb[/]  PROBE  n_edges={len(edges)}  "
            f"predicates={len(by_pred)}  resp_bytes={len(resp.content)}  "
            f"latency={dt:.2f}s"
        )

        return PredicateProbe(
            pinned_curie=pinned_curie,
            pinned_cat=pinned_cat,
            answer_cat=answer_cat,
            total_edges=len(edges),
            by_predicate=by_pred,
            latency_s=dt,
            error=None,
        )


@dataclass(frozen=True)
class PredicateProbe:
    pinned_curie: str
    pinned_cat: str
    answer_cat: str
    total_edges: int
    # predicate -> {"count": int, "forward": int, "reverse": int}
    # "forward"  = edges where pinned_curie is the SUBJECT in KG2c
    # "reverse"  = edges where pinned_curie is the OBJECT  in KG2c
    by_predicate: dict[str, dict[str, int]]
    latency_s: float
    error: str | None
