# scorer.py — the offline benchmark scorer. reads saved run artifacts
# from code/outputs/RUN_*/<model>/<condition>/<q_id>/ plus the gold set,
# and writes one CSV row per (run, model, gold question) cell together
# with a per-model summary CSV and a console table.
#
# hard rule: this module makes NO network calls and mutates nothing
# under outputs/RUN_*. re-running it on the same artifacts must produce
# byte-identical CSV bodies, because the paper's numbers have to be
# reproducible from the archived run folders alone. that is also why
# answer equivalence is plain CURIE string equality and not a NodeNorm
# lookup — see the caveat comment on `answer_match`.
#
# every metric is a small pure function over already-parsed JSON, so
# the test file can exercise each one without touching the filesystem
# layout, and a failed cell (crashed, llm_error, no meta.json at all)
# is a normal row with None in the columns whose stage never ran —
# nothing in here raises on a half-written cell.

from __future__ import annotations

# argparse: stdlib. same reasoning as runner.py — three flags do not
# justify click/typer, and the two CLIs stay stylistically identical.
import argparse

# csv: stdlib. the two output files are plain CSV so R / pandas / a
# spreadsheet can read them with no custom parser.
import csv

# json: stdlib. every artifact except explanation.md is JSON.
import json

# re: stdlib. citation tokens are extracted from the Markdown
# explanation with two literal patterns (see the regexes below).
import re

# dataclasses: stdlib. CellScore and ModelSummary are frozen dataclasses
# so the CSV column order is the field order — one place to change it,
# and no dict-key drift between the header and the rows.
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

# our config module. gold questions and the outputs root both come from
# config.yaml, so the scorer never hard-codes a path.
from .config import Config, load_benchmark_questions, load_config

# utc_stamp: the same "2026-09-01T16-49-43Z" formatter the runner uses
# for RUN_ folders, reused here so a scores file sorts next to the runs
# it scored and carries no colons in its name.
from .logging_setup import utc_stamp


# the plain condition's folder: ARAX, the default reasoner. the lookup
# condition, oracle runs and stage swaps write to sibling folders
# (lookup/, arax_oracle_query/, arax__answer_pick=m1/ ...; see
# runner.condition_name) and are discovered and reported separately.
CONDITION_DIR = "arax"

# free-form questions are written to <condition>/adhoc/ and have no gold
# record, so they are never scored.
ADHOC_Q_ID = "adhoc"

# Stage 15 emits citations as bare tokens inside square brackets, e.g.
# "[edge:46516189, PMID:14684759]" (the lookup condition's explainer).
# edge ids are opaque strings, so the character class stops at
# whitespace, "," and "]" to avoid swallowing the next citation or
# sentence punctuation that follows one.
EDGE_CITATION_RE = re.compile(r"\bedge:([^\s,\]]+)")
PMID_CITATION_RE = re.compile(r"PMID:(\d+)")


@dataclass(frozen=True)
class GoldQuestion:
    q_id: str
    nl_question: str
    pinned_curie: str
    predicate: str
    # curated q*.json: the hand-verified answers. converted Translator
    # questions: every id of every TopAnswer / Acceptable answer.
    verified_curies: frozenset[str]
    # the rest only exist on converted Translator questions; the
    # defaults keep a curated record exactly as it was scored before.
    nevershow_curies: frozenset[str] = frozenset()
    gold_direction: str | None = None
    split: str | None = None
    phrasing: str | None = None
    group_id: str | None = None
    # curated no-answer questions (q14, q15): the correct one-hop query
    # returned nothing in KG2, so the right behaviour was to decline.
    expects_abstain: bool = False


# the Translator labels that count as a correct answer. BadButForgivable
# and OverlyGeneric are neither credited nor penalised.
POSITIVE_LABELS = frozenset({"TopAnswer", "Acceptable"})
NEVERSHOW_LABELS = frozenset({"NeverShow"})
DIRECTION_QUALIFIER = "biolink:object_direction_qualifier"


@dataclass(frozen=True)
class CellPath:
    # where one (run, model, question) cell lives, plus the identifiers
    # recovered from the folder names. model_dir is kept verbatim
    # because it is the only on-disk record of which OpenRouter slug
    # produced the cell (the slug's "/" became "_" when the folder was
    # created, and that is not reliably invertible).
    run_id: str
    model_id: str
    model_dir: str
    q_id: str
    root: Path
    condition: str = CONDITION_DIR


