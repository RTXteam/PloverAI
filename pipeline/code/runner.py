# runner.py — CLI entry point for the benchmark. parses args,
# loads pipeline/.env, sets up the rich console + log file, walks
# the (model x question) grid for one condition, and prints a
# summary table. the per-question logic is in pipeline.py — this
# file is glue and UX.

from __future__ import annotations

# argparse: stdlib. simple CLI parser; we deliberately don't pull in
# click/typer because the flags are few and flat (no subcommands).
import argparse

# sys: stdlib. process exit code. we exit(1) when any run fails so CI
# / shells can detect regression without grepping the summary table.
import sys

# time.perf_counter: stdlib. wall clock for the whole-run summary line.
import time

# uuid: stdlib. a short random suffix keeps run ids unique across
# runners started in the same second (see run_id in main).
import uuid

# pathlib.Path: stdlib. all paths are Path objects; the only places that
# touch raw strings are .env loading and CLI flags.
from pathlib import Path

# typing.Any: stdlib. gold question records are nested dicts.
from typing import Any

# collections.abc.Callable: stdlib. service_versions takes its HTTP GET
# as an argument so the test can hand it canned replies.
from collections.abc import Callable

# httpx: third-party, same client the service wrappers use. only the
# three version probes at start-up call it directly.
import httpx

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
from .config import Config, Endpoints, ModelSpec, load_benchmark_questions, load_config

# our shared rich Console + per-run logger factory + UTC stamp +
# log-path helper (so the banner can show the eventual log path even
# on --dry-run, when the logger isn't actually wired up).
from .logging_setup import console, log_path_for, setup_logger, utc_stamp

# OpenRouter and Retriever clients. one of each is created here and
# passed down into the per-question runners — connection pooling is
# why we don't construct them per question.
from .openrouter_client import OpenRouterClient
from .retriever_client import (
    RetrieverClient,
    build_category_set,
    build_predicate_index,
    openapi_summary,
)

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

# PubTatorClient: Stage 14's PMID co-mention check (lookup condition
# only). the FastAPI service enables it, so the benchmark does too;
# failures degrade to "not verified" inside the pipeline and never fail
# a cell.
from .pubtator_client import PubTatorClient

# AraxClient: ARAX mode's Stage 10 service (runner --reasoner arax).
from .arax_client import AraxClient

# the per-question runner. it owns the stage-by-stage grounded logic;
# we just call it once per (model, question) and aggregate the result.
from .pipeline import (
    LLM_STAGES,
    ORACLE_MODES,
    REASONERS,
    STATUS_CRASHED,
    QuestionResult,
    run_grounded,
)

# trace: builds the per-run folder layout and writes run.json. we do
# the writes here in runner.py because the runner is the only thing
# that knows the full plan (which models ran together).
from .trace import QuestionPaths, make_run_dir, make_run_root, write_json


# pipeline/.env is the secret store. .env.example is committed; .env is
# gitignored. it lives at the pipeline root (next to config.yaml), so
# from this file (pipeline/code/runner.py) we go up two parents.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ploverai-runner",
        description="run the PloverAI grounded pipeline.",
    )
    # ----- model selection (one of these or default) -----
    p.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="model ids to run (e.g. --models m1 m5). default: every benchmark-tier model in config.yaml (dev tier excluded).",
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
        help="question ids to run (e.g. --questions q1 tr01t). default: every benchmark question (curated gold + converted Translator tests, both phrasings).",
    )
    p.add_argument(
        "--split",
        choices=("dev", "test"),
        default=None,
        help="run only one split: dev = the curated questions prompts may be tuned on; test = the held-out set (Translator tests + the newer curated questions). default: both.",
    )
    p.add_argument(
        "--question",
        default=None,
        help='free-form NL question (e.g. --question "What treats Crohn\'s disease?"). bypasses the gold set entirely; mutually exclusive with --questions. defaults --model to m7 (the pilot workhorse) so an ad-hoc query is one short command; use --model m0 for the free dev-tier model.',
    )
    # ----- experimental conditions -----
    p.add_argument(
        "--oracle",
        choices=ORACLE_MODES,
        default=None,
        help="hand the pipeline a gold answer: entity = the gold pinned entity replaces Stages 2-7; query = the gold query graph also replaces Stage 8. gold questions only.",
    )
    p.add_argument(
        "--stage-model",
        action="append",
        default=None,
        metavar="STAGE=MODEL_ID",
        help=f"run one LLM stage on another model, e.g. --stage-model answer_pick=m1 (repeatable). stages: {', '.join(LLM_STAGES)}.",
    )
    p.add_argument(
        "--reasoner",
        choices=REASONERS,
        default="arax",
        help="who answers the query at Stage 10: arax = ARAX, multi-hop reasoning over the Translator Tier 0 graph with reasoning paths (default); lookup = one-hop Tier 0 lookup on Retriever, the LLM reasons.",
    )
    # ----- shortcuts / dry-run -----
    p.add_argument(
        "--smoke",
        action="store_true",
        help="shortcut: run m7 (the pilot workhorse) on gold q1 only. ignores other flags.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and exit. no API calls, no folders created.",
    )
    return p.parse_args()


