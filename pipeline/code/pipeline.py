# pipeline.py — per-question orchestrator. the grounded flow:
#
#   [2]   LLM extracts the focal entity mention from the NL
#   [3]   NameRes /lookup → top-1 CURIE by rank
#   [6]   NodeNorm /get_normalized_nodes → canonical CURIE + Biolink categories
#   [8]   LLM builds TRAPI query graph (NL + canonical pinned)
#   [9]   reasoner-validator on the query graph
#   [10]  POST to ARAX /query (the default: multi-hop reasoning over the
#         Translator Tier 0 graph), or to Retriever /query for the
#         benchmark's one-hop "lookup" condition over the same graph
#   [11-] ARAX: ranked answers with their reasoning paths; lookup: rank
#         every fact by evidence strength, keep the top K, render as a
#         table (reduction.py)
#   [11]  LLM picks answer CURIEs
#   [12]  NodeNorm canonicalises every answer CURIE
#   [13]  ARAX: the reasoning graph behind the picked answers, facts
#         numbered F1..Fn with their evidence; lookup: answer graph view
#   [14]  lookup condition only: PubTator checks that cited PMIDs
#         mention both entities
#   [15]  LLM writes NL explanation citing the facts
#
# this file is intentionally linear: at each stage we either succeed
# and continue or we fail with a named status that the benchmark
# metrics count separately. it knows nothing about the CLI, progress
# bars, or run-level folder layout — that is runner.py's job.

from __future__ import annotations

# json: stdlib. parses LLM JSON in stages 1 and 4; serialises the
# stage-11 answer object into the stage-15 user message. the Stage 10
# response itself never goes into a prompt as JSON — see reduction.py
# and arax_reasoning.py.
import json

# logging: stdlib. logger injected so every stage logs through the
# same per-run logger as the runner and the API clients.
import logging

# re: stdlib. used by _extract_json to peel off ```json fences when
# the LLM wraps its output despite the system prompt asking it not to.
import re

# time.perf_counter: stdlib. measures elapsed seconds for meta.json.
import time

# dataclasses: stdlib. QuestionResult is a frozen dataclass returned
# to runner.py for the summary table.
from dataclasses import dataclass

# ThreadPoolExecutor: stdlib. runs the Stage 4 Tier 0 entity probes in
# parallel, so the slowest probe, not their sum, sets the wait.
from concurrent.futures import ThreadPoolExecutor

# difflib.SequenceMatcher: stdlib. used by Stage 7 to compare the
# user's entity mention against the resolved canonical label. cheap
# Levenshtein-style ratio (0..1) — flags cases where NameRes+candidate-
# pick still grounded to a label that's nothing like what the user typed.
from difflib import SequenceMatcher

# typing.Any: stdlib. gold question records and TRAPI messages are
# nested dicts whose shape lives in their own spec/JSON files.
from typing import Any

# Callable: the ARAX progress relay's type.
from collections.abc import Callable

# Config / ModelSpec come from our config module.
from .config import Config, ModelSpec

# in_context: Stage 4's pool threads log as part of their question, so
# the API's live stream of that question keeps their lines.
from .logging_setup import in_context

# OpenRouter and Retriever clients are constructed once per run by the
# runner and passed in here.
from .openrouter_client import OpenRouterClient, OpenRouterError
from .retriever_client import RetrieverClient, RetrieverError, RetrieverReply

# ARAX mode: the reasoner client (Stage 10) and the reader that turns
# its reply into ranked answers, reasoning paths and the UI graph
# (Stages 11, 13, 15).
from .arax_client import AraxClient, AraxError, AraxReply, progress_words
from .arax_reasoning import (
    answers_from_response,
    ask_arax_to_reason,
    evidence_edges,
    number_facts,
    reasoning_graph,
    render_answers_table,
    render_facts_block,
)

# RENCI clients — Stage 3 / Stage 6 / Stage 12.
from .nameres_client import NameResClient, NameResError
# BMT-derived loose-neighborhood expansion for the Stage 3 biolink_type
# filter. see biolink_helper.py for the full story; tl;dr: pick a
# category and we get back the set of biolink:CategoryName strings
# NameRes should ALL accept (e.g. Pathway → {Pathway, BiologicalProcess,
# PhysiologicalProcess, ...}) so we don't filter out correct answers
# that happen to be typed under the parent in the graph.
from .biolink_helper import loose_filter_for
from .nodenorm_client import NodeNormClient, NodeNormError

# reduction: the Stage 11 pre-step of the lookup condition. ranks every
# fact in the Retriever response by evidence strength and flattens the top-K to a table, so
# Stages 11 and 15 read a ranked subset instead of an arbitrary prefix
# of the raw TRAPI JSON. pure functions, unit-tested in
# tests/test_reduction.py.
from .reduction import reduce_response, render_table, to_json

# pubtator: Stage 13 enrichment. independently re-verifies each edge's
# supporting PMIDs by checking whether PubTator's NER co-mentions BOTH
# endpoints. graceful degradation: if the client is None or errors,
# edges get pubtator_verified=None and the pipeline carries on.
from .pubtator_client import PubTatorClient, PubTatorError

# prompts: SYS_ENTITY_EXTRACT, SYS_TRAPI_BUILD, SYS_ANSWER_PICK,
# SYS_EXPLAIN. flat strings re-exported by name so diffs for tuning
# are small and obvious.
from . import prompts

# trace: the on-disk artifact layout. CostLedger accumulates per-stage
# cost; QuestionPaths owns the file paths inside one question's folder.
from .trace import CostLedger, QuestionPaths, write_json, write_text

# trapi_validator: thin wrapper over reasoner-validator. policy:
# invalid query -> stop, no repair loop.
from .trapi_validator import validate_query


# ---------- terminal statuses (written into meta.json) ----------
# `status` describes the RUNTIME of the pipeline: did each stage
# complete without an error? these strings end up in meta.json and are
# what the scorer groups by when computing valid_trapi_rate,
# executed_rate, etc. keep them stable across runs.
STATUS_OK = "ok"
STATUS_INVALID_QUERY = "invalid_query"     # validator rejected the LLM's TRAPI
STATUS_LLM_BAD_JSON = "llm_bad_json"       # LLM returned non-JSON in a JSON-only stage
STATUS_LLM_TRUNCATED = "llm_truncated"     # LLM reply hit max_tokens mid-output (finish_reason=length)
STATUS_CRASHED = "crashed"                 # unhandled exception escaped run_grounded; recorded by the runner
STATUS_LOOKUP_ERROR = "lookup_error"       # Retriever (lookup condition) returned non-200 / network error
STATUS_LLM_ERROR = "llm_error"             # OpenRouter returned non-200 / network error
STATUS_NAMERES_FAILED = "nameres_failed"   # NameRes returned no candidates for the mention
STATUS_NODENORM_FAILED = "nodenorm_failed" # NodeNorm could not canonicalise the pinned CURIE
STATUS_ENTITY_EMPTY = "entity_empty"       # LLM produced an empty entity mention in Stage 2
STATUS_NO_CANDIDATE_MATCH = "no_candidate_match"  # Stage 4 LLM declared none of the NameRes
                                                  # candidates fits the user's intent (typo
                                                  # survived Stage 2, or all candidates wrong)
STATUS_LOW_CONFIDENCE_RESOLUTION = "low_confidence_resolution"  # Stage 7: resolved label
                                                  # has low textual similarity to the user
                                                  # mention; pipeline refuses to query KG
                                                  # against a probably-wrong entity
# Stage 1 (scope check) decided the input is outside PloverAI's
# biomedical-KG scope (politics, weather, chit-chat, ...). this is a
# deliberate refusal, not a failure — pipeline exits cleanly with a
# brief user-facing markdown explanation and no downstream cost.
STATUS_OUT_OF_SCOPE = "out_of_scope"
# Stage 8 returned the {"error": ...} reply its prompt allows when no
# predicate fits the question. a deliberate decline like out_of_scope:
# nothing is sent to ARAX or Retriever.
STATUS_QUERY_DECLINED = "query_declined"
# ARAX returned non-200 / network error (the lookup condition's twin of
# this is STATUS_LOOKUP_ERROR).
STATUS_ARAX_ERROR = "arax_error"


# ---------- outcomes (written into meta.json alongside status) ----------
# `outcome` describes the SEMANTIC result of a successful run:
# even when status=ok, the model may not have actually answered.
# this lets us count "the pipeline ran but produced no useful answer"
# separately from "the pipeline crashed."
OUTCOME_ANSWERED = "answered"                 # LLM picked at least one answer entity
OUTCOME_NO_RESULTS = "no_results"             # ARAX / Retriever returned zero results
OUTCOME_NO_ANSWER_PICKED = "no_answer_picked" # results came back but the LLM picked answers=[]
OUTCOME_QUERY_DECLINED = "query_declined"     # Stage 8 declined to build a query
# (when status != "ok" we leave outcome=None — no evaluation possible)


@dataclass(frozen=True)
class QuestionResult:
    q_id: str
    status: str
    cost_total_usd: float
    cost_total_tokens: tuple[int, int]
    elapsed_s: float
    error: str | None = None
    outcome: str | None = None         # answered | no_results | no_answer_picked | None
    outcome_reason: str | None = None  # short human-readable explanation
    n_results: int = -1         # -1 = stage didn't run; 0+ = actual count
    answers_n_picked: int = -1         # -1 = stage didn't run; 0+ = actual count


def _enrich_edges_with_pubtator(
    edges: list[dict[str, Any]],
    equivalent_curies: dict[str, list[str]],
    pubtator_annotations: dict[str, set[str]],
) -> list[dict[str, Any]]:
    # Stage 13 enrichment: for each edge, decide per supporting-PMID
    # whether PubTator's independent NER co-mentions BOTH endpoints
    # (or any of their equivalent CURIEs across MeSH/UMLS/etc.).
    # pure function, no IO. spec: tests/test_enrich_edges_pubtator.py.
    out_edges: list[dict[str, Any]] = []
    for e in edges:
        # copy to avoid mutating caller's edge dicts
        new_e = dict(e)
        pmids = e.get("supporting_publications") or []
        if not pmids:
            new_e["pubtator_verified"] = None
            out_edges.append(new_e)
            continue
        # collect ALL CURIEs (canonical + equivalents) for each endpoint.
        # if equivalents are missing, fall back to canonical alone.
        subj = e.get("source")
        obj = e.get("target")
        subj_curies: set[str] = set()
        obj_curies: set[str] = set()
        if subj:
            subj_curies.add(subj)
            subj_curies.update(equivalent_curies.get(subj, []))
        if obj:
            obj_curies.add(obj)
            obj_curies.update(equivalent_curies.get(obj, []))

        co_mention: list[str] = []
        subject_only: list[str] = []
        object_only: list[str] = []
        missing: list[str] = []
        for pmid in pmids:
            if pmid not in pubtator_annotations:
                missing.append(pmid)
                continue
            pmid_set = pubtator_annotations[pmid]
            has_subj = bool(pmid_set & subj_curies)
            has_obj = bool(pmid_set & obj_curies)
            if has_subj and has_obj:
                co_mention.append(pmid)
            elif has_subj:
                subject_only.append(pmid)
            elif has_obj:
                object_only.append(pmid)
            # else: neither endpoint mentioned — silent, the PMID is
            # in PubTator's index but contains neither entity. counted
            # in the total but not in any sub-list.

        non_missing = len(pmids) - len(missing)
        rate = (len(co_mention) / non_missing) if non_missing else 0.0
        new_e["pubtator_verified"] = {
            "co_mention_pmids": co_mention,
            "subject_only_pmids": subject_only,
            "object_only_pmids": object_only,
            "missing_pmids": missing,
            "co_mention_rate": rate,
            "verified": len(co_mention) >= 1,
        }
        out_edges.append(new_e)
    return out_edges


def _pubtator_verified_edge_rate(
    answer_graph_view: dict[str, Any],
) -> dict[str, Any]:
    # eval-level summary metric: of the edges that HAD PMIDs to verify,
    # what fraction did PubTator independently confirm via co-mention?
    # spec: tests/test_pubtator_verified_rate.py.
    edges = answer_graph_view.get("edges") or []
    verified = 0
    unverified = 0
    not_applicable = 0
    for e in edges:
        pv = e.get("pubtator_verified")
        if pv is None:
            not_applicable += 1
        elif isinstance(pv, dict) and pv.get("verified"):
            verified += 1
        else:
            unverified += 1
    denom = verified + unverified
    rate = (verified / denom) if denom > 0 else None
    return {
        "verified": verified,
        "unverified": unverified,
        "not_applicable": not_applicable,
        "total_edges": len(edges),
        "rate": rate,
    }


# TRAPI attribute type IDs that carry the provenance fields we surface
# in the answer_graph_view. defined once at module level so tests and
# the Stage 13 builder reference the same strings.
_ATTR_KNOWLEDGE_LEVEL = "biolink:knowledge_level"
_ATTR_PRIMARY_KS = "biolink:primary_knowledge_source"
_ATTR_PUBLICATIONS = "biolink:publications"
_ATTR_SUPPORTING_TEXT = "biolink:supporting_text"


