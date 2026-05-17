# nodenorm_client.py — RENCI Node Normalization.
# used twice in the grounded pipeline:
#   - Stage 6: canonicalise the pinned CURIE NameRes returned, so
#                the LLM gets a stable id (and Biolink categories)
#                consistent with how KG2c was built.
#   - Stage 12: canonicalise every CURIE the LLM picked as an answer,
#                so scoring against gold anchors is canonical-vs-canonical
#                rather than a string-equality lottery (DRUGBANK:DB00331
#                vs CHEBI:6801 — same drug, different ids).

from __future__ import annotations

# logging: stdlib. injected logger so this client's calls show up in
# the per-run log alongside every other API call.
import logging

# time.perf_counter: stdlib. high-resolution timer for per-call latency.
import time

# dataclasses: stdlib. NodeNormReply is frozen so callers can't drift
# its fields after the call.
from dataclasses import dataclass

# typing.Any: stdlib. NodeNorm responses are nested dicts whose detailed
# shape lives in the NodeNormalization OpenAPI spec.
from typing import Any

# httpx: same HTTP client we use everywhere else.
import httpx

# Config: provides the request timeout. NodeNorm is fast (usually
# <200ms) but we still cap to be defensive.
from .config import Config


@dataclass(frozen=True)
class NodeNormReply:
    requested: list[str]                          # CURIEs we asked about
    raw: dict[str, Any]                           # full NodeNorm body, preserved
    canonical: dict[str, str | None]              # raw_curie -> canonical id, or None if unmapped
    categories: dict[str, list[str]]              # raw_curie -> Biolink categories of canonical
    labels: dict[str, str | None]                 # raw_curie -> canonical label
    information_content: dict[str, float | None]  # raw_curie -> IC (higher = more specific concept)
    equivalent_identifiers: dict[str, list[str]]  # raw_curie -> equivalent CURIEs across namespaces
                                                  # (MeSH, UMLS, NCBIGene, etc.) — required for
                                                  # cross-namespace matching against PubTator's
                                                  # MeSH-flavoured annotations.
    latency_s: float


class NodeNormError(RuntimeError):
    pass


class NodeNormClient:
    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        self._cfg = cfg
        self._log = logger
        # base URL comes from config.yaml so swapping to a staging
        # mirror is a one-line edit there, not a code change.
        self._base = cfg.endpoints.nodenorm
        self._http = httpx.Client(timeout=cfg.generation.request_timeout_s)

    def close(self) -> None:
        self._http.close()

    def normalize(
        self,
        curies: list[str],
        *,
        conflate: bool = True,
        drug_chemical_conflate: bool = True,
    ) -> NodeNormReply:
        # one POST handles a batch. we always pass conflate=True and
        # drug_chemical_conflate=True so equivalent gene/protein and
        # drug/chemical identifiers fold to one canonical id, which
        # matches how KG2c is built. callers can flip them off for
        # diagnostic runs but defaults are the right thing 99% of the
        # time.
        if not curies:
            # cheap early-out: no point round-tripping over an empty list.
            return NodeNormReply(
                requested=[],
                raw={},
                canonical={},
                categories={},
                labels={},
                information_content={},
                equivalent_identifiers={},
                latency_s=0.0,
            )

        url = f"{self._base}/get_normalized_nodes"
        payload: dict[str, Any] = {
            "curies": curies,
            "conflate": conflate,
            "drug_chemical_conflate": drug_chemical_conflate,
        }

        self._log.info(
            f"[bold cyan]→ nodenorm[/]  POST /get_normalized_nodes  "
            f"n_curies={len(curies)}  "
            f"first=[magenta]{curies[0]}[/]"
        )

        t0 = time.perf_counter()
        try:
            resp = self._http.post(url, json=payload)
        except httpx.HTTPError as e:
            raise NodeNormError(f"network error calling NodeNorm: {e}") from e
        dt = time.perf_counter() - t0

        if resp.status_code != 200:
            self._log.error(
                f"NodeNorm returned {resp.status_code}: {resp.text[:400]}"
            )
            raise NodeNormError(
                f"NodeNorm HTTP {resp.status_code} for {len(curies)} CURIEs"
            )

        body = resp.json()
        # NodeNorm shape (current):
        # {
        #   "<requested_curie>": {
        #     "id": {"identifier": "<canonical>", "label": "...", ...},
        #     "type": ["biolink:Drug", ...],
        #     "equivalent_identifiers": [{"identifier": "...", "label": "..."}, ...],
        #     "information_content": <float>
        #   } | None
        # }
        # an unresolvable CURIE comes back as null; we surface that as
        # canonical=None for that key so callers can detect "NodeNorm
        # didn't recognise this id".
        canonical: dict[str, str | None] = {}
        categories: dict[str, list[str]] = {}
        labels: dict[str, str | None] = {}
        equivalent_identifiers: dict[str, list[str]] = {}
        # information_content is the IC score NodeNorm computes from
        # the Babel equivalence-class size — higher = more specific
        # concept ("hypoglycemic seizures" > "seizure" > "phenotype").
        # we use it in Stage 5 to re-rank NameRes top-K candidates
        # when the question wants a broad concept (granularity=general)
        # vs. a specific one (granularity=specific).
        information_content: dict[str, float | None] = {}
        for raw_curie in curies:
            entry = body.get(raw_curie)
            if not entry:
                canonical[raw_curie] = None
                categories[raw_curie] = []
                labels[raw_curie] = None
                information_content[raw_curie] = None
                equivalent_identifiers[raw_curie] = []
                continue
            id_block = entry.get("id") or {}
            canonical[raw_curie] = id_block.get("identifier")
            labels[raw_curie] = id_block.get("label")
            categories[raw_curie] = list(entry.get("type") or [])
            ic_val = entry.get("information_content")
            information_content[raw_curie] = float(ic_val) if ic_val is not None else None
            # equivalent_identifiers is a list of {identifier, label, ...}
            # dicts in NodeNorm's response. we only need the identifiers
            # for cross-namespace matching, so flatten to a list of CURIE strings.
            eq_block = entry.get("equivalent_identifiers") or []
            equivalent_identifiers[raw_curie] = [
                e["identifier"] for e in eq_block
                if isinstance(e, dict) and isinstance(e.get("identifier"), str)
            ]

        n_resolved = sum(1 for v in canonical.values() if v is not None)
        self._log.info(
            f"[bold green]✓ nodenorm[/]  resolved={n_resolved}/{len(curies)}  "
            f"latency={dt:.2f}s"
        )

        return NodeNormReply(
            requested=list(curies),
            raw=body,
            canonical=canonical,
            categories=categories,
            labels=labels,
            information_content=information_content,
            equivalent_identifiers=equivalent_identifiers,
            latency_s=dt,
        )