@dataclass(frozen=True)
class CellScore:
    # one CSV row. field order here IS the column order.
    run_id: str
    model_id: str
    model_dir: str
    condition: str
    q_id: str
    status: str | None
    outcome: str | None
    elapsed_s: float | None
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    entity_extract_ok: bool
    nameres_top1_match: bool | None
    valid_trapi: bool | None
    executed: bool | None
    predicate_match: bool | None
    predicate_used: str | None
    answer_match: bool | None
    verified_hits: int | None
    n_picked: int | None
    verified_hit_rate: float | None
    citations_cited: int | None
    citations_resolved: int | None
    unsupported_citation_count: int | None
    split: str | None
    phrasing: str | None
    group_id: str | None
    nevershow_picked: int | None
    direction_match: bool | None
    abstained: bool | None


@dataclass(frozen=True)
class ModelSummary:
    # one row of summary_<stamp>.csv. every *_rate is computed over the
    # cells where that metric is not None; the paired *_n_none column
    # says how many cells were excluded, so a rate of 1.00 over one
    # cell can never be mistaken for a rate of 1.00 over twenty.
    model_id: str
    condition: str
    n_cells: int
    answered_rate: float
    entity_extract_ok_rate: float
    nameres_top1_match_rate: float | None
    nameres_top1_match_n_none: int
    valid_trapi_rate: float | None
    valid_trapi_n_none: int
    executed_rate: float | None
    executed_n_none: int
    predicate_match_rate: float | None
    predicate_match_n_none: int
    answer_match_rate: float | None
    answer_match_n_none: int
    mean_verified_hit_rate: float | None
    verified_hit_rate_n_none: int
    total_citations_cited: int
    total_citations_resolved: int
    total_unsupported_citations: int
    # share of cells (on questions that HAVE NeverShow labels) where at
    # least one picked entity is labelled NeverShow.
    nevershow_cell_rate: float | None
    nevershow_cell_n_none: int
    direction_match_rate: float | None
    direction_match_n_none: int
    abstain_rate: float | None
    abstain_n_none: int
    mean_cost_usd: float | None
    total_cost_usd: float
    mean_elapsed_s: float | None


# ---------------------------------------------------------------- gold


def labelled_curies(
    labelled_answers: list[dict[str, Any]],
    labels: frozenset[str],
) -> frozenset[str]:
    # every id under which a labelled answer could be picked: the id in
    # the Translator asset, its NodeNorm canonical form and the rest of
    # its NodeNorm clique, all frozen into the record at conversion
    # time so this comparison stays offline and reproducible.
    curies: set[str] = set()
    for answer in labelled_answers:
        if answer.get("expected_output") not in labels:
            continue
        for key in ("asset_curie", "canonical_curie"):
            if isinstance(answer.get(key), str):
                curies.add(answer[key])
        curies.update(str(c) for c in answer.get("equivalent_curies") or [])
    return frozenset(curies)


def _record_direction(record: dict[str, Any]) -> str | None:
    for qualifier in record.get("qualifiers") or []:
        if isinstance(qualifier, dict) and qualifier.get("qualifier_type_id") == DIRECTION_QUALIFIER:
            value = qualifier.get("qualifier_value")
            return str(value) if value else None
    return None


def build_gold_index(cfg: Config) -> dict[str, GoldQuestion]:
    index: dict[str, GoldQuestion] = {}
    for record in load_benchmark_questions(cfg):
        pinned = record.get("pinned_entity")
        pinned_curie = str(pinned["curie"]) if isinstance(pinned, dict) else ""
        labelled = record.get("labelled_answers")
        nevershow: frozenset[str] = frozenset()
        if isinstance(labelled, list):
            curies = set(labelled_curies(labelled, POSITIVE_LABELS))
            nevershow = labelled_curies(labelled, NEVERSHOW_LABELS)
        else:
            curies = set()
            verified = record.get("verified_answers")
            if isinstance(verified, list):
                for entry in verified:
                    if isinstance(entry, dict) and entry.get("curie"):
                        curies.add(str(entry["curie"]))
        index[str(record["id"])] = GoldQuestion(
            q_id=str(record["id"]),
            nl_question=str(record.get("nl_question", "")),
            pinned_curie=pinned_curie,
            predicate=str(record.get("predicate", "")),
            verified_curies=frozenset(curies),
            nevershow_curies=nevershow,
            gold_direction=_record_direction(record),
            split=record.get("split"),
            phrasing=record.get("phrasing"),
            group_id=record.get("group_id"),
            expects_abstain=record.get("expected_behavior") == "abstain",
        )
    return index


