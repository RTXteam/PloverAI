# arax_client.py — POSTs TRAPI messages to ARAX, the RTX team's
# reasoner, at Stage 10. the same shape as retriever_client:
# one class that knows the URL and the HTTP details, above which the
# pipeline hands around plain dicts. ARAX differs from a lookup in
# three ways that matter here:
#   - it reasons, so a reply can take minutes (a wall-clock budget of
#     its own, config arax.timeout_s);
#   - it reports a failure it understood as HTTP 200 with a status and
#     description in the body;
#   - its plain request/response mode stopped delivering large replies
#     on 2026-09-29: ARAX logged an inferred query "completed in 32 s"
#     while curl, httpx and urllib received not one byte in 17 minutes.
#     so every query asks for ARAX's progress stream (stream_progress):
#     one JSON object per line, ARAX's log entries while it works, then
#     the TRAPI response itself. the connection never sits idle, and the
#     log entries are handed to on_progress, which the UI shows live.

from __future__ import annotations

# json: stdlib. request size for the log line, and the progress stream.
import json

# re: stdlib. matches ARAX's progress lines to plain words.
import re

# logging: stdlib. the runner's / API's logger, so ARAX calls sit in the
# same run.log as every other external call.
import logging

# time: stdlib. latency and the pause before the single retry.
import time

# dataclasses: stdlib. AraxReply is built once and only read.
from dataclasses import dataclass
from typing import Any

# Callable: the progress callback type.
from collections.abc import Callable

# httpx: third-party, the HTTP client used by every service wrapper.
import httpx

# Config: base URL and the ARAX timeout.
from .config import Config


@dataclass(frozen=True)
class AraxReply:
    body: dict[str, Any]
    status_code: int
    latency_s: float


class AraxError(RuntimeError):
    pass


# pause before the single retry on a network error or HTTP 5xx.
ARAX_RETRY_PAUSE_S = 5.0


class AraxClient:
    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        self._cfg = cfg
        self._log = logger
        # per-read timeout: ARAX writes a progress line every few seconds
        # while it works, so two minutes of silence means trouble. the
        # whole query has its own wall-clock budget (arax.timeout_s).
        self._http = httpx.Client(timeout=httpx.Timeout(120.0, connect=30.0))

    def close(self) -> None:
        self._http.close()

    def query(
        self,
        trapi_message: dict[str, Any],
        on_progress: Callable[[str], None] | None = None,
    ) -> AraxReply:
        url = f"{self._cfg.endpoints.arax}/query"
        request = {**trapi_message, "stream_progress": True}
        budget = self._cfg.arax.timeout_s
        self._log.info(
            f"[bold cyan]→ arax[/]  POST {url}  "
            f"req_bytes={len(json.dumps(request))}  budget={budget}s  (progress stream)"
        )
        t0 = time.perf_counter()
        last_err = ""
        for attempt in (1, 2):
            try:
                body, n_bytes = self._stream(url, request, t0 + budget, on_progress)
            except _Retryable as e:
                last_err = str(e)
                if attempt == 1:
                    self._log.warning(
                        f"arax  attempt 1 failed ({last_err[:120]}); "
                        f"retrying in {ARAX_RETRY_PAUSE_S:.0f}s"
                    )
                    time.sleep(ARAX_RETRY_PAUSE_S)
                continue
            dt = time.perf_counter() - t0
            message = body.get("message") or {}
            knowledge_graph = message.get("knowledge_graph") or {}
            n_results = len(message.get("results") or [])
            # ARAX's own verdict on the query ("OK", "QueryGraphError", ...),
            # logged because a 200 with zero results can be either "nothing
            # found" or "ARAX could not run this query shape".
            self._log.info(
                f"[bold green]✓ arax[/]  status={body.get('status')!r}  results={n_results}  "
                f"nodes={len(knowledge_graph.get('nodes') or {})}  "
                f"edges={len(knowledge_graph.get('edges') or {})}  "
                f"aux_graphs={len(message.get('auxiliary_graphs') or {})}  "
                f"resp_bytes={n_bytes}  latency={dt:.2f}s  "
                f"description={str(body.get('description') or '')[:120]!r}"
            )
            return AraxReply(body=body, status_code=200, latency_s=dt)
        self._log.error(last_err)
        raise AraxError(last_err)

    def _stream(
        self,
        url: str,
        request: dict[str, Any],
        deadline: float,
        on_progress: Callable[[str], None] | None,
    ) -> tuple[dict[str, Any], int]:
        # reads the progress stream to its final line, the TRAPI response.
        # network trouble or a 5xx before the response is _Retryable; an
        # ARAX-reported error, a stream that ends without a response, or
        # a blown budget is final.
        final: dict[str, Any] | None = None
        n_bytes = 0
        try:
            with self._http.stream("POST", url, json=request) as resp:
                if resp.status_code >= 500:
                    raise _Retryable(f"ARAX HTTP {resp.status_code}: {resp.read()[:200]!r}")
                if resp.status_code != 200:
                    text = resp.read().decode("utf-8", "replace")
                    self._log.error(f"ARAX returned {resp.status_code}: {text[:400]}")
                    raise AraxError(f"ARAX HTTP {resp.status_code}: {text[:200]}")
                for line in resp.iter_lines():
                    if time.perf_counter() > deadline:
                        raise AraxError(
                            f"ARAX did not answer within {self._cfg.arax.timeout_s} s"
                        )
                    n_bytes += len(line)
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if isinstance(entry.get("message"), dict):
                        final = entry
                    elif on_progress is not None and entry.get("level") == "INFO":
                        on_progress(str(entry.get("message") or ""))
        except httpx.HTTPError as e:
            raise _Retryable(f"network error calling ARAX: {e}") from e
        if final is None:
            raise AraxError("ARAX's progress stream ended without a response")
        return final, n_bytes


class _Retryable(Exception):
    pass


# ARAX's progress log, said the way the live view says it: the steps a
# reader cares about in plain words, bookkeeping lines dropped (None).
# patterns are tried in order; the first match wins.
_PROGRESS_WORDS: list[tuple[re.Pattern[str], str | None]] = [
    (re.compile(r"^Calling XDTD"), "Predicting treatments with ARAX's drug-repurposing model (xDTD)"),
    (re.compile(r"^Launching ARAX inferer"), "Starting inference"),
    (re.compile(r"^Resultify created (\d+) results"), "Assembled {0} candidate answers"),
    (re.compile(r"^Expanding qedge \S+ using infores:retriever"), "Looking facts up in the Tier 0 graph through Retriever"),
    (re.compile(r"^Expanding qedge"), "Expanding the query graph"),
    (re.compile(r"^After Expand, the KG has (\d+) nodes and (\d+) edges"), "Gathered {0} entities and {1} facts"),
    (re.compile(r"^Computing the normalized Google distance"), "Scoring how often answers and question co-occur in PubMed"),
    (re.compile(r"remove_general_concept_nodes"), "Removing overly general concepts"),
    (re.compile(r"reranker", re.IGNORECASE), "Ranking the answers"),
    (re.compile(r"^Processing is complete and resulted in (\d+) results"), "Done: {0} ranked answers"),
    (re.compile(r"^ARAX Query launching"), "Query received"),
]


def progress_words(message: str) -> str | None:
    for pattern, words in _PROGRESS_WORDS:
        match = pattern.search(message)
        if match:
            return words.format(*match.groups()) if words else None
    return None