PILOT_MODEL_ID = "m7"


def parse_stage_models(pairs: list[str] | None) -> dict[str, str]:
    # "answer_pick=m1" -> {"answer_pick": "m1"}. rejected loudly at the
    # CLI boundary: an unknown stage would otherwise be silently ignored
    # and the run would measure the plain condition under a swap label.
    swaps: dict[str, str] = {}
    for pair in pairs or []:
        stage, sep, model_id = pair.partition("=")
        if not sep or not model_id:
            raise SystemExit(f"--stage-model expects STAGE=MODEL_ID, got {pair!r}")
        if stage not in LLM_STAGES:
            raise SystemExit(f"unknown stage {stage!r}; known: {', '.join(LLM_STAGES)}")
        if stage in swaps:
            raise SystemExit(f"stage {stage!r} given twice")
        swaps[stage] = model_id
    return swaps


# operational counters and index sizes that NodeNorm and NameRes put in
# /status: large, change every minute, and say nothing about which
# data the answers came from.
BULKY_STATUS_KEYS = frozenset({"databases", "recent_queries", "solr"})


def service_versions(
    get_json: Callable[[str], dict[str, Any]],
    endpoints: Endpoints,
) -> dict[str, Any]:
    # the reasoner, the graph service and the identifier services are
    # released independently (on 2026-09-29: ARAX 1.6.2 with Biolink
    # 4.2.5, Retriever with Biolink 4.3.2, identifiers from Babel
    # 2026jul22 with Biolink 4.4.3), so every run.json records what each
    # one reported. ARAX and Retriever publish their versions in their
    # OpenAPI info block; NodeNorm and NameRes on /status. a probe that
    # fails is recorded, never raised.
    urls = {
        "arax": f"{endpoints.arax}/openapi.json",
        "retriever": f"{endpoints.retriever}/openapi.json",
        "nodenorm": f"{endpoints.nodenorm}/status",
        "nameres": f"{endpoints.nameres}/status",
    }
    versions: dict[str, Any] = {}
    for service, url in urls.items():
        try:
            body = get_json(url)
        except Exception as e:
            versions[service] = {"url": url, "error": f"{type(e).__name__}: {e}"}
            continue
        if "openapi" in body:
            versions[service] = {"url": url, **openapi_summary(body)}
            continue
        versions[service] = {
            "url": url,
            **{key: value for key, value in body.items() if key not in BULKY_STATUS_KEYS},
        }
    return versions


def _get_json(url: str) -> dict[str, Any]:
    reply = httpx.get(url, timeout=30)
    reply.raise_for_status()
    body: dict[str, Any] = reply.json()
    return body


def condition_name(
    oracle: str | None, swaps: dict[str, str], reasoner: str = "arax",
) -> str:
    # the folder every cell of this condition is written under. swaps
    # are listed in pipeline order so the same condition always gets
    # the same name. the two reasoners are different systems and get a
    # prefix each: arax/ and lookup/ (Tier 0 on Retriever). the old
    # KG2 PloverDB runs live in grounded/ and oracle_*/ folders on the
    # frozen main branch and are never mixed with these.
    prefix = "arax" if reasoner == "arax" else "lookup"
    base = f"{prefix}_oracle_{oracle}" if oracle else prefix
    suffix = "".join(
        f"__{stage}={swaps[stage]}" for stage in LLM_STAGES if stage in swaps
    )
    return base + suffix


def _default_pilot_model(cfg: Config) -> str:
    # ad-hoc questions and --smoke run on the pilot workhorse (m7,
    # ~$0.12 and ~50 s per run, 40+ logged pilot stories). the free
    # dev-tier model was measured at 550 s per run with reasoning
    # tokens blowing the output cap, so it is opt-in via --model m0.
    return PILOT_MODEL_ID if any(m.id == PILOT_MODEL_ID for m in cfg.models) else cfg.models[0].id


def _select_models(cfg: Config, ids: list[str] | None) -> list[ModelSpec]:
    if ids is None:
        # the benchmark grid is every tier except dev. dev models only
        # run when named explicitly (--model m0).
        return [m for m in cfg.models if m.tier != "dev"]
    # preserve user-given order; raise loud if any id is wrong (the
    # KeyError from cfg.model() bubbles out of argparse cleanly).
    return [cfg.model(i) for i in ids]


