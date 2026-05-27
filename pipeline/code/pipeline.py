# pipeline.py — per-question orchestrator. v15 grounded flow:
#
#   [2]   LLM extracts the focal entity mention from the NL
#   [3]   NameRes /lookup → top-1 CURIE by rank
#   [6]   NodeNorm /get_normalized_nodes → canonical CURIE + Biolink categories
#   [8]   LLM builds TRAPI query graph (NL + canonical pinned)
#   [9]   reasoner-validator on the query graph
#   [10]  POST to PloverDB /query
#   [11]  LLM picks answer CURIEs from the response
#   [12]  NodeNorm canonicalises every answer CURIE
#   [15]  LLM writes NL explanation citing returned edges
#
# this file is intentionally linear: at each stage we either succeed
# and continue or we fail with a named status that the v15 metrics
# will count separately. it knows nothing about the CLI, progress
# bars, or run-level folder layout — that is runner.py's job.

from __future__ import annotations

# json: stdlib. parses LLM JSON in stages 1 and 4; serialises slices
# of the PloverDB response into stage-11/15 user messages.
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
from dataclasses import asdict, dataclass

# difflib.SequenceMatcher: stdlib. used by Stage 7 to compare the
# user's entity mention against the resolved canonical label. cheap
# Levenshtein-style ratio (0..1) — flags cases where NameRes+candidate-
# pick still grounded to a label that's nothing like what the user typed.
from difflib import SequenceMatcher

# typing.Any: stdlib. gold question records and TRAPI messages are
# nested dicts whose shape lives in their own spec/JSON files.
from typing import Any

# Config / ModelSpec come from our config module.
from .config import Config, ModelSpec

# OpenRouter and PloverDB clients are constructed once per run by the
# runner and passed in here.
from .openrouter_client import OpenRouterClient, OpenRouterError
from .plover_client import PloverClient, PloverError

# RENCI clients — Stage 3 / Stage 6 / Stage 12.
from .nameres_client import NameResClient, NameResError
# BMT-derived loose-neighborhood expansion for the Stage 3 biolink_type
# filter. see biolink_helper.py for the full story; tl;dr: pick a
# category and we get back the set of biolink:CategoryName strings
# NameRes should ALL accept (e.g. Pathway → {Pathway, BiologicalProcess,
# PhysiologicalProcess, ...}) so we don't filter out correct answers
# that happen to be typed under the parent in KG2c.
from .biolink_helper import loose_filter_for
from .nodenorm_client import NodeNormClient, NodeNormError

# pubtator: Stage 13 enrichment. independently re-verifies each edge's
# supporting PMIDs by checking whether PubTator's NER co-mentions BOTH
# endpoints. graceful degradation: if the client is None or errors,
# edges get pubtator_verified=None and the pipeline carries on.
from .pubtator_client import PubTatorClient, PubTatorError

# reduction: Strategy B reduces the PloverDB body to top-N results per
# predicate before Stage 11 sees it, ranked by knowledge_level then
# agent_type. full spec: docs/specs/response-reduction-strategy-b.md.
from .reduction import reduce_plover_response

# prompts: SYS_ENTITY_EXTRACT, SYS_TRAPI_BUILD, SYS_ANSWER_PICK,
# SYS_EXPLAIN. flat strings re-exported by name so diffs for tuning
# are small and obvious.
from . import prompts

# trace: the on-disk artifact layout. CostLedger accumulates per-stage
# cost; QuestionPaths owns the file paths inside one question's folder.
from .trace import CostLedger, QuestionPaths, write_json, write_text

# trapi_validator: thin wrapper over reasoner-validator. v15 policy:
# invalid query -> stop, no repair loop.
from .trapi_validator import validate_query


# ---------- terminal statuses (written into meta.json) ----------
# `status` describes the RUNTIME of the pipeline: did each stage
# complete without an error? these strings end up in meta.json and are
# what the v15 analysis script groups by when computing valid_trapi_rate,
# plover_executed_rate, etc. keep them stable across runs.
STATUS_OK = "ok"
STATUS_INVALID_QUERY = "invalid_query"     # validator rejected the LLM's TRAPI
STATUS_LLM_BAD_JSON = "llm_bad_json"       # LLM returned non-JSON in a JSON-only stage
STATUS_PLOVER_ERROR = "plover_error"       # PloverDB returned non-200 / network error
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


