# nameres_client.py — RENCI Name Resolution lookup.
# this is Stage 3 of the grounded pipeline: a free-text entity
# mention (extracted by the LLM in Stage 2) goes in, ranked candidate
# CURIEs come out. we always take the top-1 by RANK and ignore the
# returned BM25 score — Name Resolution scores are unbounded text
# match weights, not confidence, and they are not comparable across
# different query strings, so a numeric threshold would be meaningless.

from __future__ import annotations

# logging: stdlib. logger injected by runner so this client's calls
# show up in the same per-run log file as everything else.
import logging

# time.perf_counter: stdlib. high-resolution timer for per-call latency.
import time

# urllib.parse.urlencode: stdlib. NameRes /lookup is a GET with a
# percent-encoded query string. urlencode handles spaces and unicode
# without us hand-rolling escaping.
from urllib.parse import urlencode

# dataclasses: stdlib. NameResReply is a frozen dataclass that
# pipeline.py destructures.
from dataclasses import dataclass

# typing.Any: stdlib. NameRes returns a JSON array of records; we
# preserve them verbatim for the trace.
from typing import Any

# httpx: same client lib used for OpenRouter and PloverDB. one HTTP
# library across the codebase keeps timeouts and error types consistent.
import httpx

# Config: gives us the request timeout. NameRes itself is fast (usually
# <100ms) but we still cap to be defensive.
from .config import Config


@dataclass(frozen=True)
class NameResReply:
    mention: str                       # what we asked for
    candidates: list[dict[str, Any]]   # raw NameRes records, preserved
    top1_curie: str | None             # convenience: top-1 by rank, or None
    top1_label: str | None
    latency_s: float


class NameResError(RuntimeError):
    pass


class NameResClient:
    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        self._cfg = cfg
        self._log = logger
        # base URL comes from config.yaml so swapping to a staging
        # mirror is a one-line edit there, not a code change.
        self._base = cfg.endpoints.nameres
        # one httpx.Client per pipeline run — TCP pool reuse across the
        # 80 grounded calls (one per question per model).
        self._http = httpx.Client(timeout=cfg.generation.request_timeout_s)

    def close(self) -> None:
        self._http.close()

    def lookup(
        self,
        mention: str,
        *,
        limit: int = 5,
        biolink_types: list[str] | None = None,
    ) -> NameResReply:
        # we ask for limit=5 by default even though we only USE top-1.
        # the extra 4 records are cheap (a few hundred bytes) and let
        # us spot ambiguity post-hoc ("did the LLM pick the same id
        # NameRes top-1 picked? what was the gap to top-2?"). the
        # `biolink_types` filter is server-side; if the caller passes
        # a list, NameRes will only return matches whose Biolink class
        # is in that UNION. multiple types are sent as repeated
        # ?biolink_type=... query params (NameRes accepts that form
        # natively — confirmed against the SRI deployment).
        #
        # plural-list (rather than a single string) is intentional: the
        # pipeline derives a "loose neighborhood" via BMT (Biolink Model
        # Toolkit) so e.g. a Stage 2 pick of biolink:Pathway also
        # matches biolink:BiologicalProcess entities — the same concept
        # is typed differently across ontologies and a strict single
        # filter excludes correct answers (cholesterol-biosynthesis
        # bug, see biolink_helper.py for the full story).
        # urlencode with doseq=True turns {"biolink_type": [...]} into
        # repeated query params automatically; we use a list of tuples
        # so the rest of the params keep their natural string form.
        query_pairs: list[tuple[str, str]] = [
            ("string", mention),
            ("limit", str(limit)),
        ]
        for t in biolink_types or []:
            query_pairs.append(("biolink_type", t))
        url = f"{self._base}/lookup?{urlencode(query_pairs)}"

        self._log.info(
            f"[bold cyan]→ nameres[/]  GET /lookup  "
            f"mention=[white]{mention!r}[/]  limit={limit}"
            + (f"  biolink_types={biolink_types}" if biolink_types else "")
        )

        t0 = time.perf_counter()
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as e:
            raise NameResError(f"network error calling NameRes: {e}") from e
        dt = time.perf_counter() - t0

        if resp.status_code != 200:
            self._log.error(
                f"NameRes returned {resp.status_code}: {resp.text[:400]}"
            )
            raise NameResError(
                f"NameRes HTTP {resp.status_code} for mention={mention!r}"
            )

        # NameRes returns a top-level JSON array. each record is a dict
        # with at least: curie, label, types (Biolink categories),
        # synonyms, taxa, score. we preserve the whole array on disk
        # and lift only top-1 here.
        body = resp.json()
        if not isinstance(body, list):
            raise NameResError(
                f"unexpected NameRes response shape (not a list): {str(body)[:200]}"
            )
        candidates: list[dict[str, Any]] = body

        top1_curie = None
        top1_label = None
        if candidates:
            top1_curie = candidates[0].get("curie")
            top1_label = candidates[0].get("label")

        self._log.info(
            f"[bold green]✓ nameres[/]  candidates={len(candidates)}  "
            f"top1=[magenta]{top1_curie}[/]  "
            f"label=[white]{top1_label!r}[/]  latency={dt:.2f}s"
        )

        return NameResReply(
            mention=mention,
            candidates=candidates,
            top1_curie=top1_curie,
            top1_label=top1_label,
            latency_s=dt,
        )