# ------------------------------------------------------------ file I/O


def read_json(path: Path) -> dict[str, Any] | None:
    # returns None for "absent or unusable" so every metric has exactly
    # one missing-input case to handle. a truncated artifact from a
    # killed run is treated the same as an absent one — the scorer must
    # never crash on a half-written cell.
    if not path.is_file():
        return None
    try:
        loaded: Any = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    return loaded if isinstance(loaded, dict) else None


def read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text()
    except (UnicodeDecodeError, OSError):
        return None


# ---------------------------------------------------------- per metric


def entity_extract_ok(nameres: dict[str, Any] | None) -> bool:
    # Stage 2 produced a mention and Stage 3 wrote it down. this is the
    # only metric that is a plain bool: "the file is missing" and "the
    # file has an empty mention" both mean the extraction did not
    # produce a usable entity, so there is no third state to model.
    if nameres is None:
        return False
    mention = nameres.get("mention")
    return isinstance(mention, str) and bool(mention.strip())


def nameres_top1_match(
    nodenorm: dict[str, Any] | None,
    gold_pinned_curie: str,
) -> bool | None:
    # the name is historical: the number we compare is the CURIE the
    # pipeline actually pinned after Stage 6 canonicalization, which is
    # the id that reaches the reasoner. nodenorm.json is the file that
    # records it (nameres.json holds the pre-canonical pick).
    if nodenorm is None:
        return None
    pinned = nodenorm.get("pinned")
    if not isinstance(pinned, dict):
        return None
    canonical = pinned.get("canonical_curie")
    if not isinstance(canonical, str):
        return None
    return canonical == gold_pinned_curie


def valid_trapi(validation: dict[str, Any] | None) -> bool | None:
    # validation.json is the reasoner-validator report flattened by
    # trapi_validator.py; its top-level "passed" is the gate Stage 9
    # keyed on. the nested "raw" block is diagnostics only.
    if validation is None:
        return None
    passed = validation.get("passed")
    return bool(passed) if isinstance(passed, bool) else None


def query_executed(meta: dict[str, Any] | None) -> bool | None:
    # meta writes -1 as the "never got there" sentinel and >= 0 for a
    # real round trip to the reasoner (0 results is still an execution).
    # so a crashed cell that has a meta.json scores False here, not None
    # — None is reserved for "we have no meta.json to ask". runs made
    # before the Tier 0 move called the count plover_n_results.
    if meta is None:
        return None
    n_results = meta.get("n_results", meta.get("plover_n_results"))
    if not isinstance(n_results, int):
        return None
    return n_results >= 0


def trapi_predicates(trapi_query: dict[str, Any] | None) -> list[str] | None:
    # every Stage 8 query graph is one hop named e0.
    if trapi_query is None:
        return None
    message = trapi_query.get("message")
    if not isinstance(message, dict):
        return None
    query_graph = message.get("query_graph")
    if not isinstance(query_graph, dict):
        return None
    edges = query_graph.get("edges")
    if not isinstance(edges, dict):
        return None
    edge = edges.get("e0")
    if not isinstance(edge, dict):
        return None
    predicates = edge.get("predicates")
    if not isinstance(predicates, list):
        return None
    return [str(p) for p in predicates]


def predicate_match(
    predicates: list[str] | None,
    gold_predicate: str,
) -> bool | None:
    # exact, whole-list equality. a model that widened the query to
    # ["biolink:treats", "biolink:in_clinical_trials_for"] did NOT ask
    # the gold question, so it does not get credit here even though the
    # gold predicate is present.
    if predicates is None:
        return None
    return predicates == [gold_predicate]


def picked_curie_groups(answer: dict[str, Any] | None) -> list[frozenset[str]] | None:
    # one set per picked entity: its raw CURIE plus the Stage 12
    # canonical form of the same entity. answer.json stores the
    # canonical list positionally alongside answers[], so we zip them
    # only when the lengths agree and fall back to raw-only otherwise.
    if answer is None:
        return None
    answers = answer.get("answers")
    if not isinstance(answers, list):
        return None
    raw_curies: list[str] = []
    for entry in answers:
        if isinstance(entry, dict) and entry.get("curie"):
            raw_curies.append(str(entry["curie"]))
    canonical = answer.get("canonical_curies")
    canonical_list = (
        [str(c) for c in canonical] if isinstance(canonical, list) else []
    )
    aligned = len(canonical_list) == len(raw_curies)
    groups: list[frozenset[str]] = []
    for position, raw in enumerate(raw_curies):
        members = {raw}
        if aligned:
            members.add(canonical_list[position])
        groups.append(frozenset(members))
    return groups


