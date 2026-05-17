# pubtator_client.py — NLM PubTator3 entity-annotation lookup.
#
# used at Stage 13 in the grounded pipeline: for each edge in the
# answer_graph_view, the supporting_publications list is a set of
# PMIDs cited by RTX-KG2c as evidence. PubTator independently re-
# annotates each abstract with biomedical entities, so we can check
# whether the cited PMIDs actually mention BOTH endpoints of the
# edge — converting "the KG says this is supported" into "this is
# supported AND independently verifiable by a different NER pipeline".
#
# the pure-parsing function _parse_pubtator_biocjson is exposed at
# module level and unit-tested in tests/test_pubtator_parser.py.
# the HTTP wrapper is mechanical and tested via the same pattern as
# nameres_client / nodenorm_client.

from __future__ import annotations

import logging
import time

from dataclasses import dataclass

from typing import Any

import httpx

from .config import Config


@dataclass(frozen=True)
class PubTatorReply:
    requested: list[str]                       # PMIDs we asked about (no PMID: prefix)
    annotations: dict[str, set[str]]           # PMID:<n> -> set of identifier CURIEs
    missing_pmids: list[str]                   # PMIDs not present in PubTator's response
    raw: dict[str, Any]                        # full response body, preserved
    latency_s: float


class PubTatorError(RuntimeError):
    pass


def _parse_pubtator_biocjson(body: dict[str, Any]) -> dict[str, set[str]]:
    # pure function — extracts {PMID:<n> -> set of CURIE identifiers}
    # from PubTator3's BioC-JSON response. defensive against:
    #   - missing "PubTator3" top-level key (returns empty)
    #   - passages with no "annotations" key
    #   - annotations with missing or non-string "infons.identifier"
    # spec is in tests/test_pubtator_parser.py.
    out: dict[str, set[str]] = {}
    docs = body.get("PubTator3") or []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        pmid_raw = doc.get("pmid")
        if pmid_raw is None:
            continue
        pmid_key = f"PMID:{pmid_raw}"
        curie_set: set[str] = set()
        for passage in doc.get("passages") or []:
            if not isinstance(passage, dict):
                continue
            for ann in passage.get("annotations") or []:
                if not isinstance(ann, dict):
                    continue
                identifier = (ann.get("infons") or {}).get("identifier")
                if isinstance(identifier, str) and identifier:
                    curie_set.add(identifier)
        # always emit the PMID even with empty set so callers can
        # distinguish "indexed but no entities" from "not indexed".
        out[pmid_key] = curie_set
    return out


class PubTatorClient:
    # explicit per-call timeout for PubTator. it's a third-party rate-
    # limited NCBI service whose latency varies wildly with load. we
    # cap at 15s so a slow PubTator can't add 60+ seconds to the user's
    # query wait — if it times out, the pipeline degrades to
    # pubtator_verified=null and the answer still ships.
    _PUBTATOR_TIMEOUT_S = 15.0

    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        self._cfg = cfg
        self._log = logger
        self._base = cfg.endpoints.pubtator
        # PubTator3 is rate-limited (~3 req/sec/IP); use the dedicated
        # short timeout, not the generation.request_timeout_s which is
        # tuned for slow LLM completions.
        self._http = httpx.Client(timeout=self._PUBTATOR_TIMEOUT_S)

    def close(self) -> None:
        self._http.close()

    def fetch_annotations(self, pmid_curies: list[str]) -> PubTatorReply:
        # `pmid_curies` may be either "PMID:33487311" or bare "33487311".
        # we strip the prefix for the API call but key results by the
        # canonical "PMID:<n>" form so edge.supporting_publications
        # entries compare directly.
        if not pmid_curies:
            return PubTatorReply(
                requested=[],
                annotations={},
                missing_pmids=[],
                raw={},
                latency_s=0.0,
            )
        bare_pmids: list[str] = []
        for p in pmid_curies:
            s = str(p).strip()
            if s.startswith("PMID:"):
                s = s[len("PMID:"):]
            if s.isdigit():
                bare_pmids.append(s)
        # de-dupe in case multiple edges cited the same PMID — costs
        # nothing to ask for fewer PMIDs and preserves the rate budget.
        bare_pmids = list(dict.fromkeys(bare_pmids))
        if not bare_pmids:
            return PubTatorReply(
                requested=[],
                annotations={},
                missing_pmids=list(pmid_curies),
                raw={},
                latency_s=0.0,
            )

        url = f"{self._base}/publications/export/biocjson"
        params = {"pmids": ",".join(bare_pmids)}

        self._log.info(
            f"[bold cyan]→ pubtator[/]  GET /publications/export/biocjson  "
            f"n_pmids={len(bare_pmids)}  first=PMID:{bare_pmids[0]}"
        )

        t0 = time.perf_counter()
        try:
            resp = self._http.get(url, params=params)
        except httpx.HTTPError as e:
            raise PubTatorError(f"network error calling PubTator: {e}") from e
        dt = time.perf_counter() - t0

        if resp.status_code != 200:
            self._log.error(
                f"PubTator returned {resp.status_code}: {resp.text[:400]}"
            )
            raise PubTatorError(
                f"PubTator HTTP {resp.status_code} for {len(bare_pmids)} PMIDs"
            )

        body = resp.json()
        annotations = _parse_pubtator_biocjson(body)
        requested_set = {f"PMID:{p}" for p in bare_pmids}
        missing = sorted(requested_set - set(annotations.keys()))

        self._log.info(
            f"[bold green]✓ pubtator[/]  annotated={len(annotations)}/{len(bare_pmids)}  "
            f"missing={len(missing)}  latency={dt:.2f}s"
        )

        return PubTatorReply(
            requested=bare_pmids,
            annotations=annotations,
            missing_pmids=missing,
            raw=body,
            latency_s=dt,
        )