def _build_answer_graph_view(
    *,
    pinned_curie: str,
    pinned_label: str | None,
    pinned_category: str | None,
    picked_answer_curies: list[str],
    kg_response: dict[str, Any],
) -> dict[str, Any]:
    # Stage 13 (lookup condition): reshape (pinned entity + picked
    # answers + the Retriever knowledge graph)
    # into a node-link graph view with full provenance per edge. pure
    # function, no IO. consumed by the frontend to render a research-
    # grade graph card with hover-able evidence on each edge.
    #
    # contract (see test_answer_graph_view.py for the strict spec):
    #   - never drop a picked answer, even if it's missing from the KG
    #     (label/category fall back to None)
    #   - keep only edges that touch the pinned node AND a picked answer
    #     (edges between two non-relevant nodes are noise — drop them)
    #   - preserve TRAPI subject/object orientation verbatim (don't flip)
    #   - degrade gracefully on missing/empty attributes blocks
    nodes_block: dict[str, Any] = (
        kg_response.get("message", {})
                   .get("knowledge_graph", {})
                   .get("nodes", {})
        or {}
    )
    edges_block: dict[str, Any] = (
        kg_response.get("message", {})
                   .get("knowledge_graph", {})
                   .get("edges", {})
        or {}
    )

    pinned_node = {
        "curie": pinned_curie,
        "label": pinned_label,
        "category": pinned_category,
        "role": "pinned",
    }

    # Build answer_nodes — never drop a picked CURIE.
    answer_nodes: list[dict[str, Any]] = []
    picked_set = set(picked_answer_curies)
    for curie in picked_answer_curies:
        kg_node = nodes_block.get(curie) or {}
        cats = kg_node.get("categories") or []
        answer_nodes.append({
            "curie": curie,
            "label": kg_node.get("name"),
            "category": cats[0] if cats else None,
            "role": "answer",
        })

    # Build edges — only those that touch (pinned_curie + a picked answer).
    # the orientation can be subject=pinned or subject=answer; both are
    # legitimate per TRAPI, so we accept either and preserve source/target
    # exactly as the graph had them.
    relevant_pair = picked_set | {pinned_curie}
    edges_out: list[dict[str, Any]] = []
    for edge_id, e in edges_block.items():
        subj = e.get("subject")
        obj = e.get("object")
        if not (subj in relevant_pair and obj in relevant_pair):
            continue
        if pinned_curie not in (subj, obj):
            continue
        # at least one endpoint must be a picked answer (otherwise it's a
        # pinned↔pinned self-edge, which TRAPI shouldn't produce but
        # we guard against)
        if not ((subj in picked_set) or (obj in picked_set)):
            continue
        attrs = e.get("attributes") or []
        # walk the attributes list once, picking up each provenance field
        # by its attribute_type_id. None / empty defaults for absent ones.
        knowledge_level: str | None = None
        primary_ks: str | None = None
        supporting_publications: list[str] = []
        supporting_text_raw: dict[str, Any] = {}
        for attr in attrs:
            type_id = attr.get("attribute_type_id")
            value = attr.get("value")
            if type_id == _ATTR_KNOWLEDGE_LEVEL and isinstance(value, str):
                knowledge_level = value
            elif type_id == _ATTR_PRIMARY_KS and isinstance(value, str):
                primary_ks = value
            elif type_id == _ATTR_PUBLICATIONS and isinstance(value, list):
                supporting_publications = list(value)
            elif type_id == _ATTR_SUPPORTING_TEXT and isinstance(value, dict):
                supporting_text_raw = value
        # flatten supporting_text from {pmid: {date, sentence, ...}} into
        # a list of {pmid, date, sentence} for easier rendering.
        supporting_text_snippets: list[dict[str, Any]] = [
            {
                "pmid": pmid,
                "date": (record or {}).get("publication date"),
                "sentence": (record or {}).get("sentence"),
            }
            for pmid, record in supporting_text_raw.items()
        ]
        edges_out.append({
            "id": edge_id,
            "source": subj,
            "target": obj,
            "predicate": e.get("predicate"),
            "knowledge_level": knowledge_level,
            "primary_knowledge_source": primary_ks,
            "supporting_publications": supporting_publications,
            "supporting_text_snippets": supporting_text_snippets,
        })

    return {
        "pinned_node": pinned_node,
        "answer_nodes": answer_nodes,
        "edges": edges_out,
    }


# Stage 7 threshold. exposed as a module constant so tests can assert
# against the same value the pipeline uses, and so a future tuning sweep
# can change it in one place. 0.50 was chosen empirically:
#   - "type 2 diabites" vs "sialidosis type 2" scores 0.38 → fails (good)
#   - "warfrin" vs "warfarin" scores 0.93 → passes (good)
#   - "type 2 diabetes" vs "type 2 diabetes mellitus" scores 1.00 (substring) → passes (good)
LOW_CONFIDENCE_THRESHOLD = 0.50


# Stage 3 NameRes tuning. we ask NameRes for a WIDE candidate set
# (NAMERES_LIMIT) on the principle that BM25 ranking is a recall filter
# rather than a precision ranker — a wider net means the right CURIE is
# more likely to appear *somewhere* in the result, even if BM25 buries
# it. then we re-rank locally with signals BM25 ignores (exact label /
# synonym match, type alignment with Stage 2's expected_category) and
# only SHOW the top NAMERES_DISPLAY of that re-ranked list to Stage 4,
# and PROBE the first cfg.retriever.probe_candidates of those against
# Tier 0 (a probe of a hub entity can take tens of seconds, see
# config.yaml).
NAMERES_LIMIT = 20
NAMERES_DISPLAY = 10


def _rerank_nameres_candidates(
    candidates: list[dict[str, Any]],
    mention: str,
    expected_category: str | None,
) -> list[dict[str, Any]]:
    # local re-rank that the BM25 score alone won't deliver. tiers go
    # MOST-discriminating first, BM25 last. lexicographic sort over the
    # tier tuple means a Tier-1 hit (exact label match) wins over a
    # Tier-4 BM25 spike no matter how big the BM25 difference is.
    #
    # tiers (each is 0 or 1, summed in a tuple and sorted DESC):
    #   T1 exact_label    — candidate.label == mention (case-folded)
    #   T2 exact_synonym  — mention appears verbatim in candidate.synonyms
    #   T3 token_match    — mention appears as a whole token (split on
    #                       whitespace) in label OR any synonym; catches
    #                       "seizures" inside "Febrile Seizure" / etc.
    #   T4 type_match     — Stage 2's expected_category is in candidate.types
    #   T5 bm25_score     — fall-through; NameRes's original ranking
    #
    # rationale per tier:
    # - T1/T2 fix the canonical-short-label problem: "Seizure" (HP) gets
    #   buried by BM25 under "Hypoglycemic seizures" because BM25 rewards
    #   token-overlap with longer labels.
    # - T3 catches the case where the user's mention is one word inside a
    #   longer canonical label (most common in HP/MONDO).
    # - T4 reverses BM25's type-blindness when the loose-neighborhood
    #   biolink_type filter lets adjacent-type entries in. for "seizures"
    #   that means HP (PhenotypicFeature) candidates beat MONDO (Disease)
    #   candidates even though MONDO scores ~165 points higher in BM25.
    # - T5 is the safety net so candidates with NO discriminating signal
    #   are still ordered deterministically by their original rank.
    m = mention.lower().strip()

    def key(c: dict[str, Any]) -> tuple[int, int, int, int, float]:
        label = str(c.get("label") or "").lower().strip()
        synonyms = [
            str(s).lower().strip()
            for s in (c.get("synonyms") or [])
            if s is not None
        ]
        types = c.get("types") or []
        try:
            bm25 = float(c.get("score") or 0.0)
        except (TypeError, ValueError):
            bm25 = 0.0

        exact_label = 1 if label == m else 0
        exact_syn = 1 if m in synonyms else 0
        label_tokens = set(label.split())
        syn_tokens = {tok for s in synonyms for tok in s.split()}
        token_match = 1 if (m in label_tokens or m in syn_tokens) else 0
        type_match = 1 if expected_category and expected_category in types else 0

        return (exact_label, exact_syn, token_match, type_match, bm25)

    return sorted(candidates, key=key, reverse=True)


def _probe_candidates(
    *,
    candidates: list[dict[str, Any]],
    primary_mention_cat: str,
    answer_category: str | None,
    retriever: RetrieverClient,
    limit: int,
    logger: logging.Logger,
    tag: str,
) -> dict[str, Any]:
    # per-candidate fact-density probe against the answer category, for
    # the first `limit` candidates, in parallel. called twice in the
    # strict-first / loose-fallback flow — once against the strict
    # candidate set, optionally again against the loose set if strict
    # had no coverage. each probe is one Tier 0 TRAPI call to Retriever;
    # failures and timeouts degrade to {total_edges:0, error:str} so the
    # caller can tell "we tried and it is unknown" from "no facts".
    probes: dict[str, Any] = {}
    if not (answer_category and candidates):
        return probes
    curies = [
        c["curie"] for c in candidates[:limit]
        if isinstance(c.get("curie"), str) and c["curie"]
    ]

    def probe_one(c_curie: str) -> dict[str, Any]:
        try:
            probe = retriever.probe_predicates(
                c_curie, primary_mention_cat, answer_category,
            )
        except RetrieverError as e:
            logger.warning(f"{tag}  probe failed for {c_curie}: {e} (non-fatal)")
            return {
                "pinned_curie": c_curie,
                "pinned_cat": primary_mention_cat,
                "answer_cat": answer_category,
                "total_edges": 0,
                "by_predicate": {},
                "qualified_by_predicate": {},
                "latency_s": 0.0,
                "error": str(e),
            }
        return {
            "pinned_curie": probe.pinned_curie,
            "pinned_cat": probe.pinned_cat,
            "answer_cat": probe.answer_cat,
            "total_edges": probe.total_edges,
            "by_predicate": probe.by_predicate,
            "qualified_by_predicate": probe.qualified_by_predicate,
            "latency_s": probe.latency_s,
            "error": probe.error,
        }

    # in_context: the probes log as part of the question that asked
    # them, so its live stream in the API still shows them.
    with ThreadPoolExecutor(max_workers=max(1, len(curies))) as pool:
        for c_curie, result in zip(curies, pool.map(in_context(probe_one), curies), strict=True):
            probes[c_curie] = result
    return probes


# a predicate with qualified edges usually has one or two qualifier
# sets (decreased / increased activity); a few have a long tail of
# aspect variants. five covered every case we measured on RTX-KG2c
# (the graph before Tier 0) without letting one predicate flood the
# Stage 8 prompt.
MAX_QUALIFIER_SETS_SHOWN = 5


def _render_qualified_lines(qualified: dict[str, int], *, limit: int) -> list[str]:
    # the sub-lines under one predicate in the Stage 8 probe block:
    # which qualifier sets its edges carry and how many edges each.
    # densest first; equal counts by text so the prompt is reproducible.
    ordered = sorted(qualified.items(), key=lambda item: (-item[1], item[0]))
    lines = [
        f"      qualified: {count} edges with {signature}"
        for signature, count in ordered[:limit]
    ]
    hidden = len(ordered) - limit
    if hidden > 0:
        noun = "combination" if hidden == 1 else "combinations"
        lines.append(f"      ({hidden} more qualifier {noun} not shown)")
    return lines


def _has_any_kg_coverage(probes: dict[str, Any]) -> bool:
    # true if at least one probed candidate found ≥1 Tier 0 fact to the
    # answer category, or could not be checked (a timeout on a hub
    # entity is "unknown", not "empty"). used to decide whether the
    # strict NameRes pass already gives us workable candidates, or
    # whether we need to fall back to the loose BMT-derived
    # neighborhood filter.
    return any(
        (p.get("total_edges") or 0) > 0 or p.get("error")
        for p in probes.values()
    )


def _check_label_consistency(mention: str, label: str) -> tuple[float, dict[str, Any]]:
    # Stage 7's similarity check, factored out for unit-testability.
    # returns (similarity_score, debug_info_dict). pure function — no
    # IO, no globals. the pipeline takes the returned similarity and
    # compares to LOW_CONFIDENCE_THRESHOLD; tests assert on the score
    # directly so threshold changes don't invalidate the test set.
    #
    # similarity = max of:
    #   (a) SequenceMatcher.ratio  ≈ character-level Levenshtein similarity
    #   (b) substring containment  (1.0 if one string is in the other else 0.0)
    # token-set Jaccard was DELIBERATELY removed: scaffolding tokens like
    # "type", "2", "the" give false high similarity for the diabetes-typo
    # failure ("type 2 diabites" vs "sialidosis type 2" shares {"type", "2"}
    # → Jaccard 0.5, masking the typo). see git blame for the bug.
    mention_normalized = mention.lower().strip()
    label_normalized = label.lower().strip()
    # both-empty corner case: SequenceMatcher.ratio("", "") returns 1.0
    # in stdlib (empty trivially matches empty), but two empty strings
    # carry no information — a 1.0 score here would let a junk
    # resolution pass the consistency threshold. caught by unit test
    # test_empty_inputs_score_zero. handle explicitly before delegating.
    if not mention_normalized or not label_normalized:
        return 0.0, {
            "mention_normalized": mention_normalized,
            "label_normalized": label_normalized,
            "seqmatcher_ratio": 0.0,
            "substring_match": False,
        }
    seq = SequenceMatcher(None, mention_normalized, label_normalized).ratio()
    substr = mention_normalized in label_normalized or label_normalized in mention_normalized
    sim = max(seq, 1.0 if substr else 0.0)
    return sim, {
        "mention_normalized": mention_normalized,
        "label_normalized": label_normalized,
        "seqmatcher_ratio": seq,
        "substring_match": substr,
    }


def _best_label_match(
    mention: str, primary_label: str, extra_labels: list[str],
) -> tuple[float, str, dict[str, Any]]:
    # Stage 7's gate compares the mention against every name the
    # resolved entity is known by, not only NodeNorm's preferred label.
    # NodeNorm sometimes prefers a stray synonym as the canonical label
    # (MONDO:0007915 → "EXCESS LMW-DNA" for systemic lupus erythematosus,
    # MONDO:0008114 → "Compulsion" for OCD) while the proper name sits
    # in the NameRes synonyms / equivalent identifiers; scoring only the
    # preferred label rejected correct CURIEs in 2 of 20 pilot stories.
    #
    # the primary label keeps the original lenient rule (typo-tolerant
    # ratio + substring). extras must be a naming variant of the whole
    # mention (token-subset, _is_same_concept_variant) — a fragment
    # synonym such as "lupus" or "syndrome" must not wave a wrong
    # entity through.
    best, debug = _check_label_consistency(mention, primary_label)
    best_label = primary_label
    seen = {primary_label.lower().strip()}
    for lab in extra_labels:
        key = lab.lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        if best < 1.0 and _is_same_concept_variant(mention, lab):
            best, best_label = 1.0, lab
    debug = dict(debug)
    debug["best_label"] = best_label
    debug["n_labels_checked"] = len(seen)
    return best, best_label, debug


