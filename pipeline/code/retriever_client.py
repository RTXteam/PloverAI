# retriever_client.py — POSTs TRAPI messages to Retriever, the NCATS
# Biomedical Data Translator's knowledge-graph service, restricted to
# the Tier 0 knowledge graph (config retriever.tiers), and returns the
# parsed response with timing and size info. Tier 0 is the graph ARAX
# reasons over, so the lookups, the entity probes and the meta
# knowledge graph here describe the same data ARAX answers from. this
# is the only place in the pipeline that knows the Retriever URL or
# the query/response shape at the HTTP level; above this layer we hand
# around plain dicts.

from __future__ import annotations

# json: stdlib. used to estimate request body size in logs without
# re-serialising what httpx will already serialise.
import json

# logging: stdlib. logger injected by the runner, same as in the
# OpenRouter client, so all API calls show up in the same per-run log.
import logging

# time.perf_counter: stdlib. high-resolution latency timer.
import time

# dataclasses: stdlib. RetrieverReply is a frozen dataclass.
from dataclasses import dataclass

# typing.Any: stdlib. TRAPI messages are nested dicts whose detailed
# shape lives in the TRAPI 1.5 spec, not in our code.
from typing import Any

# httpx: third-party. same client library we use for OpenRouter — using
# one HTTP library across the codebase keeps timeout handling and
# error types consistent.
import httpx

# Config: our config module. provides the Retriever base URL, the tiers
# to query and the timeouts.
from .config import Config

# edge_qualifiers / render_qualifiers: the one definition of how a
# TRAPI edge's qualifiers are read and written as text, shared with
# the Stage 11 evidence table so the probe and the table agree.
from .reduction import edge_qualifiers, render_qualifiers


@dataclass(frozen=True)
class RetrieverReply:
    body: dict[str, Any]
    status_code: int
    latency_s: float


class RetrieverError(RuntimeError):
    pass


# pause before the single retry on a transient Retriever failure.
RETRIEVER_RETRY_PAUSE_S = 3.0


