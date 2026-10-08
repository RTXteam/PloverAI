# api.py — FastAPI HTTP wrapper around the pipeline.
# the runner is the batch driver used to evaluate the gold question
# set. this module is the always-on service: one HTTP request per
# question, identical pipeline, structured JSON out. both the Next.js
# UI and external services (e.g. ARAX) hit the same endpoint here.

from __future__ import annotations

# stdlib only.
# os: read PLOVERAI_API_KEY and PLOVERAI_CORS_ORIGINS from the env.
import os
# uuid: short token appended to the run id so concurrent requests
# never collide on the same artifact folder.
import uuid
# json: per-request artifact files plus the SSE event payloads we
# emit while a query streams.
import json
# logging: the SSE handler attaches itself to the same Logger the
# pipeline writes to, so every log line becomes a stream event.
import logging
# asyncio: the SSE endpoint needs the event loop reference so the
# worker thread can hand records off thread-safely via call_soon.
import asyncio
# threading: the pipeline is sync; the SSE endpoint runs it in a
# background thread so the request coroutine can flush events as
# they arrive instead of after the whole pipeline finishes.
import threading
# time.monotonic: per-event timestamps so the UI can show elapsed
# seconds without trusting client clocks.
import time
# contextlib: asynccontextmanager for the FastAPI lifespan hook
# (modern replacement for @app.on_event), suppress() to wrap the SSE
# logging handler so a bad log line never crashes a request.
from contextlib import asynccontextmanager, suppress
# pathlib.Path: every on-disk location is a Path, never a str.
from pathlib import Path
# collections.abc.AsyncIterator: the modern home for AsyncIterator
# (typing's alias is deprecated under PEP 585 / ruff's UP035).
from collections.abc import AsyncIterator
# typing: Annotated for FastAPI header dependencies; Any for free-form
# JSON payloads loaded back from disk into the response body.
from typing import Annotated, Any, Literal

# httpx: the HTTP client every pipeline client uses; here only for the
# two version GETs (ARAX, Retriever) at start-up.
import httpx

# FastAPI: lightweight, OpenAPI-native web framework. lines up with
# the Translator SmartAPI convention (ARAX and Retriever are documented
# the same way).
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
# StreamingResponse: emits the SSE byte stream to the client. we
# wrap an async generator that pulls events off the per-request queue.
from fastapi.responses import StreamingResponse

# pydantic: request / response model validation. shipped with FastAPI.
from pydantic import BaseModel, Field

# python-dotenv: same env loading the runner uses. in production we
# either keep using a .env or rely on systemd's EnvironmentFile=
# directive; this call is a no-op when the file is missing.
from dotenv import load_dotenv

# our own modules: same objects the runner builds at start-up. the
# difference is lifetime — for an always-on service we want one set of
# clients per process, not per request, so the httpx connection pools
# and OpenRouter rate-limit buckets survive across calls.
from pipeline.code.config import ModelSpec, load_config
from pipeline.code.logging_setup import RequestFilter, current_request, setup_logger, utc_stamp
from pipeline.code.nameres_client import NameResClient
# BMT (Biolink Model Toolkit) wrappers used at boot to derive the
# loose-neighborhood map for the Stage 3 biolink_type filter.
from pipeline.code.biolink_helper import (
    build_neighborhood_map as build_biolink_neighborhood_map,
    make_toolkit as make_biolink_toolkit,
)
from pipeline.code.nodenorm_client import NodeNormClient
from pipeline.code.pubtator_client import PubTatorClient

# AraxClient: the reasoner behind reasoner="arax" requests.
from pipeline.code.arax_client import AraxClient
from pipeline.code.openrouter_client import OpenRouterClient
from pipeline.code.pipeline import run_grounded
# PublicGuard: per-address and site-wide question limits, used only when
# the service runs as the public site (PLOVERAI_PUBLIC=1).
from pipeline.code.public_guard import PublicGuard
from pipeline.code.retriever_client import (
    RetrieverClient,
    build_category_set,
    build_predicate_index,
    openapi_summary,
)
from pipeline.code.trace import QuestionPaths, make_run_dir, make_run_root