def _stem_tokens(text: str) -> set[str]:
    # crude plural folding so "seizures" and "Seizure" compare equal.
    # anything smarter (a real stemmer) would be a new dependency for
    # no measured gain.
    out: set[str] = set()
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        out.add(tok[:-1] if len(tok) > 3 and tok.endswith("s") else tok)
    return out


def _is_same_concept_variant(mention: str, label: str) -> bool:
    # Stage 5's swap-eligibility test, factored out for unit-testability.
    # a swap target must be a naming variant of the user's mention —
    # "Seizure" for "seizures", "diabetes mellitus" for "diabetes" — so
    # every token of the mention must survive in the label. this is
    # deliberately token-based, not character-based: "vitamin b
    # deficiency" is 95% character-similar to "vitamin d deficiency" and
    # a different disease; "syndrome" is a substring of "marfan
    # syndrome" and a generic fragment. both must be rejected, and both
    # fail the token test (missing "d", missing "marfan").
    m_toks = _stem_tokens(mention)
    l_toks = _stem_tokens(label)
    if not m_toks or not l_toks:
        return False
    return m_toks <= l_toks


def _format_low_confidence_explanation(
    mention: str, canonical: str, label: str, similarity: float,
) -> str:
    # Markdown body for the explanation.md artifact when Stage 7 refuses
    # to query the graph because the resolved entity has low textual
    # similarity to what the user typed. mirrors the section structure of
    # the normal explainer + the out_of_scope refusal so the UI renders
    # the same component, just with a low_confidence_resolution badge.
    return (
        "## Answer\n\n"
        f"PloverAI could not confidently resolve **{mention}** to a "
        f"known biomedical entity in the Translator knowledge graph.\n\n"
        "## Reason\n\n"
        f"The closest match found was **{canonical}** "
        f"(*{label}*), but the resolved label is too different from what "
        f"you typed (similarity {similarity:.2f}, threshold 0.50). "
        f"Asking ARAX about this entity would likely return results "
        f"for a different concept than you intended, so the pipeline "
        f"stopped here rather than silently grounding the wrong entity.\n\n"
        "## What to try\n\n"
        "- Check the spelling of the entity in your question.\n"
        "- Use the canonical name (e.g., *type 2 diabetes mellitus* "
        "instead of *type 2 diabites*).\n"
        "- Expand abbreviations if the entity is well-known by its full "
        "name (e.g., *T2DM* → *type 2 diabetes mellitus*).\n"
        "- If you intended a different entity, please rephrase the "
        "question with the entity's full name.\n"
    )


def _format_out_of_scope_explanation(reason: str) -> str:
    # short Markdown body for the explanation.md artifact when Stage 1
    # refuses a question. matches the section structure of the normal
    # explainer output so the UI renders the same component for both,
    # just with the out_of_scope status badge.
    reason_line = reason.strip() if reason else (
        "This input does not look like a biomedical question."
    )
    return (
        "## Answer\n\n"
        "This question is outside PloverAI's scope and was not run "
        "against the knowledge graph.\n\n"
        "## Reason\n\n"
        f"{reason_line}\n\n"
        "## What PloverAI can answer\n\n"
        "PloverAI answers biomedical questions with ARAX, which reasons "
        "over the NCATS Biomedical Data Translator's Tier 0 knowledge "
        "graph of relationships between drugs, diseases, "
        "genes, proteins, chemicals, phenotypes, biological processes, "
        "pathways, and anatomical structures. Examples it can handle:\n\n"
        "- *What drugs treat type 2 diabetes?*\n"
        "- *Which genes are associated with cystic fibrosis?*\n"
        "- *What pathways involve HMGCR?*\n"
        "- *Which diseases present with seizures?*\n"
    )


def stage8_decline(reply: dict[str, Any]) -> str | None:
    # SYS_TRAPI_BUILD's escape hatch is {"error": "<reason>"}. it is not
    # a query graph, and reasoner-validator passes it anyway (it checks
    # the graph it finds, and there is none), so it must be caught here
    # before a reasoner answers it with an HTTP error.
    if "message" in reply or "error" not in reply:
        return None
    return str(reply.get("error") or "").strip() or "(no reason given)"


def _format_query_declined_explanation(reason: str) -> str:
    return (
        "## Answer\n\n"
        "PloverAI could not build a knowledge-graph query for this "
        "question, so no answer is given.\n\n"
        "## Reason\n\n"
        f"{reason}\n"
    )


def _llm_response_meta(rep: Any) -> dict[str, Any]:
    # pulls the small but valuable metadata fields from a raw LLM
    # response: reasoning (Anthropic / DeepSeek expose it differently),
    # finish_reason, refusal, and the canonical model id the provider
    # echoed back. raw `content` is intentionally omitted — it lives
    # in its own per-stage destination (answer.json, trapi_query.json,
    # explanation.md). this metadata feeds the "Reasoning" card in the
    # research-grade UI.
    choices = rep.raw.get("choices") or []
    if not choices:
        return {}
    msg = choices[0].get("message") or {}
    return {
        "reasoning": msg.get("reasoning") or msg.get("reasoning_content"),
        "finish_reason": choices[0].get("finish_reason"),
        "refusal": msg.get("refusal"),
        "model_returned": rep.raw.get("model"),
        "input_tokens": rep.input_tokens,
        "output_tokens": rep.output_tokens,
        "latency_s": round(rep.latency_s, 3),
    }


@dataclass(frozen=True)
class _ScopeCheckOutput:
    # Stage 1's contract: {in_scope: bool, reason: str}. parsed
    # tolerantly the same way Stage 2 is — fenced JSON, plain JSON,
    # or a legacy string fallback that defaults to "in scope" so the
    # pipeline never refuses a question because the guardrail
    # mis-parsed itself.
    in_scope: bool
    reason: str


def _parse_scope_check_output(raw: str) -> _ScopeCheckOutput:
    text = raw.strip().strip('"').strip("'")
    if not text:
        return _ScopeCheckOutput(in_scope=True, reason="")
    try:
        obj = _extract_json(text)
    except (json.JSONDecodeError, ValueError):
        # legacy / malformed: fail OPEN, not closed. blocking a real
        # biomedical question because the guardrail's own output was
        # malformed is worse than letting one chit-chat through.
        return _ScopeCheckOutput(in_scope=True, reason="")
    in_scope_val = obj.get("in_scope")
    reason_val = obj.get("reason", "")
    in_scope = bool(in_scope_val) if isinstance(in_scope_val, bool) else True
    reason = str(reason_val).strip() if isinstance(reason_val, str) else ""
    return _ScopeCheckOutput(in_scope=in_scope, reason=reason)


@dataclass(frozen=True)
class _Stage0Output:
    mention: str
    expected_category: str | None       # for NameRes biolink_type filter
    answer_category: str | None         # for Stage 8 predicate-list lookup
    granularity_preference: str         # "general" | "specific"; defaults to general


def _parse_stage0_output(raw: str) -> _Stage0Output:
    # Stage 2 is contracted to return JSON:
    #   {"entity": "...", "expected_category": "biolink:...",
    #    "answer_category": "biolink:...", "granularity_preference": "general"|"specific"}
    # tolerance, by precedence:
    #   1. perfect JSON object
    #   2. JSON wrapped in ```json ... ```
    #   3. plain string (legacy, pre-2026-05 output)
    # missing fields are treated as None / "general" so the pipeline
    # always has something to run with — worst case it falls back to
    # the pre-fix behaviour with a warning.
    text = raw.strip().strip('"').strip("'")
    if not text:
        return _Stage0Output("", None, None, "general")
    try:
        obj = _extract_json(text)
    except (json.JSONDecodeError, ValueError):
        return _Stage0Output(text, None, None, "general")
    entity_val: Any = obj.get("entity") or obj.get("name") or ""
    expected_cat_val: Any = obj.get("expected_category") or obj.get("category")
    answer_cat_val: Any = obj.get("answer_category")
    gran_val: Any = obj.get("granularity_preference") or "general"
    mention = str(entity_val).strip().strip('"').strip("'")
    expected_category = (
        str(expected_cat_val).strip()
        if isinstance(expected_cat_val, str) and expected_cat_val.strip()
        else None
    )
    answer_category = (
        str(answer_cat_val).strip()
        if isinstance(answer_cat_val, str) and answer_cat_val.strip()
        else None
    )
    granularity = "specific" if str(gran_val).strip().lower() == "specific" else "general"
    return _Stage0Output(mention, expected_category, answer_category, granularity)


MAX_REPAIRED_BRACES = 3


def _normalise_answers(raw: Any) -> list[dict[str, Any]]:
    # stage 11 asks for a list of {curie, label, supporting_edge_ids}
    # objects; some models return bare CURIE strings, or a dict keyed by
    # CURIE. accept those shapes instead of crashing the cell — the
    # smoke run lost deepseek-v4-flash-0731 to `'list' object has no
    # attribute 'get'` on a list of strings.
    if isinstance(raw, dict):
        raw = [{"curie": k, **(v if isinstance(v, dict) else {})} for k, v in raw.items()]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("curie"), str) and item["curie"]:
            out.append(item)
        elif isinstance(item, str) and item.strip():
            out.append({"curie": item.strip(), "label": item.strip(), "supporting_edge_ids": []})
    return out