def all_picked_curies(answer: dict[str, Any] | None) -> frozenset[str] | None:
    # the union the spec scores answer_match on: every raw pick plus
    # every canonical CURIE, including canonical entries we could not
    # align to a specific pick.
    groups = picked_curie_groups(answer)
    if groups is None:
        return None
    union: set[str] = set()
    for group in groups:
        union |= group
    canonical = (answer or {}).get("canonical_curies")
    if isinstance(canonical, list):
        union |= {str(c) for c in canonical}
    return frozenset(union)


def answer_match(
    picked: frozenset[str] | None,
    gold_verified: frozenset[str],
) -> bool | None:
    # EQUIVALENCE CAVEAT (documented here because this is the only
    # place two CURIEs are compared for "same thing"): matching is
    # exact string equality on the CURIE, offline. that means a drug
    # picked as its chemical-substance id (CHEBI:6801, metformin) and
    # the same drug recorded in gold under a different clique member
    # (DRUGBANK:DB00331, RXCUI:6809, ...) do NOT match here, even
    # though NodeNorm would collapse them. this is the drug/chemical
    # conflation problem, and we accept the undercount deliberately:
    # resolving cliques would need a live NodeNorm call, which would
    # make the score depend on the day it was computed. gold curies
    # were authored against the KG2 build the first runs queried, so
    # in practice the pipeline's Stage 12 canonical form already
    # agrees with gold; a mismatch is a real miss far more often than
    # a vocabulary artifact.
    if picked is None:
        return None
    return bool(picked & gold_verified)


def verified_hit_counts(
    answer: dict[str, Any] | None,
    gold_verified: frozenset[str],
) -> tuple[int | None, int | None, float | None]:
    # (hits, n_picked, rate). n_picked counts ENTITIES the model
    # picked, not CURIE strings, so canonicalization adding a second id
    # for one drug cannot inflate the denominator.
    #
    # this number is descriptive only. verified_answers is a
    # hand-verified, deliberately non-exhaustive subset of the true
    # answer set (open-world), so a cell that picks five correct drugs
    # of which one is in gold scores 0.2 and is not thereby wrong.
    groups = picked_curie_groups(answer)
    if groups is None:
        return None, None, None
    n_picked = len(groups)
    hits = sum(1 for group in groups if group & gold_verified)
    rate = hits / n_picked if n_picked else None
    return hits, n_picked, rate


def nevershow_count(
    answer: dict[str, Any] | None,
    nevershow: frozenset[str],
) -> int | None:
    # counted per picked ENTITY, like verified_hits. None (not 0) on a
    # question without NeverShow labels, so the rate is taken only over
    # questions where a harmful pick was possible to detect.
    if not nevershow:
        return None
    groups = picked_curie_groups(answer)
    if groups is None:
        return None
    return sum(1 for group in groups if group & nevershow)


def direction_match(
    trapi_query: dict[str, Any] | None,
    gold_direction: str | None,
) -> bool | None:
    # did Stage 8 constrain the query to the direction the question
    # asked? a query with no direction at all returns edges of both
    # directions (and undirected ones), so it did not ask the question.
    if gold_direction is None or trapi_query is None:
        return None
    edge = (
        (trapi_query.get("message") or {}).get("query_graph", {}).get("edges", {}).get("e0")
    )
    if not isinstance(edge, dict):
        return None
    for constraint in edge.get("qualifier_constraints") or []:
        for qualifier in (constraint or {}).get("qualifier_set") or []:
            if (
                isinstance(qualifier, dict)
                and qualifier.get("qualifier_type_id") == DIRECTION_QUALIFIER
            ):
                return bool(qualifier.get("qualifier_value") == gold_direction)
    return False


# outcomes that mean the pipeline ran and chose not to answer: the
# query came back empty, the model picked nothing from a non-empty
# table, or Stage 8 declined to build a query at all.
DECLINED_OUTCOMES = frozenset({"no_results", "no_answer_picked", "query_declined"})