def _select_questions(
    qs: list[dict[str, Any]],
    ids: list[str] | None,
    split: str | None = None,
) -> list[dict[str, Any]]:
    if split is not None:
        qs = [q for q in qs if q.get("split") == split]
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
    # "wait, I didn't mean to run every model" footgun before money
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
        f"[bold]total runs[/bold]: {n_runs}\n"
    )
    console.print(Panel(body, title="PloverAI — pipeline runner", border_style="cyan"))


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
    t.add_column("results/picks", justify="right")
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
            f"{r.n_results}/{r.answers_n_picked}"
            if r.n_results >= 0 else "—"
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
    questions = load_benchmark_questions(cfg)

    # ----- mutual-exclusion guards (loud failure at the CLI boundary) -----
    if args.model and args.models:
        raise SystemExit("--model and --models are mutually exclusive.")
    if args.question and args.questions:
        raise SystemExit("--question and --questions are mutually exclusive.")

    # ----- shortcuts / aliases -----
    # smoke trumps everything else: pilot model + gold q1, exactly.
    if args.smoke:
        args.models = [_default_pilot_model(cfg)]
        args.questions = ["q1"]
        args.model = None
        args.question = None
    # singular --model is just --models with one entry.
    if args.model:
        args.models = [args.model]
    # ad-hoc question default: a free-form question only makes sense
    # paired with one model. if the user didn't pick one, use the
    # pilot workhorse so ad-hoc is one short command.
    adhoc = args.question is not None
    if adhoc and args.models is None:
        args.models = [_default_pilot_model(cfg)]

    chosen_models = _select_models(cfg, args.models)
    swaps = parse_stage_models(args.stage_model)
    # resolved now so a typo in a swapped model id fails before any call.
    stage_models = {stage: cfg.model(model_id) for stage, model_id in swaps.items()}
    condition = condition_name(args.oracle, swaps, args.reasoner)
    if args.oracle and adhoc:
        raise SystemExit("--oracle needs a gold question: it hands over the gold answer.")

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
        chosen_qs = _select_questions(questions, args.questions, args.split)

    # the uuid suffix is the same guard the FastAPI service uses: two
    # runners started in the same UTC second (a grid split per model)
    # would otherwise share one RUN_ folder, overwrite its run.json and
    # interleave one run.log.
    run_id = f"{utc_stamp()}_{uuid.uuid4().hex[:8]}"
    # log file path is decided up front, even on --dry-run, so the
    # banner can show it. on --dry-run we don't actually create the
    # file or its parent folder; setup_logger() does that for real
    # runs only. layout: logs/RUN_<run_id>/run.log.
    log_path = log_path_for(cfg.paths.logs, run_id)

    _banner(cfg, run_id, log_path, chosen_models, chosen_qs, adhoc=adhoc)
    console.print(f"condition: [bold]{condition}[/]")

    if args.dry_run:
        console.print("[yellow]dry-run: exiting without doing any work.[/]")
        return 0

    logger, log_path = setup_logger(cfg.paths.logs, run_id)
    logger.info(f"[bold]run started[/]  run_id={run_id}")

    # one client of each kind for the whole run. this matters at grid
    # scale (models x questions): per-question construction would burn a TCP
    # handshake and (for OpenRouter) a fresh rate-limit bucket per call.
    llm = OpenRouterClient(cfg, logger)
    retriever = RetrieverClient(cfg, logger)
    # RENCI clients are stateless from our side; they hold an httpx
    # pool and a logger. used at Stages 3, 5, 6, 12.
    nameres = NameResClient(cfg, logger)
    nodenorm = NodeNormClient(cfg, logger)
    # Stage 14 co-mention check (lookup condition only), on in the UI
    # and therefore here too.
    pubtator = PubTatorClient(cfg, logger)
    # the reasoner Stage 10 queries (the lookup condition asks Retriever).
    arax = AraxClient(cfg, logger) if args.reasoner == "arax" else None

    # cache Retriever's (Tier 0) meta_knowledge_graph once per run.
    # Stage 8 uses the (subject_cat, object_cat) -> [valid predicates]
    # index to constrain its predicate choice to predicates the graph
    # actually has. ~4 MB JSON, fetched in ~2 s.
    # the same two indexes the FastAPI service builds at start-up (see
    # retriever_client.build_predicate_index / build_category_set), so the
    # benchmark runs exactly the pipeline the UI serves: Stage 2 gets
    # the category list, Stage 3 the BMT neighborhoods built from it.
    predicate_index: dict[tuple[str, str], list[str]] = {}
    available_categories: list[str] = []
    try:
        meta_kg = retriever.fetch_meta_kg()
        predicate_index = build_predicate_index(meta_kg)
        available_categories = build_category_set(meta_kg)
        logger.info(
            f"meta_KG cached: {sum(len(v) for v in predicate_index.values())} "
            f"(cat-pair, predicate) entries indexed · "
            f"{len(available_categories)} biolink categories"
        )
    except Exception as e:
        # non-fatal — the pipeline still runs without the meta_KG
        # constraint: Stage 8 gets no predicate list.
        logger.warning(f"could not fetch meta_KG at start-up: {e}")

    # BMT-derived loose-neighborhood map for Stage 3's biolink_type
    # filter. computed once per run; passed to every run_grounded
    # invocation below. failure here is non-fatal — Stage 3 falls back
    # to the strict single-category filter the same way it did before.
    biolink_neighborhoods: dict[str, list[str]] = {}
    try:
        toolkit = make_biolink_toolkit()
        biolink_neighborhoods = build_biolink_neighborhood_map(
            available_categories, toolkit, logger,
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
    versions = service_versions(_get_json, cfg.endpoints)
    for service, info in versions.items():
        logger.info(
            f"service {service}: "
            f"{info.get('babel_version') or info.get('version') or info.get('error')}"
        )
    write_json(run_dir / "run.json", {
        "run_id": run_id,
        "started_utc": run_id,
        "condition": condition,
        "reasoner": args.reasoner,
        "service_versions": versions,
        # the reasoner settings define the experiment as much as the
        # service versions do: ARAX's time budget, answer cut and paths
        # per answer, and the Tier 0 tiers Retriever is asked for.
        "arax": {
            "timeout_s": cfg.arax.timeout_s,
            "top_k_answers": cfg.arax.top_k_answers,
            "paths_per_answer": cfg.arax.paths_per_answer,
        },
        "retriever": {"tiers": list(cfg.retriever.tiers)},
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
            "arax": cfg.endpoints.arax,
            "retriever": cfg.endpoints.retriever,
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
                "[cyan]running benchmark[/]",
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
                    "condition": condition,
                    "oracle": args.oracle,
                    "stage_models": {
                        stage: {"id": spec.id, "slug": spec.slug}
                        for stage, spec in stage_models.items()
                    },
                    "question_ids": [q["id"] for q in chosen_qs],
                    "endpoints": {
                        "arax": cfg.endpoints.arax,
                        "retriever": cfg.endpoints.retriever,
                        "openrouter": cfg.endpoints.openrouter,
                    },
                    "generation": {
                        "temperature": cfg.generation.temperature,
                        "max_tokens": cfg.generation.max_tokens,
                    },
                })

                for q in chosen_qs:
                    # one folder per question per (run, model,
                    # condition): outputs/RUN_<ts>/<model>/<condition>/<q_id>/.
                    qp = QuestionPaths.under(run_root.root / condition, q["id"])
                    progress.update(
                        task,
                        description=f"[cyan]{model.id} • {q['id']}[/]",
                    )
                    t_q0 = time.perf_counter()
                    try:
                        r = run_grounded(
                            cfg=cfg, model=model, q=q, qp=qp,
                            llm=llm, nameres=nameres, nodenorm=nodenorm,
                            retriever=retriever, logger=logger,
                            predicate_index=predicate_index,
                            biolink_neighborhoods=biolink_neighborhoods,
                            available_categories=available_categories,
                            pubtator=pubtator,
                            stage_models=stage_models,
                            oracle=args.oracle,
                            reasoner=args.reasoner,
                            arax=arax,
                        )
                    except Exception as e:
                        # every failure mode run_grounded knows about is
                        # already a status. this catches the ones it
                        # doesn't, so one surprise cannot abort the other
                        # cells of the grid: traceback to run.log, a
                        # meta.json so the scorer and the UI see the cell,
                        # then carry on.
                        logger.exception(
                            f"{model.id}/{q['id']} crashed outside the pipeline's own error handling"
                        )
                        err = f"{type(e).__name__}: {e}"[:400]
                        write_json(qp.meta, {
                            "q_id": q["id"],
                            "status": STATUS_CRASHED,
                            "outcome": None,
                            "outcome_reason": None,
                            "n_results": -1,
                            "answers_n_picked": -1,
                            "error": err,
                            "elapsed_s": round(time.perf_counter() - t_q0, 3),
                        })
                        r = QuestionResult(
                            q_id=q["id"],
                            status=STATUS_CRASHED,
                            cost_total_usd=0.0,
                            cost_total_tokens=(0, 0),
                            elapsed_s=time.perf_counter() - t_q0,
                            error=err,
                        )
                    grand_cost += r.cost_total_usd
                    results.append((model.id, q["id"], r))
                    progress.advance(task)
    finally:
        llm.close()
        retriever.close()
        nameres.close()
        nodenorm.close()
        pubtator.close()
        if arax is not None:
            arax.close()

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
