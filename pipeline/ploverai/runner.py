# runner.py — CLI entry point for the v15 benchmark. parses args,
# loads pipeline/.env, sets up the rich console + log file, walks
# the (model x question) grid (v15 is grounded-only), and prints a
# summary table. the per-question logic is in pipeline.py — this
# file is glue and UX.

from __future__ import annotations

# argparse: stdlib. simple CLI parser; we deliberately don't pull in
# click/typer because there are only six flags and one positional intent.
import argparse

# sys: stdlib. process exit code. we exit(1) when any run fails so CI
# / shells can detect regression without grepping the summary table.
import sys

# time.perf_counter: stdlib. wall clock for the whole-run summary line.
import time

# pathlib.Path: stdlib. all paths are Path objects; the only places that
# touch raw strings are .env loading and CLI flags.
from pathlib import Path

# typing.Any: stdlib. gold question records are nested dicts.
from typing import Any

# rich.panel / progress / table: pretty terminal. one shared Console
# (imported from logging_setup) so the progress bar doesn't fight the
# log lines that stream above it.
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

# python-dotenv: loads pipeline/.env into os.environ. we point it at an
# explicit file path (next to this script) instead of relying on cwd
# so `python -m pipeline.runner` works from anywhere.
from dotenv import load_dotenv

# our config module — the YAML reader. ModelSpec lookup is in there.
from .config import Config, ModelSpec, load_config, load_questions

# our shared rich Console + per-run logger factory + UTC stamp +
# log-path helper (so the banner can show the eventual log path even
# on --dry-run, when the logger isn't actually wired up).
from .logging_setup import console, log_path_for, setup_logger, utc_stamp

# OpenRouter and PloverDB clients. one of each is created here and
# passed down into the per-question runners — connection pooling is
# why we don't construct them per question.
from .openrouter_client import OpenRouterClient
from .plover_client import PloverClient

# RENCI clients used at Stages 3, 6, and 12.
from .nameres_client import NameResClient
# BMT (Biolink Model Toolkit) wrappers: derive a "loose neighborhood"
# of biolink categories for the Stage 3 NameRes filter. see
# biolink_helper.py for the full story.
from .biolink_helper import (
    build_neighborhood_map as build_biolink_neighborhood_map,
    make_toolkit as make_biolink_toolkit,
)
from .nodenorm_client import NodeNormClient

# the per-question runner. it owns the 8-stage grounded logic; we
# just call it once per (model, question) and aggregate the result.
from .pipeline import QuestionResult, run_grounded

# trace: builds the per-run folder layout and writes run.json. we do
# the writes here in runner.py because the runner is the only thing
# that knows the full plan (which models ran together).
from .trace import QuestionPaths, make_run_dir, make_run_root, write_json


# pipeline/.env is the secret store. .env.example is committed; .env is
# gitignored. it lives at the pipeline root (next to config.yaml), so
# from this file (pipeline/ploverai/runner.py) we go up two parents.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ploverai-runner",
        description="run the PloverAI v15 grounded pipeline.",
    )
    # ----- model selection (one of these or default) -----
    p.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="model ids to run (e.g. --models m1 m5). default: all 8 from config.yaml.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="single model id (e.g. --model m5). shorthand for --models <one>; mutually exclusive with --models.",
    )
    # ----- question selection (one of these or default) -----
    p.add_argument(
        "--questions",
        nargs="+",
        default=None,
        help="gold question ids to run (e.g. --questions q1 q2). default: all 10 gold questions.",
    )
    p.add_argument(
        "--question",
        default=None,
        help='free-form NL question (e.g. --question "What treats Crohn\'s disease?"). bypasses the gold set entirely; mutually exclusive with --questions. defaults --model to m5 if no model is specified, so an ad-hoc query is one short command.',
    )
    # ----- shortcuts / dry-run -----
    p.add_argument(
        "--smoke",
        action="store_true",
        help="shortcut: run m5 (Haiku) on gold q1 only. ignores other flags.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and exit. no API calls, no folders created.",
    )
    return p.parse_args()