def abstained(meta: dict[str, Any] | None, expects_abstain: bool) -> bool | None:
    # only defined where gold says "decline". a cell that never reached
    # a decision (crash, llm_error, entity failure: outcome None) says
    # nothing about whether the model would have abstained.
    if not expects_abstain or meta is None:
        return None
    outcome = meta.get("outcome")
    if outcome in DECLINED_OUTCOMES:
        return True
    if outcome == "answered":
        return False
    return None


def parse_citations(explanation: str | None) -> tuple[frozenset[str], frozenset[str]]:
    # (edge ids, PMIDs), de-duplicated. citing the same edge in three
    # bullets is one claim about one edge, so distinct tokens are what
    # we count on both the cited and the resolved side.
    if explanation is None:
        return frozenset(), frozenset()
    edge_ids = frozenset(EDGE_CITATION_RE.findall(explanation))
    pmids = frozenset(f"PMID:{d}" for d in PMID_CITATION_RE.findall(explanation))
    return edge_ids, pmids


def resolvable_edge_ids(
    reduced: dict[str, Any] | None,
    answer: dict[str, Any] | None,
) -> frozenset[str]:
    # an edge id is real if it appears in the evidence table the LLM
    # was shown, or in the supporting_edge_ids it recorded at pick
    # time. runs made before the Stage 11 reduction step have no
    # reduced_data.json, and for those the answer.json list is the
    # whole pool — that is a real coverage limit of the older runs, not
    # a scorer bug.
    ids: set[str] = set()
    if reduced is not None:
        rows = reduced.get("rows")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("edge_id") is not None:
                    ids.add(str(row["edge_id"]))
    if answer is not None:
        answers = answer.get("answers")
        if isinstance(answers, list):
            for entry in answers:
                if not isinstance(entry, dict):
                    continue
                supporting = entry.get("supporting_edge_ids")
                if isinstance(supporting, list):
                    ids |= {str(e) for e in supporting}
    return frozenset(ids)


def resolvable_pmids(reduced: dict[str, Any] | None) -> frozenset[str]:
    # publications live only on the reduced rows, so a cell without
    # reduced_data.json has an empty PMID pool and every PMID it cites
    # counts as unsupported.
    if reduced is None:
        return frozenset()
    rows = reduced.get("rows")
    if not isinstance(rows, list):
        return frozenset()
    pmids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        publications = row.get("publications")
        if isinstance(publications, list):
            pmids |= {str(p) for p in publications}
    return frozenset(pmids)


def citation_audit(
    explanation: str | None,
    reduced: dict[str, Any] | None,
    answer: dict[str, Any] | None,
) -> tuple[int | None, int | None, int | None]:
    # (cited, resolved, unsupported). unsupported is the
    # faithfulness-adjacent number: a token the explanation presented
    # as evidence that is not in anything the model was given.
    if explanation is None:
        return None, None, None
    cited_edges, cited_pmids = parse_citations(explanation)
    real_edges = resolvable_edge_ids(reduced, answer)
    real_pmids = resolvable_pmids(reduced)
    cited = len(cited_edges) + len(cited_pmids)
    resolved = len(cited_edges & real_edges) + len(cited_pmids & real_pmids)
    return cited, resolved, cited - resolved


# ------------------------------------------------------------ one cell


def _meta_str(meta: dict[str, Any] | None, key: str) -> str | None:
    if meta is None:
        return None
    value = meta.get(key)
    return str(value) if isinstance(value, str) else None


def _cost_totals(cost: dict[str, Any] | None) -> tuple[float | None, int | None, int | None]:
    if cost is None:
        return None, None, None
    totals = cost.get("totals")
    if not isinstance(totals, dict):
        return None, None, None
    usd = totals.get("total_usd")
    tokens_in = totals.get("input_tokens")
    tokens_out = totals.get("output_tokens")
    return (
        float(usd) if isinstance(usd, int | float) else None,
        int(tokens_in) if isinstance(tokens_in, int) else None,
        int(tokens_out) if isinstance(tokens_out, int) else None,
    )