# pipeline/.env lives next to config.yaml, two parents up from this
# file. mirroring runner.py's resolution rule so a single .env serves
# both the CLI and the service.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # start-up: load env, parse config, attach a logger that lives for
    # the lifetime of the process, and build one client of each kind.
    # everything goes on app.state so request handlers can pull it out
    # without doing any work of their own.
    load_dotenv(dotenv_path=_ENV_FILE)
    cfg = load_config()
    server_run_id = utc_stamp()
    logger, _log_path = setup_logger(cfg.paths.logs, server_run_id)
    logger.info(
        f"[bold]api server started[/]  server_run_id={server_run_id}"
    )

    app.state.cfg = cfg
    app.state.logger = logger
    app.state.started_utc = server_run_id
    # public site mode: no login, so every question passes the guard and
    # only cfg.public.models are served. the run history stays shared:
    # every visitor sees every run, their own and everyone else's. off
    # unless pipeline/.env says PLOVERAI_PUBLIC=1.
    app.state.guard = PublicGuard(cfg.public) if os.environ.get("PLOVERAI_PUBLIC") == "1" else None
    if app.state.guard is not None:
        logger.info(
            f"public site mode ON  models={list(cfg.public.models)}  "
            f"per_ip_per_hour={cfg.public.questions_per_ip_per_hour}  "
            f"per_day={cfg.public.questions_per_day}  "
            f"concurrent={cfg.public.concurrent_runs}  "
            f"concurrent_per_ip={cfg.public.concurrent_runs_per_ip}"
        )
    app.state.llm = OpenRouterClient(cfg, logger)
    app.state.retriever = RetrieverClient(cfg, logger)
    app.state.nameres = NameResClient(cfg, logger)
    app.state.nodenorm = NodeNormClient(cfg, logger)
    # Stage 14 PubTator enrichment (lookup condition only) is optional;
    # if endpoints.pubtator is unreachable, the per-edge verification
    # block degrades to None and the pipeline carries on without it.
    app.state.pubtator = PubTatorClient(cfg, logger)
    # the reasoner: Stage 10 asks ARAX, which reasons over Tier 0.
    app.state.arax = AraxClient(cfg, logger)

    # what ARAX and Retriever say about themselves (version, TRAPI,
    # Biolink), for the sidebar's system panel. one GET each at boot;
    # a service that does not answer shows as "unknown".
    app.state.service_info = {}
    for service, base in (("arax", cfg.endpoints.arax), ("retriever", cfg.endpoints.retriever)):
        try:
            reply = httpx.get(f"{base}/openapi.json", timeout=30)
            reply.raise_for_status()
            app.state.service_info[service] = openapi_summary(reply.json())
        except Exception as e:
            logger.warning(f"could not read {service} version at start-up: {e}")
            app.state.service_info[service] = {}

    # read Retriever's (Tier 0) meta_knowledge_graph at start-up (~4 MB
    # JSON) and keep only what the pipeline uses: a tiny index keyed by
    # (subject_cat, object_cat) -> [predicates] that Stage 8 uses to
    # constrain its predicate choice to predicates the graph actually has
    # (kills the "biolink:presents_with"-style hallucination), and the
    # category list below.
    # if Retriever is down at boot we still come up — the index is empty
    # and Stage 8 falls back to its prior (unconstrained) behaviour with
    # a logged warning per query.
    try:
        meta_kg = app.state.retriever.fetch_meta_kg()
        app.state.predicate_index = build_predicate_index(meta_kg)
        # the full list of Biolink categories the Tier 0 graph actually has
        # nodes / edges for. injected into Stage 2's user message so
        # the LLM picks expected_category and answer_category from
        # categories that REALLY exist in this KG build — not from a
        # hardcoded rubric that may miss whole entity types like
        # biolink:Cell or biolink:AnatomicalEntity.
        app.state.available_categories = build_category_set(meta_kg)
        logger.info(
            f"meta_KG cached: {sum(len(v) for v in app.state.predicate_index.values())} "
            f"(cat-pair, predicate) entries indexed · "
            f"{len(app.state.available_categories)} biolink categories"
        )
    except Exception as e:
        # any failure here is non-fatal — the pipeline degrades to its
        # prior unconstrained behaviour. better to come up than to crash.
        logger.warning(f"could not fetch meta_KG at start-up: {e}")
        app.state.predicate_index = {}
        app.state.available_categories = []

    # BMT-derived loose-neighborhood map for the Stage 3 NameRes filter.
    # one Toolkit per process, ~10 MB YAML loaded at construction; we
    # use it once here to precompute neighborhoods for every category
    # the Tier 0 graph actually carries, then we never touch BMT again per
    # request. failures are non-fatal: pipeline falls back to the
    # strict single-category filter, same as the old behaviour.
    try:
        toolkit = make_biolink_toolkit()
        app.state.biolink_neighborhoods = build_biolink_neighborhood_map(
            app.state.available_categories or [], toolkit, logger,
        )
        sample = app.state.biolink_neighborhoods.get("biolink:Pathway", [])
        logger.info(
            f"biolink neighborhoods cached: "
            f"{len(app.state.biolink_neighborhoods)} categories · "
            f"e.g. biolink:Pathway → {sample}"
        )
    except Exception as e:
        logger.warning(f"could not build biolink neighborhoods at start-up: {e}")
        app.state.biolink_neighborhoods = {}

    yield

    app.state.arax.close()
    logger.info("[bold]api server stopping[/]")