# ---------- outcomes (written into meta.json alongside status) ----------
# `outcome` describes the SEMANTIC result of a successful run:
# even when status=ok, the model may not have actually answered.
# this lets us count "the pipeline ran but produced no useful answer"
# separately from "the pipeline crashed."
OUTCOME_ANSWERED = "answered"                 # LLM picked at least one answer entity
OUTCOME_NO_RESULTS = "no_results"             # PloverDB returned zero results
OUTCOME_NO_ANSWER_PICKED = "no_answer_picked" # results came back but the LLM picked answers=[]
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
    plover_n_results: int = -1         # -1 = stage didn't run; 0+ = actual count
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
    plover_response: dict[str, Any],
) -> dict[str, Any]:
    # Stage 13: reshape (pinned entity + picked answers + PloverDB KG)
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
        plover_response.get("message", {})
                       .get("knowledge_graph", {})
                       .get("nodes", {})
        or {}
    )
    edges_block: dict[str, Any] = (
        plover_response.get("message", {})
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
    # exactly as PloverDB had them.
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
# only PROBE / SHOW the top NAMERES_DISPLAY of that re-ranked list to
# Stage 4. probing all 20 would be slow (~10 PloverDB calls / question);
# probing top-10 is the sweet spot between recall and per-question
# latency.
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
    plover: PloverClient,
    logger: logging.Logger,
    tag: str,
) -> dict[str, Any]:
    # per-candidate edge-density probe against the answer category.
    # called twice in the strict-first / loose-fallback flow — once
    # against the strict candidate set, optionally again against the
    # loose set if strict had no coverage. each probe is one TRAPI call
    # to PloverDB; failures degrade to {total_edges:0, error:str} so
    # the caller can still tell "we tried" from "we didn't try".
    probes: dict[str, Any] = {}
    if not (answer_category and candidates):
        return probes
    for c in candidates[:NAMERES_DISPLAY]:
        c_curie = c.get("curie")
        if not isinstance(c_curie, str) or not c_curie:
            continue
        try:
            probe = plover.probe_predicates(
                c_curie, primary_mention_cat, answer_category,
            )
        except PloverError as e:
            logger.warning(f"{tag}  probe failed for {c_curie}: {e} (non-fatal)")
            probes[c_curie] = {
                "pinned_curie": c_curie,
                "pinned_cat": primary_mention_cat,
                "answer_cat": answer_category,
                "total_edges": 0,
                "by_predicate": {},
                "latency_s": 0.0,
                "error": str(e),
            }
            continue
        probes[c_curie] = {
            "pinned_curie": probe.pinned_curie,
            "pinned_cat": probe.pinned_cat,
            "answer_cat": probe.answer_cat,
            "total_edges": probe.total_edges,
            "by_predicate": probe.by_predicate,
            "latency_s": probe.latency_s,
            "error": probe.error,
        }
    return probes