def score_cell(cell: CellPath, gold: GoldQuestion) -> CellScore:
    meta = read_json(cell.root / "meta.json")
    cost = read_json(cell.root / "cost.json")
    nameres = read_json(cell.root / "nameres.json")
    nodenorm = read_json(cell.root / "nodenorm.json")
    validation = read_json(cell.root / "validation.json")
    trapi_query = read_json(cell.root / "trapi_query.json")
    reduced = read_json(cell.root / "reduced_data.json")
    answer = read_json(cell.root / "answer.json")
    explanation = read_text(cell.root / "explanation.md")
    # reasoner_response.json is deliberately never opened — it is ~1 MB
    # per cell and everything we need from it is already flattened into
    # reduced_data.json and meta.json.

    elapsed = meta.get("elapsed_s") if meta else None
    cost_usd, tokens_in, tokens_out = _cost_totals(cost)
    predicates = trapi_predicates(trapi_query)
    picked = all_picked_curies(answer)
    hits, n_picked, hit_rate = verified_hit_counts(answer, gold.verified_curies)
    matched = answer_match(picked, gold.verified_curies)
    if gold.expects_abstain:
        # no gold answer exists, so "matched none of them" is not a miss.
        matched, hits, n_picked, hit_rate = None, None, None, None
    elif not gold.verified_curies:
        # a Translator question labelled only NeverShow / BadButForgivable:
        # nothing to hit, but what the model picked still counts.
        matched, hits, hit_rate = None, None, None
    cited, resolved, unsupported = citation_audit(explanation, reduced, answer)

    return CellScore(
        run_id=cell.run_id,
        model_id=cell.model_id,
        model_dir=cell.model_dir,
        condition=cell.condition,
        q_id=cell.q_id,
        status=_meta_str(meta, "status"),
        outcome=_meta_str(meta, "outcome"),
        elapsed_s=float(elapsed) if isinstance(elapsed, int | float) else None,
        cost_usd=cost_usd,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        entity_extract_ok=entity_extract_ok(nameres),
        nameres_top1_match=nameres_top1_match(nodenorm, gold.pinned_curie),
        valid_trapi=valid_trapi(validation),
        executed=query_executed(meta),
        predicate_match=predicate_match(predicates, gold.predicate),
        predicate_used=", ".join(predicates) if predicates is not None else None,
        answer_match=matched,
        verified_hits=hits,
        n_picked=n_picked,
        verified_hit_rate=hit_rate,
        citations_cited=cited,
        citations_resolved=resolved,
        unsupported_citation_count=unsupported,
        split=gold.split,
        phrasing=gold.phrasing,
        group_id=gold.group_id,
        nevershow_picked=nevershow_count(answer, gold.nevershow_curies),
        direction_match=direction_match(trapi_query, gold.gold_direction),
        abstained=abstained(meta, gold.expects_abstain),
    )


# ----------------------------------------------------------- discovery


def model_id_from_dir(model_dir: str) -> str:
    # folders are "<model_id>_<safe_slug>", e.g.
    # "m6_google_gemini-3.7-flash". only the id is recovered; the slug
    # had "/" and ":" replaced at write time and is kept verbatim as
    # model_dir rather than guessed back into its OpenRouter form.
    head, sep, _ = model_dir.partition("_")
    return head if sep else model_dir


def discover_cells(
    results_root: Path,
    gold_ids: frozenset[str],
    run_names: list[str] | None = None,
) -> list[CellPath]:
    # walks outputs/RUN_*/<model_dir>/<condition>/<q_id>/. anything whose
    # q_id is not a gold id is skipped, which drops <condition>/adhoc/ and
    # any older layout that wrote artifacts directly under the model
    # folder.
    if not results_root.is_dir():
        return []
    wanted = set(run_names) if run_names is not None else None
    cells: list[CellPath] = []
    for run_dir in sorted(results_root.glob("RUN_*")):
        if not run_dir.is_dir():
            continue
        if wanted is not None and run_dir.name not in wanted:
            continue
        for model_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            for condition_dir in sorted(p for p in model_dir.iterdir() if p.is_dir()):
                for cell_dir in sorted(p for p in condition_dir.iterdir() if p.is_dir()):
                    if cell_dir.name == ADHOC_Q_ID or cell_dir.name not in gold_ids:
                        continue
                    cells.append(CellPath(
                        run_id=run_dir.name.removeprefix("RUN_"),
                        model_id=model_id_from_dir(model_dir.name),
                        model_dir=model_dir.name,
                        q_id=cell_dir.name,
                        root=cell_dir,
                        condition=condition_dir.name,
                    ))
    return cells


# --------------------------------------------------------- aggregation


def _rate(values: list[bool | None]) -> tuple[float | None, int]:
    # rate over the cells where the metric is not None, plus how many
    # cells were excluded. an all-None column returns (None, n).
    present = [v for v in values if v is not None]
    n_none = len(values) - len(present)
    if not present:
        return None, n_none
    return sum(1 for v in present if v) / len(present), n_none