app = FastAPI(
    title="PloverAI",
    version="0.1.0",
    description=(
        "LLM interface to ARAX, the NCATS Biomedical Data Translator "
        "reasoner. POST a natural-language biomedical question; an LLM "
        "builds the TRAPI query, ARAX reasons over the Translator Tier 0 "
        "knowledge graph, and the LLM explains ARAX's reasoning paths "
        "with cited facts. The response carries the full pipeline trace."
    ),
    lifespan=lifespan,
)

# CORS: the Next.js dev server runs on a different origin than the
# Python service (localhost:3000 vs localhost:8000). in production we
# put both behind one nginx vhost so this list is short — typically
# just the dev origin plus the public hostname.
_origins = os.environ.get(
    "PLOVERAI_CORS_ORIGINS",
    "http://localhost:3000",
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins if o.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)


# request / response models. FastAPI uses these for both runtime
# validation and OpenAPI schema generation (the discoverable contract
# ARAX sees at /docs and /openapi.json).
class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    # model id from config.yaml. m8 (openai/gpt-6-luna, $0.10 / $0.50
    # per 1M tokens) is the cheapest model on the roster and the one the
    # ARAX-mode cost figures were measured on (~$0.002 per question).
    model: str = Field("m8")
    # "arax" (the default, and all the UI sends) asks ARAX, which
    # reasons over Tier 0 and returns the reasoning paths the UI draws;
    # "lookup" is the benchmark's one-hop Tier 0 lookup on Retriever.
    reasoner: Literal["arax", "lookup"] = Field("arax")


class QueryResponse(BaseModel):
    # QuestionResult summary fields plus the on-disk artifacts read
    # back into the body. the intermediates dict is intentionally
    # untyped here: each stage writes a different JSON shape and we
    # don't want to freeze a schema before the pipeline itself does.
    run_id: str
    # the question and model this run was made with, read from the run's
    # own folder: the UI labels a result with these, never with whatever
    # is in the input box now.
    question: str | None = None
    model_id: str | None = None
    success: bool
    outcome: str | None
    cost_usd: float
    elapsed_s: float
    answer: dict[str, Any] | None
    answer_graph_view: dict[str, Any] | None    # Stage 13, lookup condition:
                                                # node-link view with per-edge
                                                # provenance, for graph rendering
    reasoning_graph: dict[str, Any] | None      # ARAX mode: reasoning paths behind
                                                # the picked answers, facts F1..Fn
    explanation: str | None
    intermediates: dict[str, Any]


class ModelInfo(BaseModel):
    # one row in the dropdown the UI renders. fields mirror ModelSpec
    # in config.py — keep them in sync. provider + tier let the UI
    # group / colour models; the two prices let it show $/M-tok inline.
    id: str
    slug: str
    provider: str
    tier: str
    price_in: float
    price_out: float


class ModelsResponse(BaseModel):
    models: list[ModelInfo]


class InfoResponse(BaseModel):
    # service-level metadata for the sidebar "what is this hitting?"
    # panel. nothing here is secret; the URLs are public Translator
    # services and the versions are what those services report.
    service: str
    version: str
    started_utc: str
    endpoints: dict[str, str]
    reasoner_version: str
    kg_version: str
    biolink_version: str
    trapi_version: str
    # true on the public site (one model, question limits).
    public: bool


class RunSummary(BaseModel):
    # one row in the history list. cheap to build (we only read the
    # tiny meta.json + question.json files, not the full reasoner
    # response) so listing 100 runs stays fast.
    run_id: str
    started_utc: str
    model_id: str
    model_slug: str
    question: str
    status: str
    outcome: str | None
    cost_usd: float
    elapsed_s: float


class RunsResponse(BaseModel):
    runs: list[RunSummary]


class ExampleQuestion(BaseModel):
    # one example question of the web UI (pipeline/examples.yaml): its
    # place in the list and its natural-language text, nothing more. the
    # menu shows the question as a user would type it, no relation or
    # Biolink types.
    id: str
    nl_question: str


class QuestionsResponse(BaseModel):
    questions: list[ExampleQuestion]


# api-key dependency. one shared key kept in env. fail closed if the
# server is misconfigured — better to 503 than to be open by mistake.
def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    expected = os.environ.get("PLOVERAI_API_KEY")
    if not expected:
        raise HTTPException(503, "server missing PLOVERAI_API_KEY")
    if x_api_key != expected:
        raise HTTPException(401, "invalid api key")


@app.get("/health")
def health() -> dict[str, str]:
    # liveness probe for nginx / monit / uptime checks. cheap and
    # uncached so the caller sees the actual process status.
    return {"status": "ok"}