def _select_models(cfg: Config, ids: list[str] | None) -> list[ModelSpec]:
    if ids is None:
        return list(cfg.models)
    # preserve user-given order; raise loud if any id is wrong (the
    # KeyError from cfg.model() bubbles out of argparse cleanly).
    return [cfg.model(i) for i in ids]


def _select_questions(
    qs: list[dict[str, Any]],
    ids: list[str] | None,
) -> list[dict[str, Any]]:
    if ids is None:
        return list(qs)
    by_id = {q["id"]: q for q in qs}
    out: list[dict[str, Any]] = []
    for i in ids:
        if i not in by_id:
            raise SystemExit(f"unknown question id: {i}. known: {sorted(by_id)}")
        out.append(by_id[i])
    return out


def _banner(
    cfg: Config,
    run_id: str,
    log_path: Path,
    models: list[ModelSpec],
    questions: list[dict[str, Any]],
    *,
    adhoc: bool,
) -> None:
    # one-page summary printed before any work happens. catches the
    # "wait, I didn't mean to run all 8 models" footgun before money
    # leaves the OpenRouter account.
    n_runs = len(models) * len(questions)
    if adhoc:
        # ad-hoc mode: there's exactly one synthetic question. show its
        # text inline so the user can spot a typo before any API call.
        q_line = f"ad-hoc: {questions[0]['nl_question']!r}"
    else:
        q_line = ", ".join(q["id"] for q in questions)
    body = (
        f"[bold]run id[/bold]   : {run_id}\n"
        f"[bold]log file[/bold] : {log_path}\n"
        f"[bold]results[/bold]  : {cfg.paths.results}\n"
        f"[bold]models[/bold]   : {', '.join(m.id + '=' + m.slug for m in models)}\n"
        f"[bold]questions[/bold]: {q_line}\n"
        f"[bold]total runs[/bold]: {n_runs}  (grounded only)\n"
    )
    console.print(Panel(body, title="PloverAI v15 — pipeline runner", border_style="cyan"))


def _summary_table(
    results: list[tuple[str, str, QuestionResult]],
) -> Table:
    # results is a list of (model_id, q_id, QuestionResult).
    # status (runtime) and outcome (semantic) are shown side by side so
    # the human can spot a "ran cleanly but produced no answer" run at
    # a glance. status=ok + outcome=no_results is exactly that case.
    t = Table(title="run summary", show_lines=False)
    t.add_column("model")
    t.add_column("question")
    t.add_column("status")
    t.add_column("outcome")
    t.add_column("plover/picks", justify="right")
    t.add_column("tokens (in/out)", justify="right")
    t.add_column("cost USD", justify="right")
    t.add_column("seconds", justify="right")
    for m_id, q_id, r in results:
        ti, to = r.cost_total_tokens
        status_color = "green" if r.status == "ok" else "red"
        outcome_color = {
            "answered":         "green",
            "no_results":       "yellow",
            "no_answer_picked": "yellow",
        }.get(r.outcome or "", "dim")
        outcome_text = r.outcome if r.outcome else "—"
        counts = (
            f"{r.plover_n_results}/{r.answers_n_picked}"
            if r.plover_n_results >= 0 else "—"
        )
        t.add_row(
            m_id,
            q_id,
            f"[{status_color}]{r.status}[/]",
            f"[{outcome_color}]{outcome_text}[/]",
            counts,
            f"{ti}/{to}",
            f"${r.cost_total_usd:.4f}",
            f"{r.elapsed_s:.1f}",
        )
    return t