def _mean(values: list[float | None]) -> tuple[float | None, int]:
    present = [v for v in values if v is not None]
    n_none = len(values) - len(present)
    if not present:
        return None, n_none
    return sum(present) / len(present), n_none


def _model_sort_key(model_id: str) -> tuple[int, str]:
    # m1 .. m13 must sort numerically, not as strings ("m10" < "m2").
    digits = model_id[1:] if model_id.startswith("m") else ""
    return (int(digits), model_id) if digits.isdigit() else (10_000, model_id)


def summarize(rows: list[CellScore]) -> list[ModelSummary]:
    # one summary per (model, condition): an oracle or stage-swap run
    # measures a different system and must never be averaged into the
    # plain run of the same model.
    by_group: dict[tuple[str, str], list[CellScore]] = {}
    for row in rows:
        by_group.setdefault((row.model_id, row.condition), []).append(row)

    summaries: list[ModelSummary] = []
    for model_id, condition in sorted(
        by_group, key=lambda group: (_model_sort_key(group[0]), group[1]),
    ):
        cells = by_group[(model_id, condition)]
        n_cells = len(cells)
        nameres_rate, nameres_none = _rate([c.nameres_top1_match for c in cells])
        trapi_rate, trapi_none = _rate([c.valid_trapi for c in cells])
        executed_rate, executed_none = _rate([c.executed for c in cells])
        pred_rate, pred_none = _rate([c.predicate_match for c in cells])
        ans_rate, ans_none = _rate([c.answer_match for c in cells])
        hit_rate, hit_none = _mean([c.verified_hit_rate for c in cells])
        nevershow_rate, nevershow_none = _rate([
            None if c.nevershow_picked is None else c.nevershow_picked > 0 for c in cells
        ])
        direction_rate, direction_none = _rate([c.direction_match for c in cells])
        abstain_rate, abstain_none = _rate([c.abstained for c in cells])
        mean_cost, _ = _mean([c.cost_usd for c in cells])
        mean_elapsed, _ = _mean([c.elapsed_s for c in cells])
        summaries.append(ModelSummary(
            model_id=model_id,
            condition=condition,
            n_cells=n_cells,
            answered_rate=sum(1 for c in cells if c.outcome == "answered") / n_cells,
            entity_extract_ok_rate=sum(1 for c in cells if c.entity_extract_ok) / n_cells,
            nameres_top1_match_rate=nameres_rate,
            nameres_top1_match_n_none=nameres_none,
            valid_trapi_rate=trapi_rate,
            valid_trapi_n_none=trapi_none,
            executed_rate=executed_rate,
            executed_n_none=executed_none,
            predicate_match_rate=pred_rate,
            predicate_match_n_none=pred_none,
            answer_match_rate=ans_rate,
            answer_match_n_none=ans_none,
            mean_verified_hit_rate=hit_rate,
            verified_hit_rate_n_none=hit_none,
            total_citations_cited=sum(c.citations_cited or 0 for c in cells),
            total_citations_resolved=sum(c.citations_resolved or 0 for c in cells),
            total_unsupported_citations=sum(c.unsupported_citation_count or 0 for c in cells),
            nevershow_cell_rate=nevershow_rate,
            nevershow_cell_n_none=nevershow_none,
            direction_match_rate=direction_rate,
            direction_match_n_none=direction_none,
            abstain_rate=abstain_rate,
            abstain_n_none=abstain_none,
            mean_cost_usd=mean_cost,
            total_cost_usd=round(sum(c.cost_usd or 0.0 for c in cells), 6),
            mean_elapsed_s=mean_elapsed,
        ))
    return summaries


# --------------------------------------------------------------- output


def _csv_cell(value: object) -> str:
    # None becomes an empty field (not the string "None") so pandas
    # reads it as NaN and a spreadsheet leaves the cell blank. bools are
    # lower-cased for the same reason: "true"/"false" survive a round
    # trip through R and Python alike.
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def unique_stamp(out_dir: Path, stamp: str) -> str:
    # the scorer is fast enough that two invocations can land in the
    # same UTC second, and an existing artifact is never overwritten.
    # so a taken stamp grows a _2, _3, ... suffix.
    candidate = stamp
    attempt = 1
    while (
        (out_dir / f"scores_{candidate}.csv").exists()
        or (out_dir / f"summary_{candidate}.csv").exists()
    ):
        attempt += 1
        candidate = f"{stamp}_{attempt}"
    return candidate