@app.get(
    "/api/v1/info",
    response_model=InfoResponse,
    dependencies=[Depends(require_api_key)],
)
def info() -> InfoResponse:
    # what the UI puts in the "system info" sidebar panel. endpoints
    # come from config.yaml; the versions are what ARAX and Retriever
    # published in their OpenAPI info blocks at server start-up.
    cfg = app.state.cfg
    arax_info = app.state.service_info.get("arax") or {}
    retriever_info = app.state.service_info.get("retriever") or {}
    retriever_version = retriever_info.get("version")
    return InfoResponse(
        service="PloverAI",
        version=app.version,
        started_utc=app.state.started_utc,
        endpoints={
            "arax": cfg.endpoints.arax,
            "retriever": cfg.endpoints.retriever,
            "openrouter": cfg.endpoints.openrouter,
            "nameres": cfg.endpoints.nameres,
            "nodenorm": cfg.endpoints.nodenorm,
        },
        reasoner_version=f"ARAX {arax_info['version']}" if arax_info.get("version") else "ARAX",
        kg_version="Translator Tier 0"
        + (f" (Retriever {retriever_version})" if retriever_version else ""),
        biolink_version=arax_info.get("biolink_version") or "unknown",
        trapi_version=arax_info.get("trapi_version") or "unknown",
        public=app.state.guard is not None,
    )


@app.get(
    "/api/v1/questions",
    response_model=QuestionsResponse,
    dependencies=[Depends(require_api_key)],
)
def list_questions() -> QuestionsResponse:
    # the "Example questions" menu and the start page prefill the
    # question box with one of these. they come from
    # pipeline/examples.yaml, not from the benchmark's gold set.
    from pipeline.code.config import load_examples
    return QuestionsResponse(questions=[
        ExampleQuestion(id=f"e{i}", nl_question=q)
        for i, q in enumerate(load_examples(app.state.cfg), start=1)
    ])


@app.get(
    "/api/v1/runs",
    response_model=RunsResponse,
    dependencies=[Depends(require_api_key)],
)
def list_runs(limit: int = 50, offset: int = 0) -> RunsResponse:
    # walks `code/outputs/RUN_*/<model>/<condition>/<q_id>/` and reads the
    # cheap-to-parse meta.json + question.json for each, newest first.
    # full artifacts are NOT loaded — that's what /api/v1/runs/{id} is
    # for. limit caps how many rows the UI sidebar pulls at once;
    # offset lets the sidebar paginate older entries on infinite-scroll.
    # the history is shared, on the public site too: every visitor sees
    # every run in the system, whoever asked it.
    results_root: Path = app.state.cfg.paths.results
    return RunsResponse(runs=_collect_run_summaries(
        results_root, limit=limit, offset=offset,
    ))


@app.get(
    "/api/v1/runs/{run_id}",
    response_model=QueryResponse,
    dependencies=[Depends(require_api_key)],
)
def get_run(run_id: str) -> QueryResponse:
    # rehydrates a past run's full artifacts into the same shape /api/
    # v1/query returns, so the UI can re-open any history entry and
    # see exactly what it saw the first time.
    results_root: Path = app.state.cfg.paths.results
    run_dir = results_root / f"RUN_{run_id}"
    if not run_dir.is_dir():
        raise HTTPException(404, f"unknown run: {run_id!r}")
    qp = _find_question_paths_in_run(run_dir)
    if qp is None:
        raise HTTPException(404, f"no artifacts inside run: {run_id!r}")
    meta = _read_json_if_exists(qp.meta) or {}
    cost = _read_json_if_exists(qp.cost) or {}
    question, model_id = _run_question_and_model(qp, run_dir)
    return QueryResponse(
        run_id=run_id,
        question=question,
        model_id=model_id,
        success=meta.get("status") == "ok",
        outcome=meta.get("outcome"),
        cost_usd=_cost_total_usd(cost),
        elapsed_s=float(meta.get("elapsed_s", 0.0)),
        answer=_read_json_if_exists(qp.answer),
        answer_graph_view=_read_json_if_exists(qp.answer_graph_view),
        reasoning_graph=_read_json_if_exists(qp.reasoning_graph),
        explanation=_read_text_if_exists(qp.explanation),
        intermediates={
            "trapi_query": _read_json_if_exists(qp.trapi_query),
            "validation": _read_json_if_exists(qp.validation),
            "reasoner_request": _read_json_if_exists(
                _first_existing(qp.reasoner_request, qp.root / "plover_request.json")
            ),
            "reasoner_response_summary": _summarize_reasoner_response(
                _first_existing(qp.reasoner_response, qp.root / "plover_response.json")
            ),
            "reduced_data": _summarize_reduced_data(qp.reduced_data),
            "nameres": _read_json_if_exists(qp.nameres),
            "candidate_probes": _read_json_if_exists(qp.candidate_probes),
            "nodenorm": _read_json_if_exists(qp.nodenorm),
            "predicate_probe": _read_json_if_exists(qp.predicate_probe),
            "cost": _read_json_if_exists(qp.cost),
            "prompts": _read_json_if_exists(qp.prompt),
        },
    )