def _extract_json(text: str, *, allow_list: bool = False) -> dict[str, Any]:
    # the LLM sometimes wraps JSON in ```json ... ``` despite our prompt.
    # we strip fences if present, then try plain json.loads, then decode
    # the first complete object. the result is always a dict: a top-level
    # array is an error unless allow_list is set, in which case it is
    # taken as the `answers` list (stage 11 only — ling-3.0-flash
    # returned a bare array there and crashed the cell on `.get`).
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```\s*$", "", s)
    try:
        loaded: Any = json.loads(s)
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, dict):
        return loaded
    if isinstance(loaded, list):
        if allow_list:
            return {"answers": loaded}
        raise json.JSONDecodeError("top-level JSON is an array, not an object", s, 0)
    if loaded is not None:
        raise json.JSONDecodeError("top-level JSON is not an object", s, 0)
    # trailing prose or a second object after the JSON: decode the first
    # complete object starting at the first brace. the greedy {.*} regex
    # used before spanned to the LAST brace and broke on exactly that.
    start = s.find("{")
    if start < 0:
        raise json.JSONDecodeError("no JSON object in reply", s, 0)
    body = s[start:]
    try:
        decoded, _end = json.JSONDecoder().raw_decode(body)
    except json.JSONDecodeError:
        # the most common model slip is a missing trailing brace (the
        # 13-slug smoke lost ling-3.0-flash to a query graph short by
        # exactly one). append only the closers the brace count says
        # are missing, at most three, and never invent anything else.
        missing = body.count("{") - body.count("}")
        if not 0 < missing <= MAX_REPAIRED_BRACES:
            raise
        decoded, _end = json.JSONDecoder().raw_decode(body + "}" * missing)
    if not isinstance(decoded, dict):
        raise json.JSONDecodeError("top-level JSON is not an object", s, start)
    loaded2: dict[str, Any] = decoded
    return loaded2


# the six OpenRouter calls, in pipeline order. these names are the
# `stage` label on every LLM call and cost-ledger entry, and the keys a
# stage swap (--stage-model answer_pick=m1) may use.
LLM_STAGES = (
    "scope_check", "entity_extract", "candidate_pick",
    "trapi_build", "answer_pick", "explain",
)

# which service answers the query at Stage 10. "arax" (the default, and
# the only one the UI offers) sends the query to ARAX, which reasons
# over Tier 0 and returns reasoning paths; "lookup" sends it to
# Retriever as a one-hop Tier 0 lookup and leaves the reasoning to the
# LLM, the benchmark's comparison condition.
REASONERS = ("arax", "lookup")

# oracle modes: which gold answer is handed to the pipeline. "entity"
# replaces Stages 2-7 with the gold pinned entity; "query" additionally
# replaces Stage 8 with the gold query graph.
ORACLE_MODES = ("entity", "query")


def _ui_event(logger: logging.Logger, tag: str, stage: str, **data: Any) -> None:
    # one structured step for the UI's live graph (entity, query,
    # results, answers, graph). the API's SSE handler forwards the
    # "ui_event" extra as a "stage" event; the runner's console and
    # run.log only show the one-line message.
    logger.info(f"{tag}  ui_event  stage={stage}", extra={"ui_event": {"stage": stage, **data}})


# ARAX's progress lines reach the UI at most this often; it can log
# dozens a second while it expands the graph.
ARAX_PROGRESS_EVERY_S = 1.0


def _arax_progress_relay(logger: logging.Logger, tag: str) -> Callable[[str], None]:
    # passes ARAX's own account of what it is doing to the live view, in
    # plain words (arax_client.progress_words; bookkeeping lines are
    # dropped), skipping repeats and throttled.
    last: dict[str, Any] = {"at": 0.0, "text": ""}

    def relay(message: str) -> None:
        text = progress_words(message)
        now = time.perf_counter()
        if not text or text == last["text"] or now - last["at"] < ARAX_PROGRESS_EVERY_S:
            return
        last.update(at=now, text=text)
        _ui_event(logger, tag, "arax_progress", message=text)

    return relay


def categories_known_to_kg(
    categories: list[str], available_categories: list[str] | None,
) -> list[str]:
    # NodeNorm answers in a newer Biolink than the graph and the
    # validator, so a pinned entity can carry categories the validator
    # does not know.
    # Stage 8 copies what it is shown into n0, so it is shown only the
    # categories this KG carries (NodeNorm's order kept). with no KG
    # list, or no overlap, the original list is the best we have.
    if not available_categories:
        return categories
    known_set = set(available_categories)
    known = [category for category in categories if category in known_set]
    return known or categories


def oracle_entity(q: dict[str, Any]) -> tuple[str, str, list[str], str]:
    # curated q*.json and converted Translator records store the gold
    # entity the same way; only the query graph lives under different
    # keys (see oracle_query_graph).
    raw_pinned = q.get("pinned_entity")
    pinned: dict[str, Any] = raw_pinned if isinstance(raw_pinned, dict) else {}
    curie = pinned.get("curie")
    if not curie:
        raise ValueError(f"question {q.get('id')!r} has no gold pinned entity")
    return (
        str(curie),
        str(pinned.get("label") or pinned["curie"]),
        [str(pinned["category"])] if pinned.get("category") else [],
        str(q.get("answer_category") or ""),
    )


def oracle_query_graph(q: dict[str, Any]) -> dict[str, Any]:
    # a missing graph and a malformed one mean the same thing here: this
    # record has no gold query to hand over.
    raw_graph = q.get("gold_query_graph") or (q.get("validation") or {}).get("query_graph")
    graph: dict[str, Any] = raw_graph if isinstance(raw_graph, dict) else {}
    if not graph:
        raise ValueError(f"question {q.get('id')!r} has no gold query graph")
    return {"message": {"query_graph": graph}}


def run_grounded(
    *,
    cfg: Config,
    model: ModelSpec,
    q: dict[str, Any],
    qp: QuestionPaths,
    llm: OpenRouterClient,
    nameres: NameResClient,
    nodenorm: NodeNormClient,
    retriever: RetrieverClient,
    logger: logging.Logger,
    predicate_index: dict[tuple[str, str], list[str]] | None = None,
    pubtator: PubTatorClient | None = None,
    available_categories: list[str] | None = None,
    biolink_neighborhoods: dict[str, list[str]] | None = None,
    stage_models: dict[str, ModelSpec] | None = None,
    oracle: str | None = None,
    reasoner: str = "arax",
    arax: AraxClient | None = None,
) -> QuestionResult:
    # `predicate_index` is the (subject_cat, object_cat) -> [predicates]
    # map built from Retriever's (Tier 0) meta_knowledge_graph at boot. when
    # supplied, Stage 8 is told to pick a predicate from the filtered
    # list (kills predicate hallucination). when None, Stage 8 falls
    # back to its prior unconstrained behaviour — handy for tests or
    # for runs where meta_KG fetch failed at start-up.
    # full grounded pipeline. at each step we write to disk before
    # moving on, so a crash mid-run leaves us with everything we had
    # up to the failure point.
    write_json(qp.question, q)
    ledger = CostLedger()
    t_start = time.perf_counter()

    # the prompt log accumulates one entry per LLM call. we write it
    # incrementally at the end of each stage so partial runs still
    # produce useful artifacts.
    prompt_log: dict[str, Any] = {}
    # nodenorm artifact has two sections: pinned (Stage 6) and
    # answers (Stage 12). we update both into one file.
    nodenorm_log: dict[str, Any] = {"pinned": None, "answers": None}

    nl_question = q["nl_question"]
    tag = f"[blue]{model.id}[/]/[cyan]{q['id']}[/]/grounded"

    # a stage swap runs ONE LLM stage on another model (see
    # runner.parse_stage_models); every other stage uses `model`.
    def stage_model(stage: str) -> ModelSpec:
        return (stage_models or {}).get(stage, model)
    logger.info(f"{tag}  question=[white]{nl_question!r}[/]")


    # ---------- Stage 1: scope-check guardrail ----------
    # decides whether the question is biomedical at all. refusing
    # here costs ~1 cheap LLM call and saves the full pipeline cost
    # (~6 LLM calls + 4 RENCI calls + ARAX) on out-of-scope
    # input. failures fall through to STAGE_0 (fail-open) because
    # blocking a real biomedical question on a guardrail glitch is
    # worse than letting the rare chit-chat through.
    user_msg_scope = f"Question: {nl_question}"
    prompt_log["stage_1_scope_check"] = {
        "system": prompts.SYS_SCOPE_CHECK,
        "user": user_msg_scope,
    }
    write_json(qp.prompt, prompt_log)
    try:
        rep_scope = llm.chat(
            model=stage_model("scope_check"),
            system=prompts.SYS_SCOPE_CHECK,
            user=user_msg_scope,
            stage="scope_check",
        )
    except OpenRouterError as e:
        return _finish_failure(qp, ledger, t_start, q["id"], STATUS_LLM_ERROR, str(e))
    prompt_log["stage_1_scope_check"]["response"] = _llm_response_meta(rep_scope)
    write_json(qp.prompt, prompt_log)
    ledger.add(
        "scope_check", stage_model("scope_check").id, stage_model("scope_check").slug,
        rep_scope.input_tokens, rep_scope.output_tokens,
        rep_scope.cost.input_usd, rep_scope.cost.output_usd, rep_scope.cost.total_usd,
        rep_scope.latency_s,
    )
    scope = _parse_scope_check_output(rep_scope.content)
    logger.info(
        f"{tag}  scope_check  in_scope={scope.in_scope}  "
        f"reason=[white]{scope.reason or '(none)'!r}[/]"
    )
    if not scope.in_scope:
        # write a clean markdown explanation so the UI has something
        # to render — no TRAPI query, no graph hit, no further cost.
        explanation = _format_out_of_scope_explanation(scope.reason)
        write_text(qp.explanation, explanation)
        _flush_meta_and_cost(
            qp, ledger, t_start, q["id"],
            STATUS_OUT_OF_SCOPE, scope.reason or "question is outside biomedical-KG scope",
            outcome="out_of_scope",
            outcome_reason=scope.reason,
            n_results=-1, answers_n_picked=-1,
        )
        return QuestionResult(
            q_id=q["id"],
            status=STATUS_OUT_OF_SCOPE,
            cost_total_usd=ledger.total_usd(),
            cost_total_tokens=ledger.total_tokens(),
            elapsed_s=round(time.perf_counter() - t_start, 3),
            error=None,
            outcome="out_of_scope",
            outcome_reason=scope.reason,
        )

    if oracle is None:
        # ---------- Stage 2: extract focal entity mention from NL ----------
        # we inject the list of Biolink categories the Tier 0 graph
        # ACTUALLY has (sourced from Retriever's meta_knowledge_graph at
        # startup).
        # the LLM must pick expected_category / answer_category from this
        # exact list — no hallucinating biolink:CellType when the KG only
        # has biolink:Cell. when the list is missing (meta-KG fetch failed
        # at boot), the LLM falls back to the system-prompt's catch-all
        # advice and still produces something usable.
        categories_block = (
            "Available Biolink categories in the knowledge graph "
            "(pick expected_category / answer_category from this list):\n"
            + "\n".join(f"  - {c}" for c in (available_categories or []))
            + "\n\n"
            if available_categories else ""
        )
        user_msg_0 = (
            f"{categories_block}"
            f"Question: {nl_question}\n\n"
            f"Return the focal entity name."
        )
        prompt_log["stage_2_entity_extract"] = {
            "system": prompts.SYS_ENTITY_EXTRACT,
            "user": user_msg_0,
        }
        write_json(qp.prompt, prompt_log)

        try:
            rep0 = llm.chat(
                model=stage_model("entity_extract"),
                system=prompts.SYS_ENTITY_EXTRACT,
                user=user_msg_0,
                stage="entity_extract",
            )
        except OpenRouterError as e:
            return _finish_failure(qp, ledger, t_start, q["id"], STATUS_LLM_ERROR, str(e))
        prompt_log["stage_2_entity_extract"]["response"] = _llm_response_meta(rep0)
        write_json(qp.prompt, prompt_log)
        ledger.add(
            "entity_extract", stage_model("entity_extract").id, stage_model("entity_extract").slug,
            rep0.input_tokens, rep0.output_tokens,
            rep0.cost.input_usd, rep0.cost.output_usd, rep0.cost.total_usd,
            rep0.latency_s,
        )
        # Stage 2 emits JSON with four fields: entity, expected_category,
        # answer_category, granularity_preference. three of them steer
        # later stages:
        #   - expected_category → NameRes biolink_type filter (entity type)
        #   - granularity_preference → IC-based re-ranking (entity granularity)
        #   - answer_category → meta_KG predicate-list lookup for Stage 8
        # see prompts.SYS_ENTITY_EXTRACT for the rationale; this is the consumer.
        s0 = _parse_stage0_output(rep0.content)
        mention = s0.mention
        expected_category = s0.expected_category
        answer_category = s0.answer_category
        granularity = s0.granularity_preference
        # surface the Stage 2 decision so the pipeline-progress stream
        # shows WHAT was extracted, not just token counts. this is where
        # a typo that survived Stage 2 spell-correction first becomes
        # visible (e.g. "type 2 diabites" if the LLM didn't normalize).
        logger.info(
            f"{tag}  entity_extract  entity=[white]{mention!r}[/]  "
            f"expected_category={expected_category or '(none)'}  "
            f"answer_category={answer_category or '(none)'}  "
            f"granularity={granularity}"
        )
        if not mention:
            return _finish_failure(
                qp, ledger, t_start, q["id"],
                STATUS_ENTITY_EMPTY, "Stage 2 produced an empty entity mention",
            )

        # ---------- Stage 3: NameRes /lookup (strict-first, loose-fallback) ----------
        # we run NameRes TWICE in the worst case:
        #   PASS 1 (always):  STRICT filter = [expected_category] only.
        #                     gives us the precision-first set — candidates
        #                     whose type matches Stage 2's pick exactly.
        #   PASS 2 (sometimes): LOOSE filter = BMT-derived neighborhood
        #                     (expected_category + siblings + parent's children),
        #                     run only when STRICT gave us ZERO candidates with
        #                     Tier 0 facts to the answer category.
        #
        # rationale: strict gives us the right TYPE; loose gives us the right
        # CONCEPT-even-if-typed-as-a-sibling. running strict first preserves
        # precision (the seizures case: HP entries stay top despite MONDO
        # having higher BM25); falling back to loose preserves recall (the
        # cholesterol case: PANTHER+REACT are all 0-edge under strict, so we
        # loosen to surface GO:0006695 which is typed as BiologicalProcess).
        #
        # biolink:NamedThing still means "no filter" (None) for both passes.
        primary_mention_cat = expected_category or "biolink:NamedThing"
        strict_filter: list[str] | None = (
            [expected_category]
            if expected_category and expected_category != "biolink:NamedThing"
            else None
        )
        loose_filter: list[str] | None = loose_filter_for(
            expected_category, biolink_neighborhoods,
        )

        try:
            nr = nameres.lookup(
                mention=mention, limit=NAMERES_LIMIT, biolink_types=strict_filter,
            )
        except NameResError as e:
            write_json(qp.nameres, {"mention": mention, "error": str(e)})
            return _finish_failure(qp, ledger, t_start, q["id"],
                                   STATUS_NAMERES_FAILED, str(e))

        # local rerank (see _rerank_nameres_candidates for the tier scheme):
        # exact label / synonym / token / type matches outrank raw BM25.
        nr_candidates_full: list[dict[str, Any]] = _rerank_nameres_candidates(
            nr.candidates, mention, expected_category,
        )
        bm25_top1 = nr.candidates[0].get("curie") if nr.candidates else None
        bm25_top1_label = nr.candidates[0].get("label") if nr.candidates else None
        rerank_top1 = nr_candidates_full[0].get("curie") if nr_candidates_full else None
        rerank_top1_label = nr_candidates_full[0].get("label") if nr_candidates_full else None

        # probe the strict candidate set so we can decide whether to fall
        # back to loose. the probes run in parallel, usually 1-3 s.
        candidate_probes_by_curie: dict[str, Any] = _probe_candidates(
            candidates=nr_candidates_full,
            primary_mention_cat=primary_mention_cat,
            answer_category=answer_category,
            retriever=retriever,
            limit=cfg.retriever.probe_candidates,
            logger=logger,
            tag=f"{tag}  strict",
        )
        nameres_filter_used: list[str] | None = strict_filter
        strict_has_coverage = _has_any_kg_coverage(candidate_probes_by_curie)
        fallback_to_loose = False

        # decision: only fall back when STRICT yielded ZERO coverage AND a
        # genuinely DIFFERENT loose filter exists (BMT may return the same
        # single-element list for categories whose parent is generic).
        if (
            not strict_has_coverage
            and answer_category
            and loose_filter
            and set(loose_filter) != set(strict_filter or [])
        ):
            logger.info(
                f"{tag}  nameres_fallback  strict filter {strict_filter} had no "
                f"Tier 0 coverage in top-{cfg.retriever.probe_candidates} → retry with loose {loose_filter}"
            )
            try:
                loose_nr = nameres.lookup(
                    mention=mention, limit=NAMERES_LIMIT, biolink_types=loose_filter,
                )
                loose_reranked = _rerank_nameres_candidates(
                    loose_nr.candidates, mention, expected_category,
                )
                loose_probes = _probe_candidates(
                    candidates=loose_reranked,
                    primary_mention_cat=primary_mention_cat,
                    answer_category=answer_category,
                    retriever=retriever,
                    limit=cfg.retriever.probe_candidates,
                    logger=logger,
                    tag=f"{tag}  loose",
                )
                # adopt the loose pass as the working set. strict results stay
                # available for the audit trail in `strict_attempt` below.
                strict_attempt = {
                    "filter": strict_filter,
                    "bm25_top1_curie": bm25_top1,
                    "bm25_top1_label": bm25_top1_label,
                    "rerank_top1_curie": rerank_top1,
                    "rerank_top1_label": rerank_top1_label,
                    "candidates": nr_candidates_full,
                    "candidate_probes_by_curie": candidate_probes_by_curie,
                    "had_coverage": False,
                }
                nr = loose_nr
                nr_candidates_full = loose_reranked
                candidate_probes_by_curie = loose_probes
                bm25_top1 = nr.candidates[0].get("curie") if nr.candidates else None
                bm25_top1_label = nr.candidates[0].get("label") if nr.candidates else None
                rerank_top1 = (
                    nr_candidates_full[0].get("curie") if nr_candidates_full else None
                )
                rerank_top1_label = (
                    nr_candidates_full[0].get("label") if nr_candidates_full else None
                )
                nameres_filter_used = loose_filter
                fallback_to_loose = True
            except NameResError as e:
                logger.warning(
                    f"{tag}  nameres_fallback  loose retry failed ({e}); "
                    f"sticking with strict-pass results"
                )
                strict_attempt = None
        else:
            strict_attempt = None

        # `biolink_type_filter_applied` reports the filter that produced
        # the FINAL candidate set. `strict_attempt` records the discarded
        # strict pass when we fell back.
        write_json(qp.nameres, {
            "mention": nr.mention,
            "expected_category": expected_category,
            "biolink_type_filter_applied": nameres_filter_used,
            "granularity_preference": granularity,
            "rerank_applied": True,
            "rerank_top1_curie": rerank_top1,
            "rerank_top1_label": rerank_top1_label,
            "bm25_top1_curie": bm25_top1,
            "bm25_top1_label": bm25_top1_label,
            "fallback_to_loose": fallback_to_loose,
            "strict_attempt": strict_attempt,
            "candidates": nr_candidates_full,
            "top1_curie": rerank_top1,
            "top1_label": rerank_top1_label,
            "latency_s": round(nr.latency_s, 3),
        })
        if not rerank_top1:
            return _finish_failure(qp, ledger, t_start, q["id"],
                                   STATUS_NAMERES_FAILED,
                                   f"NameRes returned no candidates for {mention!r}")

        candidate_curies: list[str] = [
            str(c["curie"]) for c in nr_candidates_full if isinstance(c.get("curie"), str)
        ]
        bm25_rank_by_curie = {
            c.get("curie"): i + 1
            for i, c in enumerate(nr.candidates)
            if isinstance(c.get("curie"), str)
        }
        top3_summary = "  ".join(
            f"#{i+1} {c.get('curie')} '{(c.get('label') or '')[:30]}' "
            f"(bm25_rank=#{bm25_rank_by_curie.get(c.get('curie'), '?')} "
            f"score={c.get('score', 0):.0f})"
            for i, c in enumerate(nr_candidates_full[:3])
        )
        logger.info(f"{tag}  nameres_top3_reranked  {top3_summary}")

        # persist the WINNING pass's probes (strict if it had coverage,
        # otherwise loose). reading code (UI + Raw Artifacts) treats this
        # as the per-candidate edge-density manifest for whichever set
        # Stage 4 will see.
        if candidate_probes_by_curie:
            write_json(qp.candidate_probes, {
                "answer_cat": answer_category,
                "expected_mention_cat": primary_mention_cat,
                "fallback_to_loose": fallback_to_loose,
                "filter_applied": nameres_filter_used,
                "by_curie": candidate_probes_by_curie,
            })
            logger.info(
                f"{tag}  candidate_probe_summary  filter={nameres_filter_used}  " +
                "  ".join(
                    f"{cur}={data['total_edges']}"
                    for cur, data in candidate_probes_by_curie.items()
                )
            )

        # ---------- Stage 4: LLM picks among NameRes candidates ----------
        # NameRes is BM25 over ontology labels/synonyms. its top-1 can be wrong
        # in three documented failure modes:
        #   (a) the user mention has a typo Stage 2 didn't catch — all 5
        #       candidates share some tokens with the mention but none mean
        #       what the user meant ("type 2 diabites" → sialidosis type 2)
        #   (b) label-type collision — a longer label of a different class
        #       outranks the canonical short label of the right class
        #   (c) BM25 surface-overlap bias — a longer label that contains the
        #       query string beats the canonical (broader/shorter) label
        #
        # the LLM sees: question + normalized mention + expected category +
        # granularity + the top NAMERES_DISPLAY candidates. it picks one CURIE,
        # or returns null with a reason. null → STATUS_NO_CANDIDATE_MATCH
        # (fail loudly, no silent grounding to garbage).
        candidates_block_lines: list[str] = []
        for i, c in enumerate(nr_candidates_full[:NAMERES_DISPLAY], start=1):
            c_curie = c.get("curie")
            c_label = c.get("label")
            c_types = c.get("types") or []
            c_score = c.get("score")
            # if we ran the candidate-density probe, attach the per-candidate
            # fact count to the answer category. this lets the LLM avoid
            # the "lexical match but no facts" trap (measured on RTX-KG2c,
            # before Tier 0: PANTHER.PATHWAY:P00014 was picked for
            # cholesterol biosynthesis when PANTHER pathways had 0 edges
            # to Gene nodes — the Reactome / GO equivalents in the same
            # candidate set had them).
            probe_suffix = ""
            if c_curie in candidate_probes_by_curie:
                cp = candidate_probes_by_curie[c_curie]
                n = cp.get("total_edges", 0)
                err = cp.get("error")
                if err:
                    probe_suffix = f"  tier0_facts_to_{answer_category}=unknown({err})"
                else:
                    probe_suffix = f"  tier0_facts_to_{answer_category}={n}"
            candidates_block_lines.append(
                f"  {i}. curie={c_curie!r}  label={c_label!r}  "
                f"types={c_types}  bm25_score={c_score}{probe_suffix}"
            )
        candidates_block = "\n".join(candidates_block_lines)
        # the density-aware preamble is only added when we actually have
        # probe data — otherwise the block falls back to the old behaviour
        # and the LLM picks on label+score alone.
        density_preamble = ""
        if candidate_probes_by_curie:
            density_preamble = (
                f"The first candidates carry a `tier0_facts_to_{answer_category}` "
                f"count: the number of facts in the Translator Tier 0 graph "
                f"from that CURIE to any node of the answer category (in either "
                f"direction); `unknown` means the check timed out. A high-BM25 "
                f"candidate with ZERO facts will produce no results downstream — "
                f"prefer a slightly lower-ranked candidate that has facts over a "
                f"perfect-label one with no data.\n\n"
            )
        user_msg_pick = (
            f"User question: {nl_question}\n"
            f"User's mention (post-Stage-2 normalization): {mention!r}\n"
            f"Expected Biolink category for the mention: {expected_category or 'biolink:NamedThing'}\n"
            f"Granularity preference: {granularity}\n\n"
            f"{density_preamble}"
            f"NameRes candidates (top {len(nr_candidates_full[:NAMERES_DISPLAY])} "
            f"of {len(nr_candidates_full)} after local rerank — BM25 score "
            f"shown per row but the ORDER is the rerank, not the raw BM25):\n"
            f"{candidates_block}\n\n"
            f"Pick the best candidate, or return chosen_curie=null with a reason."
        )
        prompt_log["stage_4_candidate_pick"] = {
            "system": prompts.SYS_CANDIDATE_PICK,
            "user": user_msg_pick,
        }
        write_json(qp.prompt, prompt_log)

        # default fallback is the RERANK top-1, not the BM25 top-1 — if the
        # LLM bails (bad JSON, refusal we couldn't parse) we still want the
        # tier-promoted candidate rather than what BM25 surfaced.
        chosen_curie: str = rerank_top1 or ""
        candidate_pick_chosen: str | None = None
        candidate_pick_reason: str | None = None
        candidate_pick_fell_back: bool = False
        try:
            rep_pick = llm.chat(
                model=stage_model("candidate_pick"),
                system=prompts.SYS_CANDIDATE_PICK,
                user=user_msg_pick,
                stage="candidate_pick",
            )
            prompt_log["stage_4_candidate_pick"]["response"] = _llm_response_meta(rep_pick)
            write_json(qp.prompt, prompt_log)
            ledger.add(
                "candidate_pick", stage_model("candidate_pick").id, stage_model("candidate_pick").slug,
                rep_pick.input_tokens, rep_pick.output_tokens,
                rep_pick.cost.input_usd, rep_pick.cost.output_usd, rep_pick.cost.total_usd,
                rep_pick.latency_s,
            )
            try:
                pick_obj = _extract_json(rep_pick.content)
            except (json.JSONDecodeError, ValueError):
                # bad JSON: fall back to RERANK top-1. log but don't fail —
                # the IC rerank or downstream stages may still recover.
                logger.warning(
                    f"Stage 4 candidate_pick returned non-JSON; "
                    f"falling back to rerank top-1 ({rerank_top1})"
                )
                candidate_pick_fell_back = True
                pick_obj = {}
            pick_curie = pick_obj.get("chosen_curie")
            pick_reason = pick_obj.get("reason")
            candidate_pick_reason = (
                str(pick_reason).strip() if isinstance(pick_reason, str) else None
            )
            if pick_curie is None and not candidate_pick_fell_back:
                # the LLM explicitly declared no match. fail loudly so the
                # user sees the resolution problem instead of being told
                # "no results found" for a wrong-entity query.
                logger.warning(
                    f"Stage 4 candidate_pick: no candidate matches "
                    f"mention={mention!r} (reason: {candidate_pick_reason})"
                )
                write_json(qp.nameres, {
                    "mention": nr.mention,
                    "expected_category": expected_category,
                    "biolink_type_filter_applied": nameres_filter_used,
                    "granularity_preference": granularity,
                    "candidates": nr_candidates_full,
                    "nameres_top1_curie": rerank_top1,
                    "nameres_top1_label": rerank_top1_label,
                    "bm25_top1_curie": bm25_top1,
                    "bm25_top1_label": bm25_top1_label,
                    "candidate_pick": {
                        "chosen_curie": None,
                        "reason": candidate_pick_reason,
                        "fell_back": False,
                    },
                    "latency_s": round(nr.latency_s, 3),
                })
                return _finish_failure(
                    qp, ledger, t_start, q["id"],
                    STATUS_NO_CANDIDATE_MATCH,
                    candidate_pick_reason or
                    f"None of the {len(nr_candidates_full[:NAMERES_DISPLAY])} "
                    f"NameRes candidates matched the user's mention {mention!r}. "
                    f"The mention may be misspelled, ambiguous, or refer to an "
                    f"entity not in the KG."
                )
            if isinstance(pick_curie, str) and pick_curie in candidate_curies:
                chosen_curie = pick_curie
                candidate_pick_chosen = pick_curie
                # log every pick (whether it matched the rerank top-1 or not)
                # so the progress stream always shows the LLM's decision.
                # previously only logged on swap, which hid the case where
                # the LLM AGREED with top-1 — equally informative for debug.
                agreement = (
                    "agrees_with_rerank_top1"
                    if pick_curie == rerank_top1
                    else "OVERRIDES_rerank_top1"
                )
                logger.info(
                    f"{tag}  candidate_pick  chose=[white]{pick_curie}[/]  "
                    f"{agreement}  reason=[white]{candidate_pick_reason!r}[/]"
                )
            elif isinstance(pick_curie, str):
                # the LLM invented a CURIE that wasn't in the supplied list.
                # ignore the invention and fall back to NameRes top-1.
                logger.warning(
                    f"Stage 4 candidate_pick invented CURIE {pick_curie!r} "
                    f"not in supplied list; falling back to NameRes top-1"
                )
                candidate_pick_fell_back = True
        except OpenRouterError as e:
            # candidate_pick is a refinement, not a hard gate. if OpenRouter
            # errors here, fall back to NameRes top-1 and let downstream
            # stages do what they can.
            logger.warning(
                f"Stage 4 candidate_pick LLM call failed ({e}); "
                f"falling back to NameRes top-1"
            )
            candidate_pick_fell_back = True

        # ---------- Stage 5: re-rank NameRes candidates by information_content ----------
        # NameRes ranks by Solr BM25 — biased toward longer labels that
        # contain the query string ("Hypoglycemic seizures" beats "Seizure"
        # for the query "seizures"). when the question wants a broad concept
        # (granularity=general), we override that by sorting candidates by
        # information_content ASCENDING (lower IC = more general concept).
        # for granularity=specific we leave the Stage-4 pick alone.
        #
        # information_content comes from NodeNorm, so this costs one extra
        # NodeNorm batch call on the top-K (K=5). NodeNorm is fast and
        # batches all 5 in a single POST.
        reranked = False
        if granularity == "general" and len(candidate_curies) > 1:
            try:
                nn_candidates = nodenorm.normalize(candidate_curies)
                # smaller IC = more general → lowest IC first. NodeNorm
                # returns None for unresolvable CURIEs; we treat those as
                # large (sort them last) so unresolvable candidates can't
                # accidentally win the broad-concept lottery.
                def _ic_key(curie: str) -> float:
                    ic = nn_candidates.information_content.get(curie)
                    return ic if ic is not None else float("inf")
                # the swap may only move to a BROADER FORM OF THE SAME CONCEPT
                # ("Seizure" over "Hypoglycemic seizures"), never to whatever
                # generic term happens to sit in NameRes's top-20. the pool
                # is therefore restricted to candidates whose label is a
                # naming variant of the mention (_is_same_concept_variant)
                # and that carry a real IC. without this guard "Marfan
                # syndrome" (IC 95) was swapped for "urinary system
                # disorder" (IC 45) and an HPV vaccine for albumin. a plain
                # similarity floor is not enough either: "syndrome" scores
                # 1.0 against "marfan syndrome" by substring, yet is exactly
                # the generic fragment that must be rejected.
                label_by_curie = {
                    str(c["curie"]): str(c.get("label") or "")
                    for c in nr_candidates_full
                    if isinstance(c.get("curie"), str)
                }
                swap_pool = [
                    cu for cu in candidate_curies
                    if cu == chosen_curie or (
                        nn_candidates.information_content.get(cu) is not None
                        and _is_same_concept_variant(mention, label_by_curie.get(cu, ""))
                    )
                ]
                sorted_by_ic = sorted(swap_pool, key=_ic_key)
                if sorted_by_ic and sorted_by_ic[0] != chosen_curie:
                    prev_curie = chosen_curie
                    chosen_curie = sorted_by_ic[0]
                    reranked = True
                    logger.info(
                        f"{tag}  ic_rerank  SWAP  "
                        f"{prev_curie} (IC={nn_candidates.information_content.get(prev_curie)}) "
                        f"→ {chosen_curie} (IC={nn_candidates.information_content.get(chosen_curie)})  "
                        f"granularity=general  pool={len(swap_pool)}/{len(candidate_curies)}"
                    )
                else:
                    # log "ran but didn't swap" so the progress stream shows
                    # the IC rerank was considered — silent inaction looks
                    # the same as "stage didn't run" without this line.
                    logger.info(
                        f"{tag}  ic_rerank  no_swap  "
                        f"{chosen_curie} is already lowest-IC "
                        f"(IC={nn_candidates.information_content.get(chosen_curie)})  "
                        f"granularity=general"
                    )
            except NodeNormError as e:
                # don't fail the pipeline over a re-rank optimisation —
                # fall back to the existing chosen_curie.
                logger.warning(f"Stage 5 IC re-rank skipped (NodeNorm error): {e}")

        # ---------- Stage 6: NodeNorm canonicalises the chosen CURIE ----------
        try:
            nn_pinned = nodenorm.normalize([chosen_curie])
        except NodeNormError as e:
            nodenorm_log["pinned"] = {"error": str(e), "input": chosen_curie}
            write_json(qp.nodenorm, nodenorm_log)
            return _finish_failure(qp, ledger, t_start, q["id"],
                                   STATUS_NODENORM_FAILED, str(e))

        canonical_pinned = nn_pinned.canonical.get(chosen_curie)
        pinned_categories = nn_pinned.categories.get(chosen_curie) or []
        pinned_label = nn_pinned.labels.get(chosen_curie) or (
            next((c.get("label") for c in nr_candidates_full if c.get("curie") == chosen_curie), None)
        )

        # surface the re-rank decisions in nameres.json for post-hoc analysis
        # (the artifact already exists on disk; we re-write with the extra fields).
        write_json(qp.nameres, {
            "mention": nr.mention,
            "expected_category": expected_category,
            "biolink_type_filter_applied": nameres_filter_used,
            "granularity_preference": granularity,
            "candidates": nr_candidates_full,
            "nameres_top1_curie": rerank_top1,
            "nameres_top1_label": rerank_top1_label,
            "bm25_top1_curie": bm25_top1,
            "bm25_top1_label": bm25_top1_label,
            "candidate_pick": {
                "chosen_curie": candidate_pick_chosen,
                "reason": candidate_pick_reason,
                "fell_back_to_top1": candidate_pick_fell_back,
            },
            "chosen_curie": chosen_curie,
            "reranked_by_ic": reranked,
            "latency_s": round(nr.latency_s, 3),
        })

        nodenorm_log["pinned"] = {
            "input_curie": chosen_curie,
            "canonical_curie": canonical_pinned,
            "label": pinned_label,
            "categories": pinned_categories,
            "raw": nn_pinned.raw,
        }
        write_json(qp.nodenorm, nodenorm_log)

        if not canonical_pinned:
            return _finish_failure(
                qp, ledger, t_start, q["id"],
                STATUS_NODENORM_FAILED,
                f"NodeNorm could not canonicalise {chosen_curie!r}",
            )

        # ---------- Stage 7: label-vs-mention consistency check ----------
        # last line of defence. by this point we have a resolved pinned entity
        # with a canonical label, but Stage 4 may have fallen back to
        # NameRes top-1 (if it bad-JSON'd or OpenRouter errored), the IC
        # rerank may have over-corrected, etc. before we waste a TRAPI build
        # and an ARAX call on a probably-wrong entity, compare the
        # resolved label against the user's mention via the testable pure
        # function _check_label_consistency (similarity = max of seqmatcher
        # ratio and substring containment).
        # every name the entity is known by: NodeNorm's preferred label,
        # the NameRes candidate's label + synonyms, and the labels of the
        # equivalent identifiers NodeNorm returned for it.
        extra_labels: list[str] = []
        for c in nr_candidates_full:
            if c.get("curie") == chosen_curie:
                extra_labels.append(str(c.get("label") or ""))
                extra_labels.extend(str(s) for s in (c.get("synonyms") or []))
                break
        for eq in (nn_pinned.raw.get(chosen_curie) or {}).get("equivalent_identifiers") or []:
            if isinstance(eq, dict) and eq.get("label"):
                extra_labels.append(str(eq["label"]))
        similarity, best_label, sim_debug = _best_label_match(
            mention, pinned_label or "", extra_labels,
        )
        logger.info(
            f"Stage 7 consistency: mention={sim_debug['mention_normalized']!r} "
            f"vs label={sim_debug['label_normalized']!r}  similarity={similarity:.2f}  "
            f"best_label={best_label!r}  n_labels={sim_debug['n_labels_checked']}  "
            f"(seqmatcher={sim_debug['seqmatcher_ratio']:.2f}, "
            f"substring={sim_debug['substring_match']})"
        )
        if similarity < LOW_CONFIDENCE_THRESHOLD:
            # write the failure-resolution info to nameres.json so the
            # artifact captures why the pipeline stopped here
            write_json(qp.nameres, {
                "mention": nr.mention,
                "expected_category": expected_category,
                "biolink_type_filter_applied": nameres_filter_used,
                "granularity_preference": granularity,
                "candidates": nr_candidates_full,
                "nameres_top1_curie": rerank_top1,
                "nameres_top1_label": rerank_top1_label,
                "bm25_top1_curie": bm25_top1,
                "bm25_top1_label": bm25_top1_label,
                "candidate_pick": {
                    "chosen_curie": candidate_pick_chosen,
                    "reason": candidate_pick_reason,
                    "fell_back_to_top1": candidate_pick_fell_back,
                },
                "chosen_curie": chosen_curie,
                "reranked_by_ic": reranked,
                "latency_s": round(nr.latency_s, 3),
                "consistency_check": {
                    "mention": sim_debug["mention_normalized"],
                    "resolved_label": sim_debug["label_normalized"],
                    "seqmatcher_ratio": round(sim_debug["seqmatcher_ratio"], 3),
                    "substring_match": sim_debug["substring_match"],
                    "best_label": best_label,
                    "similarity": round(similarity, 3),
                    "threshold": LOW_CONFIDENCE_THRESHOLD,
                    "passed": False,
                },
            })
            # surface a "did you mean?" style failure to the user via the
            # standard explanation.md path (same Markdown structure as the
            # OUT_OF_SCOPE refusal so the UI renders the same component).
            # the message names both the user's mention and what we resolved
            # it to, so the user can tell whether to rephrase.
            did_you_mean_md = _format_low_confidence_explanation(
                mention=mention,
                canonical=canonical_pinned,
                label=pinned_label or "(no label)",
                similarity=similarity,
            )
            write_text(qp.explanation, did_you_mean_md)
            short_err = (
                f"Resolved {canonical_pinned!r} ({pinned_label!r}) has low "
                f"similarity ({similarity:.2f} < {LOW_CONFIDENCE_THRESHOLD}) to "
                f"user mention {mention!r}; pipeline refused to query KG against "
                f"a probably-wrong entity."
            )
            return _finish_failure(
                qp, ledger, t_start, q["id"],
                STATUS_LOW_CONFIDENCE_RESOLUTION,
                short_err,
            )
    else:
        # oracle: the gold pinned entity and answer category replace
        # Stages 2-7, so every later stage is measured without
        # entity-resolution errors. the question text still reaches
        # Stages 8, 11 and 15 unchanged.
        canonical_pinned, pinned_label, pinned_categories, answer_category = oracle_entity(q)
        candidate_probes_by_curie = {}
        nodenorm_log["pinned"] = {"canonical_curie": canonical_pinned, "oracle": oracle}
        write_json(qp.nodenorm, nodenorm_log)
        logger.info(
            f"{tag}  oracle={oracle}  pinned={canonical_pinned}  "
            f"answer_category={answer_category}"
        )

    pinned_categories = categories_known_to_kg(pinned_categories, available_categories)
    # needed by Stage 8's prompt AND by Stage 13's graph view, so it is
    # set before the oracle=query branch can skip Stage 8.
    primary_pinned_cat = pinned_categories[0] if pinned_categories else None
    _ui_event(
        logger, tag, "entity",
        curie=canonical_pinned, label=pinned_label, category=primary_pinned_cat,
        answer_category=answer_category,
    )

    if oracle != "query":
        # ---------- Stage 8: LLM builds TRAPI query graph ----------
        # the predicate list is the meta_KG slice for the
        # (pinned_category, answer_category) pair. we try both directions
        # because Stage 8 may put the pinned entity as either subject or
        # object — the LLM picks the direction along with the predicate.
        # if the predicate_index is empty (meta_KG fetch failed at boot)
        # or we don't know the answer_category, fall back to the old
        # unconstrained prompt so the pipeline still runs.
        valid_predicates_forward: list[str] = []
        valid_predicates_reverse: list[str] = []
        if predicate_index and primary_pinned_cat and answer_category:
            valid_predicates_forward = predicate_index.get((primary_pinned_cat, answer_category), [])
            valid_predicates_reverse = predicate_index.get((answer_category, primary_pinned_cat), [])

        # CURIE-specific predicate-density data: grounds the LLM in the
        # ACTUAL edge distribution for this pinned entity, not just the
        # schema-valid predicates. REUSED from the candidate-density
        # sweep that ran before Stage 4 — same CURIE, same answer category,
        # same predicate distribution — so we look up the chosen CURIE's
        # probe in `candidate_probes_by_curie` instead of firing another
        # Retriever call here.
        #
        # falls back to a fresh probe IF the candidate-probe sweep was
        # skipped (e.g. no answer_category resolved at Stage 4) but the
        # chosen CURIE we ended up with does have one — rare but possible.
        probe_dict: dict[str, Any] | None = None
        if primary_pinned_cat and answer_category:
            cached = candidate_probes_by_curie.get(canonical_pinned)
            # Stage 4 may have picked a CURIE whose canonicalised form
            # (post-NodeNorm) differs from the raw NameRes CURIE we probed.
            # if the canonical isn't in the cache, fall back to a fresh probe.
            if cached is not None and cached.get("error") is None:
                probe_dict = cached
            else:
                try:
                    fresh = retriever.probe_predicates(
                        canonical_pinned, primary_pinned_cat, answer_category,
                    )
                    probe_dict = {
                        "pinned_curie": fresh.pinned_curie,
                        "pinned_cat": fresh.pinned_cat,
                        "answer_cat": fresh.answer_cat,
                        "total_edges": fresh.total_edges,
                        "by_predicate": fresh.by_predicate,
                        "qualified_by_predicate": fresh.qualified_by_predicate,
                        "latency_s": fresh.latency_s,
                        "error": fresh.error,
                    }
                except RetrieverError as e:
                    logger.warning(f"Stage 8 fallback predicate probe failed: {e}")
                    probe_dict = None

        # persist the chosen-CURIE probe to its own file for backward
        # compatibility (Stage 8 UI row, intermediates.predicate_probe).
        if probe_dict is not None:
            write_json(qp.predicate_probe, probe_dict)

        predicate_block: str
        if probe_dict is not None and probe_dict.get("total_edges", 0) > 0:
            # probe found edges — show the LLM the actual distribution.
            # sort by descending count so the dense predicates lead the list.
            lines = [
                f"Predicates POPULATED for THIS pinned CURIE ({canonical_pinned} "
                f"{pinned_label!r}) against {answer_category} entities, with "
                f"actual Tier 0 fact counts:"
            ]
            sorted_preds = sorted(
                probe_dict["by_predicate"].items(),
                key=lambda kv: kv[1]["count"],
                reverse=True,
            )
            for pred, stats in sorted_preds:
                # describe dominant direction. "forward" = pinned is SUBJECT
                # of the stored fact, so subject={pinned_cat} object={answer_cat} in
                # the TRAPI query graph. "reverse" = pinned is OBJECT.
                fwd, rev = stats["forward"], stats["reverse"]
                if fwd > 0 and rev == 0:
                    direction = f"subject={primary_pinned_cat} object={answer_category}"
                elif rev > 0 and fwd == 0:
                    direction = f"subject={answer_category} object={primary_pinned_cat}"
                elif fwd > 0 and rev > 0:
                    direction = (
                        f"mostly subject={primary_pinned_cat} object={answer_category}"
                        if fwd >= rev
                        else f"mostly subject={answer_category} object={primary_pinned_cat}"
                    )
                else:
                    # neither endpoint matched pinned_curie exactly — the
                    # graph expanded the pinned to descendants, all facts
                    # touch descendant CURIEs. we can't infer direction
                    # cleanly, so just label it.
                    direction = "descendant-only (orient either way)"
                lines.append(f"  - {pred}: {stats['count']} facts ({direction})")
                # probes cached before qualifiers were tallied have no such
                # key; .get keeps an old candidate_probes.json readable.
                lines.extend(_render_qualified_lines(
                    probe_dict.get("qualified_by_predicate", {}).get(pred, {}),
                    limit=MAX_QUALIFIER_SETS_SHOWN,
                ))
            lines.append("")
            lines.append(
                "Prefer the HIGHEST-COUNT predicate above. Orient your TRAPI "
                "edge (subject/object) to match its dominant direction. The "
                "counts above are the ACTUAL number of Tier 0 facts for this "
                "pinned CURIE — predicates not listed have ZERO facts from "
                "this CURIE to the answer category and will return no results."
            )
            predicate_block = "\n".join(lines) + "\n\n"
        elif valid_predicates_forward or valid_predicates_reverse:
            # probe returned 0 edges (or didn't run) — fall back to the
            # schema-valid list. warn the LLM that these may be sparse for
            # this specific CURIE.
            lines = ["Valid Biolink predicates in the knowledge graph:"]
            if valid_predicates_forward:
                lines.append(
                    f"  - if subject={primary_pinned_cat} and object={answer_category}: "
                    f"{valid_predicates_forward}"
                )
            if valid_predicates_reverse:
                lines.append(
                    f"  - if subject={answer_category} and object={primary_pinned_cat}: "
                    f"{valid_predicates_reverse}"
                )
            lines.append(
                "You MUST pick one predicate from the appropriate list above, "
                "and orient the edge to match (subject/object)."
            )
            if probe_dict is not None and probe_dict.get("total_edges", 0) == 0:
                lines.append(
                    "WARNING: a per-CURIE probe found ZERO edges from "
                    f"{canonical_pinned} to any {answer_category} entity in "
                    "either direction. The predicates above are schema-valid "
                    "but may yield 0 results for this specific question."
                )
            predicate_block = "\n".join(lines) + "\n\n"
        else:
            predicate_block = ""

        user_msg_1 = (
            f"User question: {nl_question}\n\n"
            f"Resolved pinned entity (NameRes top-1 -> NodeNorm canonical):\n"
            f"  CURIE: {canonical_pinned}\n"
            f"  Label: {pinned_label}\n"
            f"  Biolink categories: {pinned_categories}\n\n"
            f"Intended answer-node Biolink category: {answer_category or '(unspecified)'}\n\n"
            f"{predicate_block}"
            f"{prompts.ARAX_TRAPI_NOTE + chr(10) if reasoner == 'arax' else ''}"
            f"Build the one-hop TRAPI query graph."
        )
        prompt_log["stage_8_trapi_build"] = {
            "system": prompts.SYS_TRAPI_BUILD,
            "user": user_msg_1,
        }
        write_json(qp.prompt, prompt_log)

        try:
            rep1 = llm.chat(
                model=stage_model("trapi_build"),
                system=prompts.SYS_TRAPI_BUILD,
                user=user_msg_1,
                stage="trapi_build",
            )
        except OpenRouterError as e:
            return _finish_failure(qp, ledger, t_start, q["id"], STATUS_LLM_ERROR, str(e))
        prompt_log["stage_8_trapi_build"]["response"] = _llm_response_meta(rep1)
        write_json(qp.prompt, prompt_log)
        ledger.add(
            "trapi_build", stage_model("trapi_build").id, stage_model("trapi_build").slug,
            rep1.input_tokens, rep1.output_tokens,
            rep1.cost.input_usd, rep1.cost.output_usd, rep1.cost.total_usd,
            rep1.latency_s,
        )

        try:
            trapi_msg = _extract_json(rep1.content)
        except json.JSONDecodeError:
            write_text(qp.explanation, rep1.content)
            if _llm_response_meta(rep1).get("finish_reason") == "length":
                return _finish_failure(
                    qp, ledger, t_start, q["id"],
                    STATUS_LLM_TRUNCATED,
                    "Stage 8 reply hit the max_tokens cap (finish_reason=length)",
                )
            return _finish_failure(
                qp, ledger, t_start, q["id"],
                STATUS_LLM_BAD_JSON, "Stage 8 output was not valid JSON",
            )

        write_json(qp.trapi_query, trapi_msg)

        decline_reason = stage8_decline(trapi_msg)
        if decline_reason is not None:
            logger.info(f"{tag}  trapi_build  declined: {decline_reason!r}")
            write_text(qp.explanation, _format_query_declined_explanation(decline_reason))
            _flush_meta_and_cost(
                qp, ledger, t_start, q["id"],
                STATUS_QUERY_DECLINED, decline_reason,
                outcome=OUTCOME_QUERY_DECLINED,
                outcome_reason=decline_reason,
                n_results=-1, answers_n_picked=-1,
            )
            return QuestionResult(
                q_id=q["id"],
                status=STATUS_QUERY_DECLINED,
                cost_total_usd=ledger.total_usd(),
                cost_total_tokens=ledger.total_tokens(),
                elapsed_s=round(time.perf_counter() - t_start, 3),
                error=None,
                outcome=OUTCOME_QUERY_DECLINED,
                outcome_reason=decline_reason,
            )
    else:
        # oracle: the gold query graph replaces Stage 8, so Stages
        # 10-15 are measured on the question's correct query.
        trapi_msg = oracle_query_graph(q)
        write_json(qp.trapi_query, trapi_msg)

    # ARAX reasons only when asked in the form it needs; for the two
    # shapes it can infer, the request is put in that form (see
    # arax_reasoning.ask_arax_to_reason). trapi_query.json keeps the
    # query Stage 8 wrote; reasoner_request.json holds what was sent.
    if reasoner == "arax":
        trapi_msg, reasoning_changes = ask_arax_to_reason(trapi_msg)
        if reasoning_changes:
            logger.info(f"{tag}  ask_arax_to_reason  {'; '.join(reasoning_changes)}")

    # ---------- Stage 9: validate ----------
    val = validate_query(trapi_msg, logger=logger)
    write_json(qp.validation, {
        "passed": val.passed,
        "errors": val.errors,
        "warnings": val.warnings,
        "info": val.info,
        "raw": val.raw,
    })
    if not val.passed:
        # policy: invalid -> stop. there is no repair loop.
        n_err = len(val.errors) if isinstance(val.errors, dict) else 0
        return _finish_failure(
            qp, ledger, t_start, q["id"],
            STATUS_INVALID_QUERY,
            f"reasoner-validator rejected with {n_err} error groups",
        )

    # ---------- Stage 10: send to ARAX (or Retriever, lookup) ----------
    # reasoner_request.json / reasoner_response.json hold the request
    # sent to, and the reply from, whichever service answered (run.json
    # records which).
    write_json(qp.reasoner_request, trapi_msg)
    _ui_event(
        logger, tag, "query",
        reasoner=reasoner, query_graph=(trapi_msg.get("message") or {}).get("query_graph"),
    )
    prep: RetrieverReply | AraxReply
    if reasoner == "arax":
        if arax is None:
            raise ValueError("reasoner='arax' needs an AraxClient")
        try:
            prep = arax.query(trapi_msg, on_progress=_arax_progress_relay(logger, tag))
        except AraxError as e:
            return _finish_failure(qp, ledger, t_start, q["id"], STATUS_ARAX_ERROR, str(e))
    else:
        try:
            prep = retriever.query(trapi_msg)
        except RetrieverError as e:
            return _finish_failure(qp, ledger, t_start, q["id"], STATUS_LOOKUP_ERROR, str(e))
    write_json(qp.reasoner_response, prep.body)

    # outcome diagnostic (used at the end of the run): how many TRAPI
    # results came back? zero means the graph had nothing for the
    # constructed query — usually a sign the resolved pinned CURIE
    # was wrong, or the predicate doesn't fit, or the question is
    # genuinely outside the graph. we record the number on every run so
    # the offline analysis can group runs by it.
    n_results = len(prep.body.get("message", {}).get("results") or [])
    _ui_event(logger, tag, "results", reasoner=reasoner, n_results=n_results)

    # ---------- Stage 11 pre-step: response reduction ----------
    # the raw body used to reach Stages 11 and 15 as
    # json.dumps(...)[:200_000] — an ARBITRARY prefix, so on a broad
    # question (one pilot question returned 286,544 results) the model
    # judged evidence it never saw. reduce_response ranks every edge by
    # (knowledge_level, agent_type, publication count, edge_id) and
    # keeps the top cfg.reduction.top_k_edges, uniformly on every run so
    # the reduction is a fixed part of the method. the full response
    # stays on disk and Stages 13/14 keep reading it.
    #
    # ARAX mode reads ARAX's ranked answers instead: the top
    # cfg.arax.top_k_answers, each with its score, evidence count and
    # shortest reasoning paths (see arax_reasoning.render_answers_table).
    if reasoner == "arax":
        all_arax_answers = answers_from_response(prep.body)
        arax_answers = all_arax_answers[: cfg.arax.top_k_answers]
        table = render_answers_table(
            prep.body, arax_answers, canonical_pinned, paths_per_answer=2,
        )
        write_json(qp.reduced_data, {
            "reasoner": "arax",
            "top_k_answers": cfg.arax.top_k_answers,
            "total_answers": len(all_arax_answers),
            "kept": len(arax_answers),
            "rows": [
                {
                    "rank": answer.rank, "curie": answer.curie, "name": answer.name,
                    "score": answer.score,
                    "evidence_edges": len(evidence_edges(prep.body, answer)),
                }
                for answer in arax_answers
            ],
        })
        system_pick, system_explain = prompts.SYS_ANSWER_PICK_ARAX, prompts.SYS_EXPLAIN_ARAX
        logger.info(
            f"{tag}  arax answers  kept={len(arax_answers)}/{len(all_arax_answers)}  "
            f"est_tokens={len(table) // 4}"
        )
    else:
        reduced = reduce_response(prep.body, top_k=cfg.reduction.top_k_edges)
        write_json(qp.reduced_data, to_json(reduced))
        table = render_table(reduced)
        system_pick, system_explain = prompts.SYS_ANSWER_PICK, prompts.SYS_EXPLAIN
        logger.info(
            f"{tag}  reduction  kept={reduced.kept}/{reduced.total_edges} edges  "
            f"top_k={cfg.reduction.top_k_edges}  est_tokens={len(table) // 4}"
        )

    # ---------- Stage 11: LLM picks answer ----------
    user_msg_4 = (
        f"User question: {nl_question}\n\n"
        f"{table}\n"
    )
    prompt_log["stage_11_answer_pick"] = {
        "system": system_pick,
        "user_truncated": user_msg_4[:2000],
    }
    write_json(qp.prompt, prompt_log)

    try:
        rep4 = llm.chat(
            model=stage_model("answer_pick"),
            system=system_pick,
            user=user_msg_4,
            stage="answer_pick",
        )
    except OpenRouterError as e:
        return _finish_failure(qp, ledger, t_start, q["id"], STATUS_LLM_ERROR, str(e))
    prompt_log["stage_11_answer_pick"]["response"] = _llm_response_meta(rep4)
    write_json(qp.prompt, prompt_log)
    ledger.add(
        "answer_pick", stage_model("answer_pick").id, stage_model("answer_pick").slug,
        rep4.input_tokens, rep4.output_tokens,
        rep4.cost.input_usd, rep4.cost.output_usd, rep4.cost.total_usd,
        rep4.latency_s,
    )

    try:
        answer_obj = _extract_json(rep4.content, allow_list=True)
    except json.JSONDecodeError:
        if _llm_response_meta(rep4).get("finish_reason") == "length":
            return _finish_failure(
                qp, ledger, t_start, q["id"],
                STATUS_LLM_TRUNCATED,
                "Stage 11 reply hit the max_tokens cap (finish_reason=length)",
            )
        return _finish_failure(
            qp, ledger, t_start, q["id"],
            STATUS_LLM_BAD_JSON, "Stage 11 output was not valid JSON",
        )

    # ---------- Stage 12: NodeNorm canonicalises every answer CURIE ----------
    answer_obj["answers"] = _normalise_answers(answer_obj.get("answers"))
    raw_answer_curies: list[str] = [str(a["curie"]) for a in answer_obj["answers"]]
    # surface "picked N of M available" so the progress stream shows
    # whether the LLM ignored most candidates (low pick rate may
    # indicate confused prompting) or chose them all (high recall but
    # possibly low precision). this is the single most useful line for
    # debugging Stage 11 calibration.
    available_count = len(
        prep.body.get("message", {}).get("knowledge_graph", {}).get("nodes", {}) or {}
    )
    logger.info(
        f"{tag}  answer_pick  picked={len(raw_answer_curies)}/{available_count}  "
        f"evidence_tier={answer_obj.get('evidence_tier', '(none)')!r}"
    )
    if raw_answer_curies:
        try:
            nn_ans = nodenorm.normalize(raw_answer_curies)
        except NodeNormError as e:
            # we don't fail the whole run for an answer-side NodeNorm
            # error: the LLM picked something, the explanation can
            # still be written. we record the failure and continue.
            nodenorm_log["answers"] = {"error": str(e), "input": raw_answer_curies}
            answer_obj["canonical_curies"] = list(raw_answer_curies)
        else:
            nodenorm_log["answers"] = {
                "canonical": nn_ans.canonical,
                "categories": nn_ans.categories,
                "labels": nn_ans.labels,
                "raw": nn_ans.raw,
            }
            # promote canonical CURIEs into answer.json so downstream
            # comparison reads canonical-vs-canonical.
            answer_obj["canonical_curies"] = [
                nn_ans.canonical.get(c) or c for c in raw_answer_curies
            ]
        write_json(qp.nodenorm, nodenorm_log)
    else:
        nodenorm_log["answers"] = {"canonical": {}, "note": "no answer CURIEs to normalise"}
        write_json(qp.nodenorm, nodenorm_log)
        answer_obj["canonical_curies"] = []

    write_json(qp.answer, answer_obj)
    _ui_event(
        logger, tag, "answers",
        answers=[
            {"curie": a.get("curie"), "label": a.get("label")}
            for a in answer_obj.get("answers") or []
        ],
    )

    if reasoner == "lookup":
        # ---------- Stage 13: build research-grade graph view ----------
        # reshape (pinned + canonical answers + Retriever knowledge_graph)
        # into a node-link graph view with per-edge provenance suitable for
        # rendering as a hoverable graph card in the UI. pure function,
        # unit-tested in tests/test_answer_graph_view.py.
        # join on the RAW picked CURIEs (the ids as they appear in the
        # Retriever response), not the NodeNorm canonical ids: canonicalising
        # can rename a node (CHEBI:3897 cortisone acetate -> CHEBI:16962)
        # to an id the KG response never contains, which loses the label,
        # the predicate, and every supporting edge in the graph view. the
        # canonical ids stay available in answer.json's canonical_curies.
        answer_graph_view = _build_answer_graph_view(
            pinned_curie=canonical_pinned,
            pinned_label=pinned_label,
            pinned_category=primary_pinned_cat,
            picked_answer_curies=raw_answer_curies,
            kg_response=prep.body,
        )

        # ---------- Stage 14: PubTator co-mention verification ----------
        # for each edge with supporting_publications, ask PubTator whether
        # the cited PMIDs actually mention BOTH endpoints (via any of their
        # equivalent CURIEs). this converts "the KG says this is supported"
        # into "this is supported AND independently verifiable by NLM NER".
        # graceful degradation: if pubtator client is None or errors,
        # edges get pubtator_verified=None and the pipeline carries on.
        pubtator_summary: dict[str, Any] = {"called": False, "reason": "client_not_provided"}
        if pubtator is not None and answer_graph_view["edges"]:
            # text-mined facts (SemMedDB) often have 30+ supporting PMIDs each.
            # 5 edges x 30 PMIDs = ~150 PMIDs in one PubTator batch, and on
            # slow PubTator days that adds 30-60s of latency to every user
            # query. cap PMIDs per edge to the top-N (most-cited research
            # is captured in the first few PMIDs anyway), AND cap the
            # cross-edge total. eval / benchmark runs can do exhaustive
            # verification offline if needed.
            MAX_PMIDS_PER_EDGE = 8
            MAX_PMIDS_TOTAL = 80
            all_pmids: list[str] = []
            seen: set[str] = set()
            for edge in answer_graph_view["edges"]:
                per_edge = (edge.get("supporting_publications") or [])[:MAX_PMIDS_PER_EDGE]
                for p in per_edge:
                    if p not in seen:
                        seen.add(p)
                        all_pmids.append(p)
                    if len(all_pmids) >= MAX_PMIDS_TOTAL:
                        break
                if len(all_pmids) >= MAX_PMIDS_TOTAL:
                    break
            # collect endpoint CURIEs (pinned + each answer) for one batched
            # NodeNorm call to fetch equivalents. canonical_pinned + the
            # picked answer CURIEs cover every endpoint of every edge.
            canonical_answer_curies: list[str] = answer_obj.get("canonical_curies") or []
            endpoint_curies = list({canonical_pinned, *canonical_answer_curies})
            equivalent_curies: dict[str, list[str]] = {}
            if endpoint_curies:
                try:
                    nn_eq = nodenorm.normalize(endpoint_curies)
                    equivalent_curies = nn_eq.equivalent_identifiers
                except NodeNormError as e:
                    # if NodeNorm fails here we still call PubTator with
                    # canonical-only matching (degrades gracefully).
                    logger.warning(
                        f"{tag}  pubtator  NodeNorm equivalents lookup failed ({e}); "
                        f"using canonical CURIEs only for matching"
                    )
            if all_pmids:
                try:
                    # all_pmids is already a deduplicated list, capped at
                    # MAX_PMIDS_TOTAL — see the per-edge / total cap above.
                    pt = pubtator.fetch_annotations(all_pmids)
                    answer_graph_view["edges"] = _enrich_edges_with_pubtator(
                        edges=answer_graph_view["edges"],
                        equivalent_curies=equivalent_curies,
                        pubtator_annotations=pt.annotations,
                    )
                    pubtator_summary = {
                        "called": True,
                        "pmids_requested": len(all_pmids),
                        "pmids_annotated": len(pt.annotations),
                        "pmids_missing": len(pt.missing_pmids),
                        "latency_s": round(pt.latency_s, 3),
                    }
                except PubTatorError as e:
                    logger.warning(
                        f"{tag}  pubtator  fetch failed ({e}); "
                        f"edges remain unverified"
                    )
                    pubtator_summary = {"called": True, "error": str(e)}
                    # still call enrichment with empty annotations so each
                    # edge gets a consistent pubtator_verified block (with
                    # all PMIDs in missing_pmids and verified=False) —
                    # downstream code expects the key to exist.
                    answer_graph_view["edges"] = _enrich_edges_with_pubtator(
                        edges=answer_graph_view["edges"],
                        equivalent_curies=equivalent_curies,
                        pubtator_annotations={},
                    )

        # compute the eval-level verified-edge-rate metric over the (now
        # enriched) edges. emitted alongside the graph view for the API
        # response + later benchmark aggregation.
        answer_graph_view["pubtator_metrics"] = _pubtator_verified_edge_rate(answer_graph_view)
        answer_graph_view["pubtator_call_summary"] = pubtator_summary

        write_json(qp.answer_graph_view, answer_graph_view)
        _ui_event(logger, tag, "graph", kind="answer_view", graph=answer_graph_view)
        logger.info(
            f"{tag}  answer_graph_view  nodes={1 + len(answer_graph_view['answer_nodes'])}  "
            f"edges={len(answer_graph_view['edges'])}  "
            f"pubtator_rate={answer_graph_view['pubtator_metrics']['rate']}"
        )
    else:
        # ARAX mode: the graph the UI draws is the reasoning behind the
        # picked answers: for each, up to cfg.arax.paths_per_answer paths
        # from the answer to the question's entity through ARAX's
        # evidence, every fact numbered F1..Fn for the explanation to cite,
        # each with the evidence it rests on (Stage 14: approvals, trials,
        # drug labels, PMIDs, co-mention; arax_reasoning.fact_evidence).
        # PubTator is not run on these facts (their PMIDs are shown as
        # they came).
        picked = set(raw_answer_curies)
        chosen = [answer for answer in arax_answers if answer.curie in picked]
        arax_graph = number_facts(reasoning_graph(
            prep.body, chosen, canonical_pinned,
            paths_per_answer=cfg.arax.paths_per_answer,
            target_label=pinned_label, target_category=primary_pinned_cat,
        ))
        write_json(qp.reasoning_graph, arax_graph)
        _ui_event(logger, tag, "graph", kind="reasoning", graph=arax_graph)
        logger.info(
            f"{tag}  reasoning_graph  answers={len(chosen)}  "
            f"nodes={len(arax_graph['nodes'])}  facts={len(arax_graph['edges'])}"
        )

    # ---------- compute end-to-end outcome ----------
    # at this point we know what came out of Stages 10 and 11. status
    # is still "ok" (we'd have returned earlier on any runtime error),
    # so this is purely about whether the model produced a useful
    # answer for the user. three cases:
    #   - the reasoner had nothing to work with                 → no_results
    #   - it had something, but the LLM picked nothing          → no_answer_picked
    #   - LLM picked at least one answer                        → answered
    answers_n_picked = len(answer_obj.get("answers") or [])
    reasoner_name = "ARAX" if reasoner == "arax" else "Retriever"
    if n_results == 0:
        outcome = OUTCOME_NO_RESULTS
        outcome_reason = (
            f"{reasoner_name} returned 0 results for the constructed query "
            f"(pinned CURIE: {canonical_pinned}). Possible cause: NameRes "
            f"top-1 picked a non-canonical or wrong identifier, the "
            f"predicate doesn't match what the graph stores, or the question "
            f"is outside the Tier 0 graph's coverage."
        )
    elif answers_n_picked == 0:
        # use the LLM's own rationale if it gave one — that's the
        # most informative description of what the model decided.
        rationale = str(answer_obj.get("rationale") or "")[:300]
        outcome = OUTCOME_NO_ANSWER_PICKED
        outcome_reason = (
            f"{reasoner_name} returned {n_results} results, but the LLM "
            f"selected zero answers. Rationale (from the model): "
            f"{rationale!r}"
        )
    else:
        outcome = OUTCOME_ANSWERED
        outcome_reason = None

    # ---------- Stage 15: explanation ----------
    # ARAX mode explains the drawn reasoning paths, citing their facts
    # as [F<n>]; the lookup condition explains from the evidence table.
    evidence_text = render_facts_block(arax_graph) if reasoner == "arax" else table
    user_msg_5 = (
        f"User question: {nl_question}\n\n"
        f"Selected answers (Stage 11):\n{json.dumps(answer_obj, ensure_ascii=False)}\n\n"
        f"{evidence_text}\n"
    )
    prompt_log["stage_15_explain"] = {
        "system": system_explain,
        "user_truncated": user_msg_5[:2000],
    }
    write_json(qp.prompt, prompt_log)

    try:
        rep5 = llm.chat(
            model=stage_model("explain"),
            system=system_explain,
            user=user_msg_5,
            stage="explain",
        )
    except OpenRouterError as e:
        # we still have a valid answer-pick; the run completes but
        # the explanation step failed. flag it as llm_error but
        # preserve the outcome we already know from Stages 10/11.
        write_text(qp.explanation, "")
        _flush_meta_and_cost(
            qp, ledger, t_start, q["id"],
            STATUS_LLM_ERROR, str(e),
            outcome=outcome, outcome_reason=outcome_reason,
            n_results=n_results, answers_n_picked=answers_n_picked,
        )
        return QuestionResult(
            q_id=q["id"],
            status=STATUS_LLM_ERROR,
            cost_total_usd=ledger.total_usd(),
            cost_total_tokens=ledger.total_tokens(),
            elapsed_s=time.perf_counter() - t_start,
            error=str(e),
            outcome=outcome,
            outcome_reason=outcome_reason,
            n_results=n_results,
            answers_n_picked=answers_n_picked,
        )
    prompt_log["stage_15_explain"]["response"] = _llm_response_meta(rep5)
    write_json(qp.prompt, prompt_log)
    ledger.add(
        "explain", stage_model("explain").id, stage_model("explain").slug,
        rep5.input_tokens, rep5.output_tokens,
        rep5.cost.input_usd, rep5.cost.output_usd, rep5.cost.total_usd,
        rep5.latency_s,
    )
    # the explainer now emits structured Markdown (## Answer, ## Evidence,
    # ## Confidence, ## Limitations). save it verbatim — no whitespace
    # reformatting, because that would mangle list items and headings.
    write_text(qp.explanation, rep5.content.strip() + "\n")

    return _finish_success(
        qp, ledger, t_start, q["id"],
        outcome=outcome, outcome_reason=outcome_reason,
        n_results=n_results, answers_n_picked=answers_n_picked,
    )


# ---------- internal helpers ----------

def _finish_success(
    qp: QuestionPaths, ledger: CostLedger, t_start: float, q_id: str,
    *,
    outcome: str | None = None,
    outcome_reason: str | None = None,
    n_results: int = -1,
    answers_n_picked: int = -1,
) -> QuestionResult:
    _flush_meta_and_cost(
        qp, ledger, t_start, q_id, STATUS_OK, None,
        outcome=outcome, outcome_reason=outcome_reason,
        n_results=n_results, answers_n_picked=answers_n_picked,
    )
    return QuestionResult(
        q_id=q_id,
        status=STATUS_OK,
        cost_total_usd=ledger.total_usd(),
        cost_total_tokens=ledger.total_tokens(),
        elapsed_s=time.perf_counter() - t_start,
        error=None,
        outcome=outcome,
        outcome_reason=outcome_reason,
        n_results=n_results,
        answers_n_picked=answers_n_picked,
    )


def _finish_failure(
    qp: QuestionPaths, ledger: CostLedger, t_start: float,
    q_id: str, status: str, err: str,
) -> QuestionResult:
    # for early-exit failures (the run never reached Stage 11), outcome
    # is left None — there's no semantic result to evaluate.
    _flush_meta_and_cost(qp, ledger, t_start, q_id, status, err)
    return QuestionResult(
        q_id=q_id,
        status=status,
        cost_total_usd=ledger.total_usd(),
        cost_total_tokens=ledger.total_tokens(),
        elapsed_s=time.perf_counter() - t_start,
        error=err,
    )


def _flush_meta_and_cost(
    qp: QuestionPaths, ledger: CostLedger, t_start: float,
    q_id: str, status: str, err: str | None,
    *,
    outcome: str | None = None,
    outcome_reason: str | None = None,
    n_results: int = -1,
    answers_n_picked: int = -1,
) -> None:
    # cost.json gets a per-stage list and a summary. meta.json is the
    # one-line summary the analysis script reads to count failures.
    # outcome/outcome_reason describe whether the model actually
    # answered (separate axis from `status`, which is about runtime
    # errors). counts are -1 when the corresponding stage didn't run.
    ti, to = ledger.total_tokens()
    # meta.json first: it is the record the analysis keys on, so a
    # crash between the two writes must never leave cost.json orphaned.
    write_json(qp.meta, {
        "q_id": q_id,
        "status": status,
        "outcome": outcome,
        "outcome_reason": outcome_reason,
        "n_results": n_results,
        "answers_n_picked": answers_n_picked,
        "error": err,
        "elapsed_s": round(time.perf_counter() - t_start, 3),
    })
    write_json(qp.cost, {
        "stages": ledger.entries,
        "totals": {
            "input_tokens": ti,
            "output_tokens": to,
            "total_usd": ledger.total_usd(),
        },
    })