def main() -> int:
    # load secrets from pipeline/.env (if it exists). we don't crash if
    # it's missing — a CI run might inject env vars directly.
    load_dotenv(dotenv_path=ENV_FILE)

    args = _parse_args()
    cfg = load_config()
    questions = load_questions(cfg)

    # ----- mutual-exclusion guards (loud failure at the CLI boundary) -----
    if args.model and args.models:
        raise SystemExit("--model and --models are mutually exclusive.")
    if args.question and args.questions:
        raise SystemExit("--question and --questions are mutually exclusive.")

    # ----- shortcuts / aliases -----
    # smoke trumps everything else: m5 + gold q1, exactly.
    if args.smoke:
        args.models = ["m5"]
        args.questions = ["q1"]
        args.model = None
        args.question = None
    # singular --model is just --models with one entry.
    if args.model:
        args.models = [args.model]
    # ad-hoc question default: a free-form question only makes sense
    # paired with one model. if the user didn't pick one, use the
    # cheapest+fastest (m5/Haiku) so ad-hoc is one short command.
    adhoc = args.question is not None
    if adhoc and args.models is None:
        args.models = ["m5"]

    chosen_models = _select_models(cfg, args.models)

    # ----- question list -----
    chosen_qs: list[dict[str, Any]]
    if adhoc:
        # synthetic, gold-free record. the pipeline only reads
        # `q["nl_question"]` and `q["id"]`, so a tiny dict is enough.
        # adhoc=True is a marker for any future analysis scripts that
        # need to skip these runs when computing gold-based metrics.
        chosen_qs = [{
            "id": "adhoc",
            "nl_question": args.question,
            "adhoc": True,
        }]
    else:
        chosen_qs = _select_questions(questions, args.questions)

    run_id = utc_stamp()
    # log file path is decided up front, even on --dry-run, so the
    # banner can show it. on --dry-run we don't actually create the
    # file or its parent folder; setup_logger() does that for real
    # runs only. layout: logs/RUN_<run_id>/run.log.
    log_path = log_path_for(cfg.paths.logs, run_id)

    _banner(cfg, run_id, log_path, chosen_models, chosen_qs, adhoc=adhoc)

    if args.dry_run:
        console.print("[yellow]dry-run: exiting without doing any work.[/]")
        return 0

    logger, log_path = setup_logger(cfg.paths.logs, run_id)
    logger.info(f"[bold]run started[/]  run_id={run_id}")

    # one client of each kind for the whole run. this matters at the
    # 160-instance scale: per-question construction would burn a TCP
    # handshake and (for OpenRouter) a fresh rate-limit bucket per call.
    llm = OpenRouterClient(cfg, logger)
    plover = PloverClient(cfg, logger)
    # RENCI clients are stateless from our side; they hold an httpx
    # pool and a logger. used at Stages 3, 5, 6, 12.
    nameres = NameResClient(cfg, logger)
    nodenorm = NodeNormClient(cfg, logger)

    # cache PloverDB's meta_knowledge_graph once per run. Stage 8 uses
    # the (subject_cat, object_cat) -> [valid predicates] index to
    # constrain its predicate choice (fix B: predicate grounding).
    # ~6 MB JSON, fetched in ~1s; well worth it across the 80
    # grounded calls of a full benchmark.
    predicate_index: dict[tuple[str, str], list[str]] = {}
    try:
        meta_kg = plover.fetch_meta_kg()
        for edge in meta_kg.get("edges") or []:
            s = edge.get("subject")
            o = edge.get("object")
            p = edge.get("predicate")
            if s and o and p:
                predicate_index.setdefault((s, o), []).append(p)
        for k in list(predicate_index.keys()):
            predicate_index[k] = sorted(set(predicate_index[k]))
        logger.info(
            f"meta_KG cached: {sum(len(v) for v in predicate_index.values())} "
            f"(cat-pair, predicate) entries indexed"
        )
    except Exception as e:
        # non-fatal — the pipeline still runs without the meta_KG
        # constraint, just unconstrained (pre-fix-B) behaviour.
        logger.warning(f"could not fetch meta_KG at start-up: {e}")

    # BMT-derived loose-neighborhood map for Stage 3's biolink_type
    # filter. computed once per run; passed to every run_grounded
    # invocation below. failure here is non-fatal — Stage 3 falls back
    # to the strict single-category filter the same way it did before.
    available_categories_list = sorted({
        c
        for (s, o) in predicate_index
        for c in (s, o)
    })
    biolink_neighborhoods: dict[str, list[str]] = {}
    try:
        toolkit = make_biolink_toolkit()
        biolink_neighborhoods = build_biolink_neighborhood_map(
            available_categories_list, toolkit, logger,
        )
        logger.info(
            f"biolink neighborhoods cached: {len(biolink_neighborhoods)} categories"
        )
    except Exception as e:
        logger.warning(f"could not build biolink neighborhoods at start-up: {e}")

    results: list[tuple[str, str, QuestionResult]] = []
    total_units = len(chosen_models) * len(chosen_qs)

    # rich.progress shares our Console, so the bar stays at the bottom
    # while the stage-by-stage log lines stream above it.
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )

    grand_t0 = time.perf_counter()
    grand_cost = 0.0

    # one parent folder for the whole invocation. every model in this
    # run ends up as a sibling under it. format: outputs/RUN_<ts>/.
    run_dir = make_run_dir(cfg.paths.results, run_id)
    # top-level run.json describes the whole invocation: which models
    # ran together, which questions, with what params. lives at
    # outputs/RUN_<ts>/run.json (a sibling of the per-model folders).
    write_json(run_dir / "run.json", {
        "run_id": run_id,
        "started_utc": run_id,
        "condition": "grounded",
        "models": [
            {
                "id": m.id,
                "slug": m.slug,
                "tier": m.tier,
                "provider": m.provider,
            }
            for m in chosen_models
        ],
        "question_ids": [q["id"] for q in chosen_qs],
        "endpoints": {
            "ploverdb_query": cfg.endpoints.ploverdb,
            "openrouter": cfg.endpoints.openrouter,
        },
        "generation": {
            "temperature": cfg.generation.temperature,
            "max_tokens": cfg.generation.max_tokens,
        },
    })

    try:
        with progress:
            task = progress.add_task(
                "[cyan]running v15 benchmark[/]",
                total=total_units,
            )
            for model in chosen_models:
                run_root = make_run_root(run_dir, model.id, model.slug)
                # write a per-model run.json describing this model's
                # subfolder. if the run dies halfway, we still know
                # what the runner intended to do here.
                write_json(run_root.run_meta, {
                    "run_id": run_id,
                    "model_id": model.id,
                    "model_slug": model.slug,
                    "tier": model.tier,
                    "provider": model.provider,
                    "condition": "grounded",
                    "question_ids": [q["id"] for q in chosen_qs],
                    "endpoints": {
                        "ploverdb_query": cfg.endpoints.ploverdb,
                        "openrouter": cfg.endpoints.openrouter,
                    },
                    "generation": {
                        "temperature": cfg.generation.temperature,
                        "max_tokens": cfg.generation.max_tokens,
                    },
                })

                for q in chosen_qs:
                    # one folder per question per (run, model). path
                    # is outputs/RUN_<ts>/<model>/<q_id>/.
                    qp = QuestionPaths.under(run_root.root, q["id"])
                    progress.update(
                        task,
                        description=f"[cyan]{model.id} • {q['id']}[/]",
                    )
                    r = run_grounded(
                        cfg=cfg, model=model, q=q, qp=qp,
                        llm=llm, nameres=nameres, nodenorm=nodenorm,
                        plover=plover, logger=logger,
                        predicate_index=predicate_index,
                        biolink_neighborhoods=biolink_neighborhoods,
                    )
                    grand_cost += r.cost_total_usd
                    results.append((model.id, q["id"], r))
                    progress.advance(task)
    finally:
        llm.close()
        plover.close()
        nameres.close()
        nodenorm.close()

    elapsed = time.perf_counter() - grand_t0
    console.print(_summary_table(results))
    console.print(
        f"[bold]done[/]  runs={len(results)}  "
        f"total_cost=${grand_cost:.4f}  elapsed={elapsed:.1f}s  "
        f"log={log_path}"
    )
    logger.info(
        f"[bold]run finished[/]  runs={len(results)}  "
        f"total_cost=${grand_cost:.4f}  elapsed={elapsed:.1f}s"
    )

    n_fail = sum(1 for _, _, r in results if r.status != "ok")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