@app.get(
    "/api/v1/models",
    response_model=ModelsResponse,
    dependencies=[Depends(require_api_key)],
)
def list_models() -> ModelsResponse:
    # the UI calls this once on mount to populate its model selector
    # with real names + prices instead of a hard-coded list. config.yaml
    # stays the single source of truth; the dropdown tracks it.
    cfg = app.state.cfg
    return ModelsResponse(
        models=[
            ModelInfo(
                id=m.id,
                slug=m.slug,
                provider=m.provider,
                tier=m.tier,
                price_in=m.price_in,
                price_out=m.price_out,
            )
            for m in cfg.models
            if app.state.guard is None or m.id in cfg.public.models
        ]
    )


@app.post(
    "/api/v1/query",
    response_model=QueryResponse,
    dependencies=[Depends(require_api_key)],
)
def query(req: QueryRequest, http_request: Request) -> QueryResponse:
    model_spec = _resolve_model(app.state.cfg, req.model)
    admitted_ip = _admit_public(req, http_request)
    try:
        return _run_query(req, model_spec)
    finally:
        _release_public(admitted_ip)


def _run_query(req: QueryRequest, model_spec: ModelSpec) -> QueryResponse:
    cfg = app.state.cfg
    logger = app.state.logger
    request_run_id, qp = _prepare_run_paths(cfg, model_spec, req.reasoner)

    logger.info(
        f"-> /api/v1/query  request_run_id={request_run_id}  "
        f"model={model_spec.id}  q_len={len(req.question)}"
    )

    result = run_grounded(
        cfg=cfg,
        model=model_spec,
        q={"id": "adhoc", "nl_question": req.question, "adhoc": True},
        qp=qp,
        llm=app.state.llm,
        nameres=app.state.nameres,
        nodenorm=app.state.nodenorm,
        retriever=app.state.retriever,
        logger=logger,
        predicate_index=app.state.predicate_index,
        pubtator=app.state.pubtator,
        available_categories=app.state.available_categories,
        biolink_neighborhoods=app.state.biolink_neighborhoods,
        reasoner=req.reasoner,
        arax=app.state.arax,
    )

    logger.info(
        f"<- /api/v1/query  request_run_id={request_run_id}  "
        f"status={result.status}  "
        f"cost=${result.cost_total_usd:.6f}  "
        f"elapsed_s={result.elapsed_s:.2f}"
    )
    return _build_query_response(request_run_id, result, qp)