def write_rows_csv(path: Path, rows: list[CellScore]) -> None:
    columns = [f.name for f in fields(CellScore)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_csv_cell(getattr(row, name)) for name in columns])


def write_summary_csv(path: Path, summaries: list[ModelSummary]) -> None:
    columns = [f.name for f in fields(ModelSummary)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for summary in summaries:
            writer.writerow([_csv_cell(getattr(summary, name)) for name in columns])


def _pct(value: float | None) -> str:
    # every table cell is rendered unpadded here and right-aligned by
    # the row f-string, so a None placeholder can never drift a column.
    return "—" if value is None else f"{value * 100:.1f}"


def _num(value: float | None, digits: int) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def render_summary_table(summaries: list[ModelSummary]) -> str:
    # plain text on purpose: the scorer is meant to be piped into a file
    # or a paper appendix, and rich markup would have to be stripped.
    header = (
        f"{'model':<6} {'cells':>5} {'answ%':>6} {'ent%':>6} {'norm%':>6} "
        f"{'trapi%':>6} {'exec%':>6} {'pred%':>6} {'ans%':>6} {'hit':>6} "
        f"{'cite':>5} {'unsup':>5} {'never%':>6} {'dir%':>6} {'abst%':>6} {'$/cell':>8} {'sec':>7}"
    )
    lines = [header, "-" * len(header)]
    for s in summaries:
        lines.append(
            f"{s.model_id:<6} {s.n_cells:>5} "
            f"{_pct(s.answered_rate):>6} "
            f"{_pct(s.entity_extract_ok_rate):>6} "
            f"{_pct(s.nameres_top1_match_rate):>6} "
            f"{_pct(s.valid_trapi_rate):>6} "
            f"{_pct(s.executed_rate):>6} "
            f"{_pct(s.predicate_match_rate):>6} "
            f"{_pct(s.answer_match_rate):>6} "
            f"{_num(s.mean_verified_hit_rate, 2):>6} "
            f"{s.total_citations_cited:>5} "
            f"{s.total_unsupported_citations:>5} "
            f"{_pct(s.nevershow_cell_rate):>6} "
            f"{_pct(s.direction_match_rate):>6} "
            f"{_pct(s.abstain_rate):>6} "
            f"{_num(s.mean_cost_usd, 4):>8} "
            f"{_num(s.mean_elapsed_s, 1):>7}"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ploverai-scorer",
        description="score saved PloverAI run artifacts against the gold set. offline, no network calls.",
    )
    p.add_argument(
        "--runs",
        nargs="+",
        default=None,
        help="run folder names to score (e.g. --runs RUN_2026-09-01T16-49-43Z). default: every RUN_* folder that contains at least one gold cell.",
    )
    p.add_argument(
        "--csv-dir",
        default=None,
        help="where to write scores_<stamp>.csv and summary_<stamp>.csv. default: <results>/scores/.",
    )
    p.add_argument(
        "--split",
        choices=("dev", "test"),
        default=None,
        help="score only one split, so dev and held-out numbers are never averaged together. default: both.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = load_config()
    gold = build_gold_index(cfg)
    if args.split is not None:
        gold = {q_id: g for q_id, g in gold.items() if g.split == args.split}

    cells = discover_cells(cfg.paths.results, frozenset(gold), args.runs)
    if not cells:
        target = " ".join(args.runs) if args.runs else str(cfg.paths.results)
        print(f"no gold cells found under {target}")
        return 1

    rows = [score_cell(cell, gold[cell.q_id]) for cell in cells]
    summaries = summarize(rows)

    out_dir = Path(args.csv_dir) if args.csv_dir else cfg.paths.results / "scores"
    stamp = unique_stamp(out_dir, utc_stamp())
    scores_path = out_dir / f"scores_{stamp}.csv"
    summary_path = out_dir / f"summary_{stamp}.csv"
    write_rows_csv(scores_path, rows)
    write_summary_csv(summary_path, summaries)

    scored_runs = sorted({row.run_id for row in rows})
    print(f"scored {len(rows)} gold cells from {len(scored_runs)} run(s): {', '.join(scored_runs)}")
    print()
    print(render_summary_table(summaries))
    print()
    print(f"rows    : {scores_path}")
    print(f"summary : {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
