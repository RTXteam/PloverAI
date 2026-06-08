# golden_eval.py — run gold questions through the full grounded pipeline
# with a chosen model and score each result against the gold
# verified_answers. drives a cost-conscious one-at-a-time loop: with
# --stop-on-fail (default) it halts at the first question that does not
# clear the objective floor, so we never pay to run all 10 to discover #3
# broke.
#
# scoring philosophy: the gold `verified_answers` is the FLOOR (manually
# verified known-correct answers), not the ceiling. recall of that set is
# the objective number; EXTRA answers the pipeline returns are surfaced for
# human review, NOT counted as errors (they may be additional correct
# answers). entity + predicate correctness are the Q1 (NL->TRAPI) signals;
# explanation.md faithfulness is a human read.
#
# this hits the network and spends real OpenRouter credits (~6 LLM calls
# per question), so it is a CLI tool, not a CI test. invoke from pipeline/:
#   python -m code.golden_eval --model m6 --questions q1
#   python -m code.golden_eval --model m6 --no-stop-on-fail   # final all-10

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .biolink_helper import (
    build_neighborhood_map as build_biolink_neighborhood_map,
    make_toolkit as make_biolink_toolkit,
)
from .config import Config, load_config
from .logging_setup import console, setup_logger, utc_stamp
from .nameres_client import NameResClient
from .nodenorm_client import NodeNormClient, NodeNormError
from .openrouter_client import OpenRouterClient
from .pipeline import QuestionResult, run_grounded
from .plover_client import PloverClient, PloverError
from .trace import QuestionPaths, make_run_dir, make_run_root, write_json

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# the single-shot answer step still caps at 5 picks (the iterative redesign
# lifts this). when the cap is hit AND gold is still missing, we flag the
# run as "capped" rather than failed — the miss may be the cap, not a bug.
SINGLE_SHOT_ANSWER_CAP = 5


@dataclass
class Runtime:
    llm: OpenRouterClient
    plover: PloverClient
    nameres: NameResClient
    nodenorm: NodeNormClient
    predicate_index: dict[tuple[str, str], list[str]]
    biolink_neighborhoods: dict[str, list[str]]

    def close(self) -> None:
        self.llm.close()
        self.plover.close()
        self.nameres.close()
        self.nodenorm.close()


@dataclass
class Score:
    qid: str
    nl_question: str
    status: str
    outcome: str | None
    cost_usd: float
    n_picked: int
    entity_expected: str | None
    entity_pinned: str | None
    entity_ok: bool
    predicate_expected: str | None
    predicate_built: list[str]
    predicate_ok: bool
    gold_curies: list[str]
    matched: list[str]
    missing: list[str]
    extras: list[str]
    recall: float
    capped: bool
    passes_floor: bool
    explanation_path: Path


def build_runtime(cfg: Config, logger: logging.Logger) -> Runtime:
    # mirrors the per-run setup in runner.main(): one client of each kind,
    # the cached meta_KG predicate index (Stage 8 constraint), and the BMT
    # loose-neighborhood map (Stage 3 filter). kept here so the eval tool
    # does not reach into the benchmark runner's CLI internals.
    llm = OpenRouterClient(cfg, logger)
    plover = PloverClient(cfg, logger)
    nameres = NameResClient(cfg, logger)
    nodenorm = NodeNormClient(cfg, logger)

    predicate_index: dict[tuple[str, str], list[str]] = {}
    try:
        meta_kg = plover.fetch_meta_kg()
        for edge in meta_kg.get("edges") or []:
            subject, obj, predicate = edge.get("subject"), edge.get("object"), edge.get("predicate")
            if subject and obj and predicate:
                predicate_index.setdefault((subject, obj), []).append(predicate)
        for pair in list(predicate_index.keys()):
            predicate_index[pair] = sorted(set(predicate_index[pair]))
    except PloverError as e:
        logger.warning(f"could not fetch meta_KG: {e} (Stage 8 runs unconstrained)")

    available_categories = sorted({c for pair in predicate_index for c in pair})
    biolink_neighborhoods: dict[str, list[str]] = {}
    try:
        toolkit = make_biolink_toolkit()
        biolink_neighborhoods = build_biolink_neighborhood_map(
            available_categories, toolkit, logger,
        )
    except Exception as e:  # non-fatal startup cache (mirrors runner); Stage 3 degrades to strict
        logger.warning(f"could not build biolink neighborhoods: {e}")

    return Runtime(llm, plover, nameres, nodenorm, predicate_index, biolink_neighborhoods)


def _load_json(path: Path) -> dict[str, Any]:
    # artifacts may be absent when the run failed before that stage; treat
    # a missing/unreadable file as empty rather than crashing the scorer.
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(loaded, dict):
        return {str(k): v for k, v in loaded.items()}
    return {}


def _load_gold(cfg: Config, qid: str) -> dict[str, Any]:
    loaded: Any = json.loads((cfg.paths.questions / f"{qid}.json").read_text())
    if not isinstance(loaded, dict):
        raise TypeError(f"gold file {qid}.json is not a JSON object")
    return {str(k): v for k, v in loaded.items()}