@app.post(
    "/api/v1/query/stream",
    dependencies=[Depends(require_api_key)],
)
async def query_stream(req: QueryRequest, http_request: Request) -> StreamingResponse:
    # Server-Sent Events variant of /api/v1/query. emits a sequence of
    # JSON events while the pipeline runs so the UI can show live
    # progress, then a final 'result' event with the same payload the
    # plain /api/v1/query would have returned.
    #
    # event types we emit:
    #   log    — one pipeline log line. {level, msg, t}
    #   stage  — one structured pipeline step for the live graph:
    #            {stage, ..., t}; stages entity / query / results /
    #            answers / graph (see pipeline._ui_event)
    #   result — terminal success. full QueryResponse body.
    #   error  — terminal failure. {message}
    #
    # SSE wire format: each event is `data: <json>\n\n`. the browser
    # EventSource API parses this natively; our fetch+ReadableStream
    # client splits on the blank line.
    cfg = app.state.cfg
    logger: logging.Logger = app.state.logger
    model_spec = _resolve_model(cfg, req.model)
    # the slot is held until the worker thread ends (not until the
    # browser disconnects: the run goes on either way).
    admitted_ip = _admit_public(req, http_request)
    try:
        request_run_id, qp = _prepare_run_paths(cfg, model_spec, req.reasoner)
    except Exception:
        _release_public(admitted_ip)
        raise

    loop = asyncio.get_running_loop()
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    t0 = time.monotonic()

    def emit(event: dict[str, Any]) -> None:
        # thread-safe enqueue: the worker thread calls this; the main
        # event loop drains the queue from the async generator below.
        loop.call_soon_threadsafe(events.put_nowait, event)

    class _SSEHandler(logging.Handler):
        # bridges the existing python logger to the SSE stream. attached
        # for the duration of this one request only; removed in finally.
        # the body is wrapped in suppress() because a logging handler
        # must never raise — it would tear down the request mid-pipeline.
        def emit(self, record: logging.LogRecord) -> None:
            with suppress(Exception):
                # a record logged with extra={"ui_event": {...}} (see
                # pipeline._ui_event) is a structured step the live graph
                # draws; it goes out as a "stage" event instead of a line.
                ui_event = getattr(record, "ui_event", None)
                if isinstance(ui_event, dict):
                    emit({"type": "stage", **ui_event, "t": round(time.monotonic() - t0, 3)})
                    return
                emit({
                    "type": "log",
                    "level": record.levelname,
                    "msg": record.getMessage(),
                    "t": round(time.monotonic() - t0, 3),
                })

    handler = _SSEHandler()
    handler.setLevel(logging.INFO)
    # the logger is shared by every question in flight; this stream takes
    # only the records of this request (set in worker, below).
    handler.addFilter(RequestFilter(request_run_id))
    logger.addHandler(handler)

    def worker() -> None:
        # run the pipeline synchronously inside the thread. on success
        # we emit a 'result' event with the same shape /api/v1/query
        # would return. on failure (an unexpected exception from inside
        # run_grounded, not a normal pipeline 'failed' status) we emit
        # an 'error' event so the UI shows the cause.
        current_request.set(request_run_id)
        try:
            logger.info(
                f"-> /api/v1/query/stream  request_run_id={request_run_id}  "
                f"model={model_spec.id}  q_len={len(req.question)}"
            )
            result = run_grounded(
                cfg=cfg,
                model=model_spec,
                q={"id": "adhoc", "nl_question": req.question, "adhoc": True},
                qp=qp,
                llm=app.state.llm,
                nameres=app.state.nameres,
                nodenorm=app.state.nodenorm,
                retriever=app.state.retriever,
                logger=logger,
                predicate_index=app.state.predicate_index,
                pubtator=app.state.pubtator,
                available_categories=app.state.available_categories,
                biolink_neighborhoods=app.state.biolink_neighborhoods,
                reasoner=req.reasoner,
                arax=app.state.arax,
            )
            logger.info(
                f"<- /api/v1/query/stream  request_run_id={request_run_id}  "
                f"status={result.status}  "
                f"cost=${result.cost_total_usd:.6f}  "
                f"elapsed_s={result.elapsed_s:.2f}"
            )
            response = _build_query_response(request_run_id, result, qp)
            emit({"type": "result", "data": response.model_dump()})
        except Exception as e:
            # we deliberately catch anything here — the contract of the
            # streaming endpoint is "always end with one terminal event",
            # so an unexpected crash inside the pipeline becomes an
            # 'error' event the UI can render rather than a dead socket.
            logger.exception("pipeline crashed inside /api/v1/query/stream")
            emit({"type": "error", "message": str(e)})
        finally:
            _release_public(admitted_ip)

    threading.Thread(target=worker, daemon=True).start()

    async def generate() -> AsyncIterator[bytes]:
        try:
            while True:
                event = await events.get()
                yield f"data: {json.dumps(event)}\n\n".encode()
                if event["type"] in ("result", "error"):
                    break
        finally:
            logger.removeHandler(handler)

    # cache-control: no caching of an SSE stream at any layer. x-accel
    # buffering off in case nginx is in front (it strips the header
    # otherwise; harmless to send unconditionally).
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# helpers shared by both /api/v1/query and /api/v1/query/stream.
def _client_ip(http_request: Request) -> str:
    # behind nginx every request arrives from loopback and nginx puts the
    # visitor's address in X-Real-IP. the header is trusted only from
    # loopback, so a caller that reaches uvicorn directly cannot fake it.
    peer = http_request.client.host if http_request.client else "unknown"
    if peer in ("127.0.0.1", "::1"):
        return http_request.headers.get("x-real-ip") or peer
    return peer


def _admit_public(req: QueryRequest, http_request: Request) -> str | None:
    # on the public site: only the allowed models, only ARAX, and only
    # when the guard has a free slot. returns the address that holds a
    # slot (release it when the run ends), or None off the public site.
    guard: PublicGuard | None = app.state.guard
    if guard is None:
        return None
    cfg = app.state.cfg
    if req.model not in cfg.public.models:
        raise HTTPException(403, f"model {req.model!r} is not available on the public site")
    if req.reasoner != "arax":
        raise HTTPException(403, "the public site answers with ARAX only")
    client_ip = _client_ip(http_request)
    admission = guard.admit(client_ip)
    if not admission.allowed:
        app.state.logger.info(
            f"public guard refused  ip={client_ip}  status={admission.status}  "
            f"retry_after_s={admission.retry_after_s}  reason={admission.reason}"
        )
        raise HTTPException(
            admission.status,
            admission.reason,
            headers={"Retry-After": str(admission.retry_after_s)},
        )
    return client_ip


def _release_public(admitted_ip: str | None) -> None:
    guard: PublicGuard | None = app.state.guard
    if guard is not None and admitted_ip is not None:
        guard.release(admitted_ip)