def _has_any_kg_coverage(probes: dict[str, Any]) -> bool:
    # true if at least one probed candidate found ≥1 KG2c edge to the
    # answer category. used to decide whether the strict NameRes pass
    # already gives us workable candidates, or whether we need to fall
    # back to the loose BMT-derived neighborhood filter.
    return any(
        (p.get("total_edges") or 0) > 0 and not p.get("error")
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
    m = mention.lower().strip()
    lbl = label.lower().strip()
    # both-empty corner case: SequenceMatcher.ratio("", "") returns 1.0
    # in stdlib (empty trivially matches empty), but two empty strings
    # carry no information — a 1.0 score here would let a junk
    # resolution pass the consistency threshold. caught by unit test
    # test_empty_inputs_score_zero. handle explicitly before delegating.
    if not m or not lbl:
        return 0.0, {
            "mention_normalized": m,
            "label_normalized": lbl,
            "seqmatcher_ratio": 0.0,
            "substring_match": False,
        }
    seq = SequenceMatcher(None, m, lbl).ratio()
    substr = m in lbl or lbl in m
    sim = max(seq, 1.0 if substr else 0.0)
    return sim, {
        "mention_normalized": m,
        "label_normalized": lbl,
        "seqmatcher_ratio": seq,
        "substring_match": substr,
    }


def _format_low_confidence_explanation(
    question: str, mention: str, canonical: str, label: str, similarity: float,
) -> str:
    # Markdown body for the explanation.md artifact when Stage 7 refuses
    # to query PloverDB because the resolved entity has low textual
    # similarity to what the user typed. mirrors the section structure of
    # the normal explainer + the out_of_scope refusal so the UI renders
    # the same component, just with a low_confidence_resolution badge.
    return (
        "## Answer\n\n"
        f"PloverAI could not confidently resolve **{mention}** to a "
        f"known biomedical entity in RTX-KG2.10.2c.\n\n"
        "## Reason\n\n"
        f"The closest match found was **{canonical}** "
        f"(*{label}*), but the resolved label is too different from what "
        f"you typed (similarity {similarity:.2f}, threshold 0.50). "
        f"Querying PloverDB with this entity would likely return results "
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


def _format_out_of_scope_explanation(question: str, reason: str) -> str:
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
        "PloverAI answers biomedical questions whose answers live in "
        "a knowledge graph of relationships between drugs, diseases, "
        "genes, proteins, chemicals, phenotypes, biological processes, "
        "pathways, and anatomical structures. Examples it can handle:\n\n"
        "- *What drugs treat type 2 diabetes?*\n"
        "- *Which genes are associated with cystic fibrosis?*\n"
        "- *What pathways involve HMGCR?*\n"
        "- *Which diseases present with seizures?*\n"
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


def _extract_json(text: str) -> dict[str, Any]:
    # the LLM sometimes wraps JSON in ```json ... ``` despite our prompt.
    # we strip fences if present, then try plain json.loads. if that
    # fails we make one last attempt with the largest {...} block. any
    # less and we give up — we want llm_bad_json failures to be loud.
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```\s*$", "", s)
    try:
        loaded: dict[str, Any] = json.loads(s)
        return loaded
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, flags=re.DOTALL)
        if not m:
            raise
        loaded2: dict[str, Any] = json.loads(m.group(0))
        return loaded2


def run_grounded(
    *,
    cfg: Config,
    model: ModelSpec,
    q: dict[str, Any],
    qp: QuestionPaths,
    llm: OpenRouterClient,
    nameres: NameResClient,
    nodenorm: NodeNormClient,
    plover: PloverClient,
    logger: logging.Logger,
    predicate_index: dict[tuple[str, str], list[str]] | None = None,
    pubtator: PubTatorClient | None = None,
    available_categories: list[str] | None = None,
    biolink_neighborhoods: dict[str, list[str]] | None = None,
) -> QuestionResult:
    # `predicate_index` is the (subject_cat, object_cat) -> [predicates]
    # map built from PloverDB's meta_knowledge_graph at boot. when
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
    logger.info(f"{tag}  question=[white]{nl_question!r}[/]")

    # cfg is held here for future stages (e.g. response reduction in
    # v16) that will read knobs from it. silence the unused-warning
    # by referencing it.
    _ = cfg

    # ---------- Stage 1: scope-check guardrail ----------
    # decides whether the question is biomedical at all. refusing
    # here costs ~1 cheap LLM call and saves the full pipeline cost
    # (~6 LLM calls + 4 RENCI calls + PloverDB) on out-of-scope
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
            model=model,
            system=prompts.SYS_SCOPE_CHECK,
            user=user_msg_scope,
            stage="scope_check",
        )
    except OpenRouterError as e:
        return _finish_failure(qp, ledger, t_start, q["id"], STATUS_LLM_ERROR, str(e))
    prompt_log["stage_1_scope_check"]["response"] = _llm_response_meta(rep_scope)
    write_json(qp.prompt, prompt_log)
    ledger.add(
        "scope_check", model.id, model.slug,
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
        explanation = _format_out_of_scope_explanation(nl_question, scope.reason)
        write_text(qp.explanation, explanation)
        _flush_meta_and_cost(
            qp, ledger, t_start, q["id"],
            STATUS_OUT_OF_SCOPE, scope.reason or "question is outside biomedical-KG scope",
            outcome="out_of_scope",
            outcome_reason=scope.reason,
            plover_n_results=-1, answers_n_picked=-1,
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

    # ---------- Stage 2: extract focal entity mention from NL ----------
    # we inject the list of Biolink categories PloverDB ACTUALLY has
    # in this KG2c build (sourced from meta_knowledge_graph at startup).
    # the LLM must pick expected_category / answer_category from this
    # exact list — no hallucinating biolink:CellType when the KG only
    # has biolink:Cell. when the list is missing (meta-KG fetch failed
    # at boot), the LLM falls back to the system-prompt's catch-all
    # advice and still produces something usable.
    categories_block = (
        "Available Biolink categories in this PloverDB build "
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
            model=model,
            system=prompts.SYS_ENTITY_EXTRACT,
            user=user_msg_0,
            stage="entity_extract",
        )
    except OpenRouterError as e:
        return _finish_failure(qp, ledger, t_start, q["id"], STATUS_LLM_ERROR, str(e))
    prompt_log["stage_2_entity_extract"]["response"] = _llm_response_meta(rep0)
    write_json(qp.prompt, prompt_log)
    ledger.add(
        "entity_extract", model.id, model.slug,
        rep0.input_tokens, rep0.output_tokens,
        rep0.cost.input_usd, rep0.cost.output_usd, rep0.cost.total_usd,
        rep0.latency_s,
    )
    # Stage 2 now emits JSON with four fields: entity, expected_category,
    # answer_category, granularity_preference. these power three fixes:
    #   - expected_category → NameRes biolink_type filter (entity type)
    #   - granularity_preference → IC-based re-ranking (entity granularity, A1)
    #   - answer_category → meta_KG predicate-list lookup for Stage 8 (B)
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
    #                     non-zero KG2c edges to the answer category.
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
    # back to loose. probe is per-CURIE: ~0.3-0.5s each, ~3-5s total
    # for 10 candidates.
    candidate_probes_by_curie: dict[str, Any] = _probe_candidates(
        candidates=nr_candidates_full,
        primary_mention_cat=primary_mention_cat,
        answer_category=answer_category,
        plover=plover,
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
            f"KG2c coverage in top-{NAMERES_DISPLAY} → retry with loose {loose_filter}"
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
                plover=plover,
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

    # NOTE: `nameres_filter_applied` (kept for back-compat with older
    # log readers) reports the filter that produced the FINAL candidate
    # set. `strict_attempt` records the discarded strict pass when we
    # fell back.
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
    # granularity + NAMERES_DISPLAY candidates. it picks one CURIE, or
    # returns null with a reason. null → STATUS_NO_CANDIDATE_MATCH
    # (fail loudly, no silent grounding to garbage).
    candidates_block_lines: list[str] = []
    for i, c in enumerate(nr_candidates_full[:NAMERES_DISPLAY], start=1):
        c_curie = c.get("curie")
        c_label = c.get("label")
        c_types = c.get("types") or []
        c_score = c.get("score")
        # if we ran the candidate-density probe, attach the per-candidate
        # edge count to the answer category. this lets the LLM avoid
        # the "lexical match but zero KG2c coverage" trap (e.g. picking
        # PANTHER.PATHWAY:P00014 for cholesterol biosynthesis when
        # PANTHER pathways have 0 edges to Gene nodes in KG2c — the
        # Reactome / GO equivalents in the same candidate set are the
        # ones that actually have edges).
        probe_suffix = ""
        if c_curie in candidate_probes_by_curie:
            cp = candidate_probes_by_curie[c_curie]
            n = cp.get("total_edges", 0)
            err = cp.get("error")
            if err:
                probe_suffix = f"  kg2c_edges_to_{answer_category}=probe_failed({err})"
            else:
                probe_suffix = f"  kg2c_edges_to_{answer_category}={n}"
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
            f"Each candidate carries a `kg2c_edges_to_{answer_category}` "
            f"count: the number of edges in KG2c from that CURIE to any "
            f"node of the answer category (in either direction). A "
            f"high-BM25 candidate with ZERO KG2c edges will produce no "
            f"results downstream — prefer a slightly lower-ranked "
            f"candidate that has actual edges over a perfect-label one "
            f"with no data.\n\n"
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
            model=model,
            system=prompts.SYS_CANDIDATE_PICK,
            user=user_msg_pick,
            stage="candidate_pick",
        )
        prompt_log["stage_4_candidate_pick"]["response"] = _llm_response_meta(rep_pick)
        write_json(qp.prompt, prompt_log)
        ledger.add(
            "candidate_pick", model.id, model.slug,
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
            sorted_by_ic = sorted(candidate_curies, key=_ic_key)
            if sorted_by_ic[0] != chosen_curie:
                prev_curie = chosen_curie
                chosen_curie = sorted_by_ic[0]
                reranked = True
                logger.info(
                    f"{tag}  ic_rerank  SWAP  "
                    f"{prev_curie} (IC={nn_candidates.information_content.get(prev_curie)}) "
                    f"→ {chosen_curie} (IC={nn_candidates.information_content.get(chosen_curie)})  "
                    f"granularity=general"
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
    # and a PloverDB call on a probably-wrong entity, compare the
    # resolved label against the user's mention via the testable pure
    # function _check_label_consistency (similarity = max of seqmatcher
    # ratio and substring containment).
    similarity, sim_debug = _check_label_consistency(
        mention=mention, label=pinned_label or "",
    )
    logger.info(
        f"Stage 7 consistency: mention={sim_debug['mention_normalized']!r} "
        f"vs label={sim_debug['label_normalized']!r}  similarity={similarity:.2f}  "
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
            question=nl_question,
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

    # ---------- Stage 8: LLM builds TRAPI query graph ----------
    # the predicate list (fix B) is the meta_KG slice for the
    # (pinned_category, answer_category) pair. we try both directions
    # because Stage 8 may put the pinned entity as either subject or
    # object — the LLM picks the direction along with the predicate.
    # if the predicate_index is empty (meta_KG fetch failed at boot)
    # or we don't know the answer_category, fall back to the old
    # unconstrained prompt so the pipeline still runs.
    valid_predicates_forward: list[str] = []
    valid_predicates_reverse: list[str] = []
    primary_pinned_cat = pinned_categories[0] if pinned_categories else None
    if predicate_index and primary_pinned_cat and answer_category:
        valid_predicates_forward = predicate_index.get((primary_pinned_cat, answer_category), [])
        valid_predicates_reverse = predicate_index.get((answer_category, primary_pinned_cat), [])

    # CURIE-specific predicate-density data (fix C: grounding the LLM
    # in the ACTUAL edge distribution for this pinned entity, not just
    # the schema-valid predicates). REUSED from the candidate-density
    # sweep that ran before Stage 4 — same CURIE, same answer category,
    # same predicate distribution — so we look up the chosen CURIE's
    # probe in `candidate_probes_by_curie` instead of firing another
    # PloverDB call here. saves ~0.3-0.5s per question.
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
                fresh = plover.probe_predicates(
                    canonical_pinned, primary_pinned_cat, answer_category,
                )
                probe_dict = {
                    "pinned_curie": fresh.pinned_curie,
                    "pinned_cat": fresh.pinned_cat,
                    "answer_cat": fresh.answer_cat,
                    "total_edges": fresh.total_edges,
                    "by_predicate": fresh.by_predicate,
                    "latency_s": fresh.latency_s,
                    "error": fresh.error,
                }
            except PloverError as e:
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
            f"actual KG2c edge counts:"
        ]
        sorted_preds = sorted(
            probe_dict["by_predicate"].items(),
            key=lambda kv: kv[1]["count"],
            reverse=True,
        )
        for pred, stats in sorted_preds:
            # describe dominant direction. "forward" = pinned is SUBJECT
            # in KG2c, so subject={pinned_cat} object={answer_cat} in
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
                # neither endpoint matched pinned_curie exactly — Plover
                # auto-expanded the pinned to descendants, all edges
                # touch descendant CURIEs. we can't infer direction
                # cleanly, so just label it.
                direction = "descendant-only (orient either way)"
            lines.append(f"  - {pred}: {stats['count']} edges ({direction})")
        lines.append("")
        lines.append(
            "Pick the predicate whose MEANING best matches the verb / "
            "intent in the user's question. Examples:\n"
            "  - 'what treats X' → biolink:treats (or biolink:applied_to_treat\n"
            "    if 'treats' is not in the list)\n"
            "  - 'what causes X' → biolink:causes\n"
            "  - 'genes associated with X' → biolink:gene_associated_with_condition\n"
            "  - 'what's in trials for X' → biolink:in_clinical_trials_for\n"
            "Do NOT pick a predicate just because it has the highest edge\n"
            "count. The counts are shown only for ORIENTATION and as a\n"
            "tie-breaker between predicates with the SAME meaning. A\n"
            "predicate with 50 edges that matches the user's verb beats a\n"
            "predicate with 500 edges that doesn't.\n"
            "\n"
            "After picking the predicate, orient your TRAPI edge\n"
            "(subject/object) to match the dominant direction shown\n"
            "above. Predicates not listed have ZERO edges from this\n"
            "pinned CURIE to the answer category and will return no\n"
            "results."
        )
        predicate_block = "\n".join(lines) + "\n\n"
    elif valid_predicates_forward or valid_predicates_reverse:
        # probe returned 0 edges (or didn't run) — fall back to the
        # schema-valid list. warn the LLM that these may be sparse for
        # this specific CURIE.
        lines = ["Valid Biolink predicates in THIS PloverDB build:"]
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
        f"Build the one-hop TRAPI query graph."
    )
    prompt_log["stage_8_trapi_build"] = {
        "system": prompts.SYS_TRAPI_BUILD,
        "user": user_msg_1,
    }
    write_json(qp.prompt, prompt_log)

    try:
        rep1 = llm.chat(
            model=model,
            system=prompts.SYS_TRAPI_BUILD,
            user=user_msg_1,
            stage="trapi_build",
        )
    except OpenRouterError as e:
        return _finish_failure(qp, ledger, t_start, q["id"], STATUS_LLM_ERROR, str(e))
    prompt_log["stage_8_trapi_build"]["response"] = _llm_response_meta(rep1)
    write_json(qp.prompt, prompt_log)
    ledger.add(
        "trapi_build", model.id, model.slug,
        rep1.input_tokens, rep1.output_tokens,
        rep1.cost.input_usd, rep1.cost.output_usd, rep1.cost.total_usd,
        rep1.latency_s,
    )

    try:
        trapi_msg = _extract_json(rep1.content)
    except json.JSONDecodeError:
        write_text(qp.explanation, rep1.content)
        return _finish_failure(
            qp, ledger, t_start, q["id"],
            STATUS_LLM_BAD_JSON, "Stage 8 output was not valid JSON",
        )

    write_json(qp.trapi_query, trapi_msg)

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
        # v15 policy: invalid -> stop. no repair loop yet (that's v16).
        n_err = len(val.errors) if isinstance(val.errors, dict) else 0
        return _finish_failure(
            qp, ledger, t_start, q["id"],
            STATUS_INVALID_QUERY,
            f"reasoner-validator rejected with {n_err} error groups",
        )

    # ---------- Stage 10: send to PloverDB ----------
    write_json(qp.plover_request, trapi_msg)
    try:
        prep = plover.query(trapi_msg)
    except PloverError as e:
        return _finish_failure(qp, ledger, t_start, q["id"], STATUS_PLOVER_ERROR, str(e))
    write_json(qp.plover_response, prep.body)

    # outcome diagnostic (used at the end of the run): how many TRAPI
    # results came back? zero means PloverDB had nothing for the
    # constructed query — usually a sign the resolved pinned CURIE
    # was wrong, or the predicate doesn't fit, or the question is
    # genuinely outside KG2c. we record the number on every run so
    # the offline analysis can group runs by it.
    plover_n_results = len(prep.body.get("message", {}).get("results") or [])

    # ---------- Stage 11: LLM picks answer ----------
    # Strategy B reduction (predicate-grouped knowledge_level ranking)
    # shrinks the PloverDB body BEFORE the LLM sees it. the top-N is a
    # config knob; the reduced body + per-group stats are written out
    # as artifacts so the faithfulness evaluator can grade answers
    # against what the LLM saw, not the full PloverDB response.
    reduction = reduce_plover_response(
        prep.body,
        top_n_per_predicate=cfg.reduction.top_n_per_predicate,
        logger=logger,
    )
    write_json(qp.reduced_data, reduction.reduced_body)
    write_json(qp.reduction_metadata, asdict(reduction.metadata))
    user_msg_4 = (
        f"User question: {nl_question}\n\n"
        f"TRAPI response (reduced — top "
        f"{reduction.metadata.top_n_per_predicate} results per predicate, "
        f"ranked by knowledge_level then agent_type):\n"
        f"{json.dumps(reduction.reduced_body, ensure_ascii=False)}\n"
    )
    prompt_log["stage_11_answer_pick"] = {
        "system": prompts.SYS_ANSWER_PICK,
        "user_truncated": user_msg_4[:2000],
    }
    write_json(qp.prompt, prompt_log)

    try:
        rep4 = llm.chat(
            model=model,
            system=prompts.SYS_ANSWER_PICK,
            user=user_msg_4,
            stage="answer_pick",
        )
    except OpenRouterError as e:
        return _finish_failure(qp, ledger, t_start, q["id"], STATUS_LLM_ERROR, str(e))
    prompt_log["stage_11_answer_pick"]["response"] = _llm_response_meta(rep4)
    write_json(qp.prompt, prompt_log)
    ledger.add(
        "answer_pick", model.id, model.slug,
        rep4.input_tokens, rep4.output_tokens,
        rep4.cost.input_usd, rep4.cost.output_usd, rep4.cost.total_usd,
        rep4.latency_s,
    )

    try:
        answer_obj = _extract_json(rep4.content)
    except json.JSONDecodeError:
        return _finish_failure(
            qp, ledger, t_start, q["id"],
            STATUS_LLM_BAD_JSON, "Stage 11 output was not valid JSON",
        )

    # ---------- Stage 12: NodeNorm canonicalises every answer CURIE ----------
    raw_answer_curies: list[str] = [
        a["curie"] for a in (answer_obj.get("answers") or []) if a.get("curie")
    ]
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

    # ---------- Stage 13: build research-grade graph view ----------
    # reshape (pinned + canonical answers + PloverDB knowledge_graph)
    # into a node-link graph view with per-edge provenance suitable for
    # rendering as a hoverable graph card in the UI. pure function,
    # unit-tested in tests/test_answer_graph_view.py.
    canonical_curies_for_view: list[str] = answer_obj.get("canonical_curies") or []
    answer_graph_view = _build_answer_graph_view(
        pinned_curie=canonical_pinned,
        pinned_label=pinned_label,
        pinned_category=primary_pinned_cat,
        picked_answer_curies=canonical_curies_for_view,
        plover_response=prep.body,
    )

    # ---------- Stage 13 enrichment: PubTator co-mention verification ----------
    # for each edge with supporting_publications, ask PubTator whether
    # the cited PMIDs actually mention BOTH endpoints (via any of their
    # equivalent CURIEs). this converts "the KG says this is supported"
    # into "this is supported AND independently verifiable by NLM NER".
    # graceful degradation: if pubtator client is None or errors,
    # edges get pubtator_verified=None and the pipeline carries on.
    pubtator_summary: dict[str, Any] = {"called": False, "reason": "client_not_provided"}
    if pubtator is not None and answer_graph_view["edges"]:
        # KG2c edges from SemMedDB often have 30+ supporting PMIDs each.
        # the cap below is a sanity budget, not the load-bearing
        # protection. measured runs show pubtator's biocjson endpoint
        # returns one batched response in ~0.3-1.5s regardless of how
        # many PMIDs are in the request — the latency cost is per-call,
        # not per-PMID, so larger caps are cheap.
        #
        # the real latency/cost bottleneck we found in profiling is
        # NOT pubtator: it's the size of the PloverDB /query response
        # being shoved into the answer_pick + explain LLM prompts. on
        # the warfarin run those two stages were 4.0s + 4.6s with 68k
        # input tokens each (~$0.07 total), against a 1.6 MB PloverDB
        # response. response reduction (predicate grouping + top-N by
        # knowledge_level) is where the real wins are — see the
        # reduction-strategy code path, not this cap.
        #
        # so the cap stays in place purely as a pathological-case
        # guard (someone runs an "all genes related to MONDO:..."
        # query and gets back 50 edges × 50 PMIDs = 2500 PMIDs). under
        # normal load these caps almost never bite.
        MAX_PMIDS_PER_EDGE = 20
        MAX_PMIDS_TOTAL = 200
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
        endpoint_curies = list({canonical_pinned, *canonical_curies_for_view})
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
    logger.info(
        f"{tag}  answer_graph_view  nodes={1 + len(answer_graph_view['answer_nodes'])}  "
        f"edges={len(answer_graph_view['edges'])}  "
        f"pubtator_rate={answer_graph_view['pubtator_metrics']['rate']}"
    )

    # ---------- compute end-to-end outcome ----------
    # at this point we know what came out of Stages 10 and 11. status
    # is still "ok" (we'd have returned earlier on any runtime error),
    # so this is purely about whether the model produced a useful
    # answer for the user. three cases:
    #   - PloverDB had nothing to work with                     → no_results
    #   - PloverDB had something, but the LLM picked nothing    → no_answer_picked
    #   - LLM picked at least one answer                        → answered
    answers_n_picked = len(answer_obj.get("answers") or [])
    if plover_n_results == 0:
        outcome = OUTCOME_NO_RESULTS
        outcome_reason = (
            f"PloverDB returned 0 results for the constructed query "
            f"(pinned CURIE: {canonical_pinned}). Possible cause: NameRes "
            f"top-1 picked a non-canonical or wrong identifier, the "
            f"predicate doesn't match what KG2c stores, or the question is "
            f"outside KG2c's coverage."
        )
    elif answers_n_picked == 0:
        # use the LLM's own rationale if it gave one — that's the
        # most informative description of what the model decided.
        rationale = str(answer_obj.get("rationale") or "")[:300]
        outcome = OUTCOME_NO_ANSWER_PICKED
        outcome_reason = (
            f"PloverDB returned {plover_n_results} results, but the LLM "
            f"selected zero answers. Rationale (from the model): "
            f"{rationale!r}"
        )
    else:
        outcome = OUTCOME_ANSWERED
        outcome_reason = None

    # ---------- Stage 15: explanation ----------
    # the LLM only sees the edges Stage 11 ALREADY PICKED, not the full
    # PloverDB body. this is a structural anti-hallucination measure:
    # the explainer can only ground citations in edges that are in its
    # prompt, so it cannot invent citations from non-picked edges.
    user_msg_5 = (
        f"User question: {nl_question}\n\n"
        f"Selected answers (Stage 11):\n{json.dumps(answer_obj, ensure_ascii=False)}\n\n"
        f"Picked-edge view (use ONLY these edges to ground citations):\n"
        f"{json.dumps(answer_graph_view, ensure_ascii=False)}\n"
    )
    prompt_log["stage_15_explain"] = {
        "system": prompts.SYS_EXPLAIN,
        "user_truncated": user_msg_5[:2000],
    }
    write_json(qp.prompt, prompt_log)

    try:
        rep5 = llm.chat(
            model=model,
            system=prompts.SYS_EXPLAIN,
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
            plover_n_results=plover_n_results, answers_n_picked=answers_n_picked,
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
            plover_n_results=plover_n_results,
            answers_n_picked=answers_n_picked,
        )
    prompt_log["stage_15_explain"]["response"] = _llm_response_meta(rep5)
    write_json(qp.prompt, prompt_log)
    ledger.add(
        "explain", model.id, model.slug,
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
        plover_n_results=plover_n_results, answers_n_picked=answers_n_picked,
    )


# ---------- internal helpers ----------

def _finish_success(
    qp: QuestionPaths, ledger: CostLedger, t_start: float, q_id: str,
    *,
    outcome: str | None = None,
    outcome_reason: str | None = None,
    plover_n_results: int = -1,
    answers_n_picked: int = -1,
) -> QuestionResult:
    _flush_meta_and_cost(
        qp, ledger, t_start, q_id, STATUS_OK, None,
        outcome=outcome, outcome_reason=outcome_reason,
        plover_n_results=plover_n_results, answers_n_picked=answers_n_picked,
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
        plover_n_results=plover_n_results,
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
    plover_n_results: int = -1,
    answers_n_picked: int = -1,
) -> None:
    # cost.json gets a per-stage list and a summary. meta.json is the
    # one-line summary the analysis script reads to count failures.
    # outcome/outcome_reason describe whether the model actually
    # answered (separate axis from `status`, which is about runtime
    # errors). counts are -1 when the corresponding stage didn't run.
    ti, to = ledger.total_tokens()
    write_json(qp.cost, {
        "stages": ledger.entries,
        "totals": {
            "input_tokens": ti,
            "output_tokens": to,
            "total_usd": ledger.total_usd(),
        },
    })
    write_json(qp.meta, {
        "q_id": q_id,
        "status": status,
        "outcome": outcome,
        "outcome_reason": outcome_reason,
        "plover_n_results": plover_n_results,
        "answers_n_picked": answers_n_picked,
        "error": err,
        "elapsed_s": round(time.perf_counter() - t_start, 3),
    })