def _predicates_in_query(trapi_msg: dict[str, Any]) -> list[str]:
    query_graph = trapi_msg.get("message", {}).get("query_graph", {})
    edges = query_graph.get("edges", {}) if isinstance(query_graph, dict) else {}
    out: list[str] = []
    if isinstance(edges, dict):
        for edge in edges.values():
            if not isinstance(edge, dict):
                continue
            predicates = edge.get("predicates")
            if isinstance(predicates, list):
                out.extend(str(p) for p in predicates)
            elif isinstance(edge.get("predicate"), str):
                out.append(str(edge["predicate"]))
    return out


def _picked_curies(answer_doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for answer in answer_doc.get("answers", []) or []:
        if isinstance(answer, dict) and isinstance(answer.get("curie"), str):
            out.append(answer["curie"])
    return out


def _canonical_map(
    nodenorm: NodeNormClient, curies: list[str], logger: logging.Logger,
) -> dict[str, str]:
    # canonicalise every curie so gold and picked are compared on the same
    # footing (the gold curies are already canonical, but normalising both
    # is robust to equivalent-identifier drift). falls back to identity on
    # a NodeNorm error rather than silently dropping curies.
    unique = [c for c in dict.fromkeys(curies) if c]
    if not unique:
        return {}
    try:
        result = nodenorm.normalize(unique)
    except NodeNormError as e:
        logger.warning(f"NodeNorm failed during scoring; comparing raw curies: {e}")
        return {c: c for c in unique}
    return {c: (result.canonical.get(c) or c) for c in unique}


def _score(
    qid: str,
    gold: dict[str, Any],
    qp: QuestionPaths,
    result: QuestionResult,
    nodenorm: NodeNormClient,
    logger: logging.Logger,
) -> Score:
    entity_expected = (gold.get("pinned_entity") or {}).get("curie")
    predicate_expected = gold.get("predicate")
    gold_curies = [
        va["curie"]
        for va in (gold.get("verified_answers") or [])
        if isinstance(va, dict) and va.get("curie")
    ]

    pinned = _load_json(qp.nodenorm).get("pinned") or {}
    entity_pinned = pinned.get("canonical_curie") if isinstance(pinned, dict) else None
    predicate_built = _predicates_in_query(_load_json(qp.trapi_query))
    picked = _picked_curies(_load_json(qp.answer))

    canon = _canonical_map(
        nodenorm,
        [*gold_curies, *picked, entity_expected or "", entity_pinned or ""],
        logger,
    )

    def canon_set(items: list[str]) -> set[str]:
        return {canon.get(x, x) for x in items}

    def canon_one(curie: str | None) -> str | None:
        return canon.get(curie, curie) if curie else None

    gold_set = canon_set(gold_curies)
    picked_set = canon_set(picked)
    matched = sorted(gold_set & picked_set)
    missing = sorted(gold_set - picked_set)
    extras = sorted(picked_set - gold_set)
    recall = len(matched) / len(gold_set) if gold_set else 0.0

    entity_ok = bool(entity_pinned) and canon_one(entity_pinned) == canon_one(entity_expected)
    predicate_ok = bool(predicate_expected) and predicate_expected in predicate_built
    capped = len(picked) >= SINGLE_SHOT_ANSWER_CAP and bool(missing)
    # objective floor: the run succeeded, pinned the right entity, built the
    # right predicate, and found at least one gold answer. the final
    # hold/advance call layers human judgement (faithfulness, extras) on top.
    passes_floor = (
        result.status == "ok" and entity_ok and predicate_ok and len(matched) > 0
    )

    return Score(
        qid=qid,
        nl_question=gold.get("nl_question", ""),
        status=result.status,
        outcome=result.outcome,
        cost_usd=result.cost_total_usd,
        n_picked=len(picked),
        entity_expected=entity_expected,
        entity_pinned=entity_pinned,
        entity_ok=entity_ok,
        predicate_expected=predicate_expected,
        predicate_built=predicate_built,
        predicate_ok=predicate_ok,
        gold_curies=gold_curies,
        matched=matched,
        missing=missing,
        extras=extras,
        recall=recall,
        capped=capped,
        passes_floor=passes_floor,
        explanation_path=qp.explanation,
    )


def _print_verdict(score: Score) -> None:
    ok = "[green]OK[/]"
    bad = "[red]WRONG[/]"
    console.rule(f"[bold]{score.qid}[/]  {score.nl_question}")
    console.print(
        f"status=[bold]{score.status}[/]  outcome={score.outcome}  "
        f"picks={score.n_picked}  cost=${score.cost_usd:.4f}"
    )
    console.print(
        f"Q1 entity     {ok if score.entity_ok else bad}  "
        f"expected={score.entity_expected}  pinned={score.entity_pinned}"
    )
    console.print(
        f"Q1 predicate  {ok if score.predicate_ok else bad}  "
        f"expected={score.predicate_expected}  built={score.predicate_built}"
    )
    console.print(
        f"Q2 recall     [bold]{len(score.matched)}/{len(score.gold_curies)}[/] "
        f"gold found ({score.recall:.0%})"
    )
    console.print(f"   matched: {score.matched or '—'}")
    console.print(f"   [yellow]MISSING[/]: {score.missing or '—'}")
    console.print(f"   extras (review, not errors): {score.extras or '—'}")
    if score.capped:
        console.print(
            "   [yellow]capped[/]: 5-pick limit hit with gold still missing — "
            "likely the single-shot answer cap (lifted by the iterative redesign)"
        )
    console.print(f"   explanation: {score.explanation_path}")
    console.print(
        f"floor: {'[green]PASS[/]' if score.passes_floor else '[red]HOLD[/]'} "
        "(advance decision adds a human read of faithfulness + extras)"
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ploverai-golden-eval",
        description="run gold questions through the full pipeline and score vs gold.",
    )
    p.add_argument("--model", default="m6", help="model id from config.yaml (default: m6, Gemini Flash).")
    p.add_argument(
        "--questions", nargs="+", default=None,
        help="gold question ids in run order (e.g. --questions q1 q2). default: all 10.",
    )
    p.add_argument(
        "--stop-on-fail", action=argparse.BooleanOptionalAction, default=True,
        help="stop at the first question that fails the objective floor (default: on).",
    )
    p.add_argument(
        "--iterative", action="store_true",
        help="enable iterative chunked answer-picking (overrides config default-off).",
    )
    return p.parse_args()


def main() -> int:
    load_dotenv(dotenv_path=ENV_FILE)
    args = _parse_args()
    cfg = load_config()
    # --iterative flips the (frozen) config's default-off iterative flag so a
    # single command can A/B single-shot vs iterative without editing yaml.
    if args.iterative:
        cfg = replace(
            cfg, stage11_iterative=replace(cfg.stage11_iterative, enabled=True),
        )
    question_ids = args.questions or [f"q{i}" for i in range(1, 11)]

    run_id = utc_stamp()
    logger, log_path = setup_logger(cfg.paths.logs, run_id)
    runtime = build_runtime(cfg, logger)
    model = cfg.model(args.model)
    # chunk budget is sized from the MANUAL context_window in config.yaml.
    budget = int(model.context_window * cfg.stage11_iterative.context_fraction)
    logger.info(
        f"golden_eval started  run_id={run_id}  model={model.id}  "
        f"context_window={model.context_window}  chunk_budget={budget}"
    )
    console.print(
        f"[bold]golden_eval[/]  model=[cyan]{model.id}[/] ({model.slug})  "
        f"context_window={model.context_window}  iterative={cfg.stage11_iterative.enabled}  "
        f"questions={question_ids}  stop_on_fail={args.stop_on_fail}  log={log_path}"
    )

    run_root = make_run_root(make_run_dir(cfg.paths.results, run_id), model.id, model.slug)
    # record the resolved context window + iterative config for reproducibility.
    write_json(run_root.run_meta, {
        "run_id": run_id,
        "model": {"id": model.id, "slug": model.slug, "context_window": model.context_window},
        "iterative": {
            "enabled": cfg.stage11_iterative.enabled,
            "context_fraction": cfg.stage11_iterative.context_fraction,
            "answer_target": cfg.stage11_iterative.answer_target,
            "max_chunks": cfg.stage11_iterative.max_chunks,
            "chunk_budget": budget,
        },
    })

    scores: list[Score] = []
    try:
        for qid in question_ids:
            gold = _load_gold(cfg, qid)
            qp = QuestionPaths.under(run_root.root, qid)
            result = run_grounded(
                cfg=cfg, model=model, q={"id": qid, "nl_question": gold["nl_question"]},
                qp=qp, llm=runtime.llm, nameres=runtime.nameres, nodenorm=runtime.nodenorm,
                plover=runtime.plover, logger=logger,
                predicate_index=runtime.predicate_index,
                biolink_neighborhoods=runtime.biolink_neighborhoods,
            )
            score = _score(qid, gold, qp, result, runtime.nodenorm, logger)
            scores.append(score)
            _print_verdict(score)
            if args.stop_on_fail and not score.passes_floor:
                console.print(
                    f"[red]stopping[/]: {qid} did not clear the floor. "
                    "fix, then re-run this question before advancing."
                )
                break
    finally:
        runtime.close()

    n_pass = sum(1 for s in scores if s.passes_floor)
    console.rule("[bold]summary[/]")
    for s in scores:
        mark = "[green]PASS[/]" if s.passes_floor else "[red]HOLD[/]"
        console.print(
            f"  {s.qid:>4}  {mark}  recall={len(s.matched)}/{len(s.gold_curies)}  "
            f"entity={'ok' if s.entity_ok else 'WRONG'}  "
            f"predicate={'ok' if s.predicate_ok else 'WRONG'}  status={s.status}"
        )
    total_cost = sum(s.cost_usd for s in scores)
    console.print(f"[bold]{n_pass}/{len(scores)} cleared the floor[/]  total_cost=${total_cost:.4f}")
    return 0 if n_pass == len(scores) and scores else 1


if __name__ == "__main__":
    raise SystemExit(main())