def _resolve_model(cfg: Any, model_id: str) -> ModelSpec:
    # a bogus id should 422 at the boundary, not blow up halfway
    # through the pipeline.
    spec: ModelSpec | None = next((m for m in cfg.models if m.id == model_id), None)
    if spec is None:
        raise HTTPException(422, f"unknown model id: {model_id!r}")
    return spec


def _prepare_run_paths(
    cfg: Any, model_spec: ModelSpec, reasoner: str = "arax",
) -> tuple[str, QuestionPaths]:
    # fresh artifact folder per HTTP request. ISO-8601 UTC timestamp
    # plus a short uuid lets concurrent requests coexist without
    # clobbering each other's files.
    request_run_id = f"{utc_stamp()}_{uuid.uuid4().hex[:8]}"
    run_dir = make_run_dir(cfg.paths.results, request_run_id)
    run_root = make_run_root(run_dir, model_spec.id, model_spec.slug)
    # the same condition folder names as the runner: arax/ for an ARAX
    # run, lookup/ for a one-hop Tier 0 lookup.
    condition = "arax" if reasoner == "arax" else "lookup"
    qp = QuestionPaths.under(run_root.root / condition, q_id="adhoc")
    return request_run_id, qp


def _build_query_response(request_run_id: str, result: Any, qp: QuestionPaths) -> QueryResponse:
    # read back what the pipeline wrote to disk. files that don't exist
    # because the run failed before that stage are returned as None;
    # the caller can tell from the status / outcome fields.
    run_dir = app.state.cfg.paths.results / f"RUN_{request_run_id}"
    question, model_id = _run_question_and_model(qp, run_dir)
    return QueryResponse(
        run_id=request_run_id,
        question=question,
        model_id=model_id,
        success=result.status == "ok",
        outcome=result.outcome,
        cost_usd=result.cost_total_usd,
        elapsed_s=result.elapsed_s,
        answer=_read_json_if_exists(qp.answer),
        answer_graph_view=_read_json_if_exists(qp.answer_graph_view),
        reasoning_graph=_read_json_if_exists(qp.reasoning_graph),
        explanation=_read_text_if_exists(qp.explanation),
        intermediates={
            "trapi_query": _read_json_if_exists(qp.trapi_query),
            "validation": _read_json_if_exists(qp.validation),
            "reasoner_request": _read_json_if_exists(
                _first_existing(qp.reasoner_request, qp.root / "plover_request.json")
            ),
            "reasoner_response_summary": _summarize_reasoner_response(
                _first_existing(qp.reasoner_response, qp.root / "plover_response.json")
            ),
            "reduced_data": _summarize_reduced_data(qp.reduced_data),
            "nameres": _read_json_if_exists(qp.nameres),
            "candidate_probes": _read_json_if_exists(qp.candidate_probes),
            "nodenorm": _read_json_if_exists(qp.nodenorm),
            "predicate_probe": _read_json_if_exists(qp.predicate_probe),
            "cost": _read_json_if_exists(qp.cost),
            "prompts": _read_json_if_exists(qp.prompt),
        },
    )


# small file-reading helpers. local to this module — they exist only
# to compose the API response from the per-request artifact files.
def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _read_text_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text()


def _cost_total_usd(cost: dict[str, Any] | None) -> float:
    # cost.json is shaped as {stages: [...], totals: {total_usd: ...}}.
    # we want the run-level total; the top-level lookup we used before
    # silently returned 0 because the key only exists nested under
    # `totals`. fall back to the top-level path defensively for any old
    # artifact that predates the totals/ section.
    if not cost:
        return 0.0
    totals = cost.get("totals")
    if isinstance(totals, dict) and "total_usd" in totals:
        return float(totals["total_usd"])
    if "total_usd" in cost:
        return float(cost["total_usd"])
    return 0.0


def _first_existing(path: Path, legacy: Path) -> Path:
    # the first ARAX runs (2026-09-29, before the file rename) still
    # wrote Stage 10 to plover_request.json / plover_response.json; 8 of
    # the 12 ARAX run folders from that day use the old names.
    return path if path.exists() or not legacy.exists() else legacy


def _summarize_reasoner_response(path: Path) -> dict[str, Any] | None:
    # raw ARAX / Retriever responses can run to megabytes; the full body would
    # bloat every API response. for the wire we send counts only — the
    # full JSON is on disk under the run_id if the UI or the user
    # actually wants it.
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    message = data.get("message") or {}
    kg = message.get("knowledge_graph") or {}
    return {
        "n_results": len(message.get("results") or []),
        "n_nodes": len(kg.get("nodes") or {}),
        "n_edges": len(kg.get("edges") or {}),
    }