class RetrieverClient:
    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        self._cfg = cfg
        self._log = logger
        self._base = cfg.endpoints.retriever
        # one httpx.Client reuses the TCP connection across questions
        # (and is shared by the parallel entity probes of Stage 4).
        self._http = httpx.Client(timeout=cfg.generation.request_timeout_s)

    def close(self) -> None:
        self._http.close()

    def _with_tiers(self, trapi_message: dict[str, Any]) -> dict[str, Any]:
        # Retriever reads the tiers to search from a top-level
        # "parameters" block; Tier 0 unless the caller already set one.
        if "parameters" in trapi_message:
            return trapi_message
        return {**trapi_message, "parameters": {"tiers": list(self._cfg.retriever.tiers)}}

    def fetch_meta_kg(self) -> dict[str, Any]:
        # GET /meta_knowledge_graph returns every valid
        # (subject_category, predicate, object_category) triple Retriever
        # serves. roughly 4 MB JSON — we fetch it once at server / runner
        # start-up, cache it, then filter to the (s_cat, o_cat) pair
        # Stage 8 cares about per query. this is what lets Stage 8 PICK a
        # predicate from a list instead of inventing one (the
        # predicate-hallucination fix).
        url = f"{self._base}/meta_knowledge_graph"
        self._log.info(f"[bold cyan]→ retriever[/]  GET {url}")
        t0 = time.perf_counter()
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as e:
            raise RetrieverError(f"network error fetching meta_KG: {e}") from e
        dt = time.perf_counter() - t0
        if resp.status_code != 200:
            raise RetrieverError(
                f"Retriever meta_KG HTTP {resp.status_code}: {resp.text[:200]}"
            )
        body: dict[str, Any] = resp.json()
        edges = body.get("edges") or []
        cats = body.get("nodes") or {}
        self._log.info(
            f"[bold green]✓ retriever[/]  meta_KG  "
            f"categories={len(cats)}  triples={len(edges)}  "
            f"resp_bytes={len(resp.content)}  latency={dt:.2f}s"
        )
        return body

    def query(self, trapi_message: dict[str, Any]) -> RetrieverReply:
        # `trapi_message` must be the FULL TRAPI message
        # (i.e. {"message": {"query_graph": ...}}). we leave shape
        # checking to reasoner-validator earlier in the pipeline; this
        # client just sends bytes and reports what came back.
        # path is appended here so config.yaml carries only the BASE URL.
        url = f"{self._base}/query"
        body_out = self._with_tiers(trapi_message)
        self._log.info(
            f"[bold cyan]→ retriever[/]  POST {url}  "
            f"tiers={body_out.get('parameters', {}).get('tiers')}  "
            f"req_bytes={len(json.dumps(body_out))}"
        )

        # one retry on transient failures (network error or 5xx). a short
        # fixed pause covers a gateway blip; anything persistent still
        # fails loudly on the second attempt.
        t0 = time.perf_counter()
        resp: httpx.Response | None = None
        last_err = ""
        for attempt in (1, 2):
            try:
                resp = self._http.post(url, json=body_out)
            except httpx.HTTPError as e:
                last_err = f"network error calling Retriever: {e}"
                resp = None
            else:
                if resp.status_code < 500:
                    break
                last_err = f"Retriever HTTP {resp.status_code}: {resp.text[:200]}"
            if attempt == 1:
                self._log.warning(
                    f"retriever  attempt 1 failed ({last_err[:120]}); "
                    f"retrying in {RETRIEVER_RETRY_PAUSE_S:.0f}s"
                )
                time.sleep(RETRIEVER_RETRY_PAUSE_S)
        dt = time.perf_counter() - t0
        if resp is None or resp.status_code >= 500:
            self._log.error(last_err)
            raise RetrieverError(last_err)

        if resp.status_code != 200:
            self._log.error(
                f"Retriever returned {resp.status_code}: {resp.text[:400]}"
            )
            raise RetrieverError(
                f"Retriever HTTP {resp.status_code}: {resp.text[:200]}"
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
            f"[bold green]✓ retriever[/]  results={n_results}  "
            f"nodes={n_nodes}  edges={n_edges}  "
            f"resp_bytes={len(resp.content)}  latency={dt:.2f}s"
        )

        return RetrieverReply(
            body=body,
            status_code=resp.status_code,
            latency_s=dt,
        )

    def probe_predicates(
        self,
        pinned_curie: str,
        pinned_cat: str,
        answer_cat: str,
    ) -> PredicateProbe:
        # CURIE-specific predicate-distribution probe. fires ONE TRAPI
        # query with the pinned CURIE on one side and the answer category
        # on the other, NO predicate filter — Retriever returns every Tier
        # 0 fact that connects this exact CURIE to any node of the answer
        # category. we then tally how many facts each predicate has and
        # which direction (pinned→answer or answer→pinned) dominates.
        #
        # this exists because the meta_KG only tells us which (s,p,o)
        # triples are SCHEMA-valid — it doesn't say which ones are
        # actually populated for a given pinned entity. without this
        # probe, Stage 8 has to guess from English semantics which
        # predicate the graph actually populates.
        #
        # cost: usually 1-3 s; a hub entity (pain, cancer) can return
        # tens of MB, so the probe has its own timeout
        # (retriever.probe_timeout_s) and a timeout is a non-fatal
        # "unknown", never "zero facts".
        msg = {
            "message": {
                "query_graph": {
                    "nodes": {
                        "n0": {"ids": [pinned_curie], "categories": [pinned_cat]},
                        "n1": {"categories": [answer_cat]},
                    },
                    "edges": {
                        # subject/object orientation here is largely
                        # cosmetic — with no predicate constrained the
                        # graph matches facts in both directions, so we
                        # just pick one and tally directions from the
                        # returned facts' actual subject/object.
                        "e0": {"subject": "n0", "object": "n1"},
                    },
                },
            },
        }
        url = f"{self._base}/query"
        self._log.info(
            f"[bold cyan]→ retriever[/]  PROBE  pinned={pinned_curie}  "
            f"answer_cat={answer_cat}"
        )
        t0 = time.perf_counter()
        try:
            resp = self._http.post(url, json=self._with_tiers(msg), timeout=self._cfg.retriever.probe_timeout_s)
        except httpx.HTTPError as e:
            # probe failures are non-fatal — Stage 8 falls back to the
            # schema-only predicate list when probe is None.
            self._log.warning(f"predicate probe network error or timeout: {e}")
            return PredicateProbe(
                pinned_curie=pinned_curie,
                pinned_cat=pinned_cat,
                answer_cat=answer_cat,
                total_edges=0,
                by_predicate={},
                qualified_by_predicate={},
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
                qualified_by_predicate={},
                latency_s=dt,
                error=f"http_{resp.status_code}",
            )

        body: dict[str, Any] = resp.json()
        edges = (
            body.get("message", {}).get("knowledge_graph", {}).get("edges") or {}
        )
        by_pred, qualified_by_pred = tally_probe_edges(edges, pinned_curie)

        self._log.info(
            f"[bold green]✓ retriever[/]  PROBE  n_edges={len(edges)}  "
            f"predicates={len(by_pred)}  qualified_predicates={len(qualified_by_pred)}  "
            f"resp_bytes={len(resp.content)}  "
            f"latency={dt:.2f}s"
        )

        return PredicateProbe(
            pinned_curie=pinned_curie,
            pinned_cat=pinned_cat,
            answer_cat=answer_cat,
            total_edges=len(edges),
            by_predicate=by_pred,
            qualified_by_predicate=qualified_by_pred,
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
    # "forward"  = facts where pinned_curie is the SUBJECT
    # "reverse"  = facts where pinned_curie is the OBJECT
    by_predicate: dict[str, dict[str, int]]
    # predicate -> rendered qualifier set -> edge count, for predicates
    # with at least one qualified edge. lets Stage 8 see that, say, 31
    # of 614 affects edges say "decreased activity" before it decides
    # whether to constrain the query on direction.
    qualified_by_predicate: dict[str, dict[str, int]]
    latency_s: float
    error: str | None


def tally_probe_edges(
    edges: dict[str, Any],
    pinned_curie: str,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    # tally per-predicate counts and direction (does the fact go
    # pinned→answer or answer→pinned, as actually stored?), and
    # per-predicate qualifier sets.
    by_pred: dict[str, dict[str, int]] = {}
    qualified_by_pred: dict[str, dict[str, int]] = {}
    for edge in edges.values():
        pred = edge.get("predicate")
        if not pred:
            continue
        # Retriever restates a fact about a DESCENDANT of the pinned
        # entity (a subtype of the disease) as a fact about the pinned
        # entity itself, backed by a support graph, and orients the
        # restatement like the query edge, not like the fact: probed
        # with the disease as subject, "drug treats subtype" comes back
        # as "disease treats drug" (rheumatoid arthritis, 2026-09-29:
        # 1599 such edges, all reversed). the descendant's own fact is
        # in the reply too, so the restatement is skipped rather than
        # counted twice and in the wrong direction.
        if _has_support_graphs(edge):
            continue
        stats = by_pred.setdefault(pred, {"count": 0, "forward": 0, "reverse": 0})
        stats["count"] += 1
        # "forward" = pinned is the SUBJECT of the stored edge
        # "reverse" = pinned is the OBJECT of the stored edge
        # the LLM should set subject/object on its query graph to
        # match the dominant direction; a schema-checking validator
        # might refuse the query otherwise.
        # facts whose endpoint is a DESCENDANT of pinned_curie (the
        # graph expands CURIE IDs to ontology descendants) match neither
        # side exactly: they stay in "count" but not in the direction
        # tally, so the direction stats stay clean.
        if edge.get("subject") == pinned_curie:
            stats["forward"] += 1
        elif edge.get("object") == pinned_curie:
            stats["reverse"] += 1
        qualifiers = edge_qualifiers(edge)
        if qualifiers:
            signatures = qualified_by_pred.setdefault(pred, {})
            signature = render_qualifiers(qualifiers)
            signatures[signature] = signatures.get(signature, 0) + 1
    return by_pred, qualified_by_pred


def _has_support_graphs(edge: dict[str, Any]) -> bool:
    return any(
        isinstance(a, dict) and a.get("attribute_type_id") == "biolink:support_graphs"
        for a in edge.get("attributes") or []
    )


# Retriever meta_KG → {(subject_cat, object_cat): [valid predicates]}.
# called once at start-up by both the FastAPI service and the CLI
# runner, so the UI and the benchmark constrain Stage 8 identically.
# the meta_KG body has ~10k category-triples; inverting it into a dict
# lookup makes the per-query "what predicates
# are valid for (Disease, PhenotypicFeature)?" question a single dict
# access. predicates are sorted alphabetically so the LLM sees a
# deterministic list (same prompt across calls = better caching).
def build_predicate_index(meta_kg: dict[str, Any]) -> dict[tuple[str, str], list[str]]:
    index: dict[tuple[str, str], set[str]] = {}
    for edge in meta_kg.get("edges") or []:
        s = edge.get("subject")
        o = edge.get("object")
        p = edge.get("predicate")
        if not s or not o or not p:
            continue
        index.setdefault((s, o), set()).add(p)
    return {k: sorted(v) for k, v in index.items()}


def build_category_set(meta_kg: dict[str, Any]) -> list[str]:
    # extract every Biolink category the graph actually carries — from
    # both the nodes block (one entry per indexed category) and the
    # edges block (subject/object categories that appear in at least
    # one supported predicate triple). returned sorted so the LLM
    # always sees the same order (prompt caching).
    cats: set[str] = set()
    for c in meta_kg.get("nodes") or {}:
        if isinstance(c, str) and c.startswith("biolink:"):
            cats.add(c)
    for edge in meta_kg.get("edges") or []:
        for k in ("subject", "object"):
            v = edge.get(k)
            if isinstance(v, str) and v.startswith("biolink:"):
                cats.add(v)
    return sorted(cats)


def openapi_summary(body: dict[str, Any]) -> dict[str, Any]:
    # the version facts a Translator service publishes in its OpenAPI
    # info block: its own version, the TRAPI version it speaks and the
    # Biolink version of its data (ARAX spells the key "biolink-version",
    # Retriever "biolink_version"). the rest of the ~150 KB document
    # says nothing about which data answered.
    info = body.get("info") or {}
    trapi = info.get("x-trapi") or {}
    translator = info.get("x-translator") or {}
    return {
        "title": info.get("title"),
        "version": info.get("version"),
        "trapi_version": trapi.get("version"),
        "biolink_version": translator.get("biolink-version") or translator.get("biolink_version"),
        "infores": translator.get("infores"),
    }