# how many reduced rows we put on the wire. the artifact can hold up to
# reduction.top_k_edges rows (300 by default) and each row is a dict of
# 13 fields, so shipping all of them would dominate the API payload.
# the stats always go out in full; the rows are a preview and the whole
# file is on disk under the run_id.
MAX_REDUCED_ROWS_ON_WIRE = 50


def _summarize_reduced_data(path: Path) -> dict[str, Any] | None:
    data = _read_json_if_exists(path)
    if data is None:
        return None
    rows = data.get("rows") or []
    if len(rows) <= MAX_REDUCED_ROWS_ON_WIRE:
        return data
    return {
        **{key: value for key, value in data.items() if key != "rows"},
        "rows": rows[:MAX_REDUCED_ROWS_ON_WIRE],
        "rows_truncated": True,
        "rows_total": len(rows),
    }


# history-walking helpers used by GET /api/v1/runs and /api/v1/runs/{id}.
# the on-disk layout is owned by trace.py — these functions assume that
# layout and break loudly if it changes (which is what we want).
def _run_question_and_model(qp: QuestionPaths, run_dir: Path) -> tuple[str | None, str | None]:
    # the question comes from the run's frozen question.json; the model
    # id from the model folder name "<id>_<safe_slug>", the first path
    # component under the run dir (same rule as _collect_run_summaries).
    record = _read_json_if_exists(qp.question) or {}
    try:
        model_folder = qp.root.relative_to(run_dir).parts[0]
    except ValueError:
        model_folder = ""
    question = record.get("nl_question")
    return (question if isinstance(question, str) else None), (model_folder.partition("_")[0] or None)


def _find_question_paths_in_run(run_dir: Path) -> QuestionPaths | None:
    # the runner writes one (model, condition, q_id) folder per
    # invocation. for the API service that's always exactly one
    # combination, so we just find the first non-empty leaf and use it.
    for model_dir in sorted(run_dir.iterdir()):
        if not model_dir.is_dir():
            continue
        for condition_dir in sorted(model_dir.iterdir()):
            if not condition_dir.is_dir():
                continue
            for q_dir in sorted(condition_dir.iterdir()):
                if q_dir.is_dir() and (q_dir / "meta.json").exists():
                    return QuestionPaths.under(condition_dir, q_dir.name)
    return None


def _collect_run_summaries(
    results_root: Path,
    limit: int,
    offset: int = 0,
) -> list[RunSummary]:
    # newest-first listing of completed runs. malformed folders (a
    # crashed run that never wrote meta.json) are skipped rather than
    # failing the whole listing — better UX, and the user can spot
    # the gap by the timestamp.
    #
    # offset enables the sidebar's infinite scroll: each successive
    # ?limit=50&offset=N call returns the next page of older runs.
    # crashed-folder skipping is applied AFTER offset/limit so callers
    # get a stable count of N rows per page regardless of how many
    # malformed folders are scattered through the directory.
    if not results_root.is_dir():
        return []
    summaries: list[RunSummary] = []
    run_dirs = sorted(
        (d for d in results_root.iterdir() if d.is_dir() and d.name.startswith("RUN_")),
        key=lambda d: d.name,
        reverse=True,
    )
    skipped = 0
    for run_dir in run_dirs:
        run_id = run_dir.name.removeprefix("RUN_")
        qp = _find_question_paths_in_run(run_dir)
        if qp is None:
            continue
        # the history is ARAX's: runs of the benchmark's lookup
        # condition stay on disk and open by URL, but are not listed.
        if not qp.root.parent.name.startswith("arax"):
            continue
        # advance past `offset` VALID rows, not raw directory entries —
        # otherwise the caller's "next page" could miss rows when
        # crashed-folder skips happen earlier in the list.
        if skipped < offset:
            skipped += 1
            continue
        meta = _read_json_if_exists(qp.meta) or {}
        question_record = _read_json_if_exists(qp.question) or {}
        cost = _read_json_if_exists(qp.cost) or {}
        # the (model_id, model_slug) pair lives at the model folder
        # name: "<id>_<safe_slug>" — the first path component under the
        # run dir.
        model_folder = qp.root.relative_to(run_dir).parts[0]
        model_id, _, model_slug = model_folder.partition("_")
        summaries.append(RunSummary(
            run_id=run_id,
            # meta.json records no start time; the run id is
            # "<utc_stamp>_<uuid8>", stamped when the run started.
            started_utc=run_id.partition("_")[0],
            model_id=model_id,
            model_slug=model_slug.replace("_", "/", 1),
            question=question_record.get("nl_question", "(unknown question)"),
            status=meta.get("status", "unknown"),
            outcome=meta.get("outcome"),
            cost_usd=_cost_total_usd(cost),
            elapsed_s=float(meta.get("elapsed_s", 0.0)),
        ))
        if len(summaries) >= limit:
            break
    return summaries
