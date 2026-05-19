# api.py — FastAPI HTTP wrapper around the pipeline.
# the runner is the batch driver used to evaluate the gold question
# set. this module is the always-on service: one HTTP request per
# question, identical pipeline, structured JSON out. both the Next.js
# UI and external services (e.g. ARAX) hit the same endpoint here.

from __future__ import annotations

# stdlib only.
# os: read PLOVERAI_API_KEY and PLOVERAI_CORS_ORIGINS from the env.
import os
# re: validate the shape of the X-Guest-Id header (UUID v4) so a
# malformed value can't escape into a filesystem path.
import re
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
from typing import Annotated, Any

# FastAPI: lightweight, OpenAPI-native web framework. lines up with
# the Translator SmartAPI convention (PloverDB itself is documented
# the same way) so ARAX can introspect our schema.
from fastapi import Depends, FastAPI, Header, HTTPException
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
from code.config import ModelSpec, load_config
from code.logging_setup import setup_logger, utc_stamp
from code.nameres_client import NameResClient
# BMT (Biolink Model Toolkit) wrappers used at boot to derive the
# loose-neighborhood map for the Stage 3 biolink_type filter.
from code.biolink_helper import (
    build_neighborhood_map as build_biolink_neighborhood_map,
    make_toolkit as make_biolink_toolkit,
)
from code.nodenorm_client import NodeNormClient
from code.pubtator_client import PubTatorClient
from code.openrouter_client import OpenRouterClient
from code.pipeline import run_grounded
from code.plover_client import PloverClient
from code.trace import QuestionPaths, make_run_dir, make_run_root


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
    app.state.llm = OpenRouterClient(cfg, logger)
    app.state.plover = PloverClient(cfg, logger)
    app.state.nameres = NameResClient(cfg, logger)
    app.state.nodenorm = NodeNormClient(cfg, logger)
    # Stage 13 PubTator enrichment is optional; if endpoints.pubtator
    # is unreachable, the per-edge verification block degrades to None
    # and the pipeline carries on without it.
    app.state.pubtator = PubTatorClient(cfg, logger)

    # cache PloverDB's meta_knowledge_graph at start-up. ~6 MB JSON;
    # we keep both the raw response (for diagnostics) and a tiny index
    # keyed by (subject_cat, object_cat) -> [predicates] that Stage 8
    # uses to constrain its predicate choice (fix B: predicate
    # grounding — kills the "biolink:presents_with"-style hallucination).
    # if PloverDB is down at boot we still come up — the index is empty
    # and Stage 8 falls back to its prior (unconstrained) behaviour with
    # a logged warning per query.
    try:
        meta_kg = app.state.plover.fetch_meta_kg()
        app.state.meta_kg = meta_kg
        app.state.predicate_index = _build_predicate_index(meta_kg)
        # the full list of Biolink categories PloverDB actually has
        # nodes / edges for. injected into Stage 2's user message so
        # the LLM picks expected_category and answer_category from
        # categories that REALLY exist in this KG build — not from a
        # hardcoded rubric that may miss whole entity types like
        # biolink:Cell or biolink:AnatomicalEntity.
        app.state.available_categories = _build_category_set(meta_kg)
        logger.info(
            f"meta_KG cached: {sum(len(v) for v in app.state.predicate_index.values())} "
            f"(cat-pair, predicate) entries indexed · "
            f"{len(app.state.available_categories)} biolink categories"
        )
    except Exception as e:
        # any failure here is non-fatal — the pipeline degrades to its
        # prior unconstrained behaviour. better to come up than to crash.
        logger.warning(f"could not fetch meta_KG at start-up: {e}")
        app.state.meta_kg = {}
        app.state.predicate_index = {}
        app.state.available_categories = []

    # BMT-derived loose-neighborhood map for the Stage 3 NameRes filter.
    # one Toolkit per process, ~10 MB YAML loaded at construction; we
    # use it once here to precompute neighborhoods for every category
    # PloverDB actually carries, then we never touch BMT again per
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

    logger.info("[bold]api server stopping[/]")


app = FastAPI(
    title="PloverAI",
    version="0.1.0",
    description=(
        "AI chat interface for PloverDB. POST a natural-language "
        "biomedical question; receive a graph-grounded answer plus "
        "the full TRAPI pipeline trace."
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
    allow_headers=["X-API-Key", "X-Guest-Id", "Content-Type"],
)


# request / response models. FastAPI uses these for both runtime
# validation and OpenAPI schema generation (the discoverable contract
# ARAX sees at /docs and /openapi.json).
class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    # model id from config.yaml. m5 by convention is the cheap default;
    # the runner uses the same fallback for its adhoc path.
    model: str = Field("m5")


class QueryResponse(BaseModel):
    # QuestionResult summary fields plus the on-disk artifacts read
    # back into the body. the intermediates dict is intentionally
    # untyped here: each stage writes a different JSON shape and we
    # don't want to freeze a schema before the pipeline itself does.
    run_id: str
    success: bool
    outcome: str | None
    cost_usd: float
    elapsed_s: float
    answer: dict[str, Any] | None
    answer_graph_view: dict[str, Any] | None    # Stage 13: structured node-link
                                                # view with per-edge provenance,
                                                # for frontend graph rendering
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
    # services and the versions are properties of the deployed KG.
    service: str
    version: str
    started_utc: str
    endpoints: dict[str, str]
    kg_version: str
    biolink_version: str
    trapi_version: str


class RunSummary(BaseModel):
    # one row in the history list. cheap to build (we only read the
    # tiny meta.json + question.json files, not the full plover
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


class GoldQuestion(BaseModel):
    # one entry from benchmark/golden_questions/questions.json. only the
    # fields the chat UI's dropdown actually needs are exposed; the rest
    # of the gold record (TRAPI graph, anchors, validation) is for the
    # offline scorer, not the user-facing menu.
    id: str
    nl_question: str
    answer_category: str
    pinned_entity_label: str


class QuestionsResponse(BaseModel):
    questions: list[GoldQuestion]


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


# per-browser identifier. minted by the UI as a localStorage UUID and
# sent on every write request so runs can be namespaced on disk and
# the sidebar shows only this browser's history. NOT an auth boundary
# — the header is client-controlled. when real sign-in lands, the
# server stops trusting this header for ownership.
_GUEST_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def require_guest_id(
    x_guest_id: Annotated[str | None, Header(alias="X-Guest-Id")] = None,
) -> str:
    # strict shape check is also the path-traversal defense: a value
    # like "../foo" would never match the UUID regex, so it can never
    # land in `cfg.paths.results / guest_id` as a malicious path.
    if not x_guest_id or not _GUEST_ID_RE.match(x_guest_id):
        raise HTTPException(400, "missing or malformed X-Guest-Id header")
    return x_guest_id.lower()


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
    # come from config.yaml; the three version strings come from the
    # per-question gold files' `validation` block (every q*.json carries
    # the same kg_version / biolink_version / trapi_version — they are
    # the deployed PloverDB build's metadata, captured at the time the
    # gold question was validated). reading the first file gets us that
    # metadata without needing a separate metadata.json.
    cfg = app.state.cfg
    from code.config import load_questions
    qs = load_questions(cfg)
    first_validation = (qs[0].get("validation") if qs else {}) or {}
    return InfoResponse(
        service="PloverAI",
        version=app.version,
        started_utc=app.state.started_utc,
        endpoints={
            "ploverdb": cfg.endpoints.ploverdb,
            "openrouter": cfg.endpoints.openrouter,
            "nameres": cfg.endpoints.nameres,
            "nodenorm": cfg.endpoints.nodenorm,
        },
        kg_version=first_validation.get("kg_version", "unknown"),
        biolink_version=first_validation.get("biolink_version", "unknown"),
        trapi_version=first_validation.get("trapi_version", "unknown"),
    )


@app.get(
    "/api/v1/questions",
    response_model=QuestionsResponse,
    dependencies=[Depends(require_api_key)],
)
def list_questions() -> QuestionsResponse:
    # the chat box's "Questions" dropdown prefills the textarea with
    # one of these 10. each gold question is its own JSON file under
    # benchmark/golden_questions/evidence/; use the shared loader so
    # the API and the offline benchmark read the gold set identically.
    cfg = app.state.cfg
    from code.config import load_questions
    out: list[GoldQuestion] = []
    for q in load_questions(cfg):
        pinned = q.get("pinned_entity") or {}
        out.append(GoldQuestion(
            id=str(q.get("question_id") or q.get("id", "")),
            nl_question=str(q.get("nl_question", "")),
            answer_category=str(q.get("answer_category", "")),
            pinned_entity_label=str(pinned.get("label", "")),
        ))
    return QuestionsResponse(questions=out)


@app.get(
    "/api/v1/runs",
    response_model=RunsResponse,
    dependencies=[Depends(require_api_key)],
)
def list_runs(
    guest_id: Annotated[str, Depends(require_guest_id)],
    limit: int = 50,
    offset: int = 0,
) -> RunsResponse:
    # walks `outputs/<guest_id>/RUN_*/<model>/grounded/<q_id>/` and
    # reads the cheap-to-parse meta.json + question.json for each,
    # newest first. full artifacts are NOT loaded — that's what
    # /api/v1/runs/{id} is for. the listing is scoped to one guest so
    # each visitor sees only their own browser's history.
    results_root: Path = app.state.cfg.paths.results
    return RunsResponse(runs=_collect_run_summaries(
        results_root, guest_id=guest_id, limit=limit, offset=offset,
    ))


@app.get(
    "/api/v1/runs/{run_id}",
    response_model=QueryResponse,
    dependencies=[Depends(require_api_key)],
)
def get_run(run_id: str) -> QueryResponse:
    # rehydrates a past run's full artifacts into the same shape /api/
    # v1/query returns, so the UI can re-open any history entry and
    # see exactly what it saw the first time. NO guest_id dep here:
    # this is a capability-style lookup so direct URLs (e.g. a link
    # shared in a slide or email) work for any visitor, not just the
    # one who originally created the run.
    results_root: Path = app.state.cfg.paths.results
    run_dir = _find_run_dir_any_guest(results_root, run_id)
    if run_dir is None:
        raise HTTPException(404, f"unknown run: {run_id!r}")
    qp = _find_question_paths_in_run(run_dir)
    if qp is None:
        raise HTTPException(404, f"no artifacts inside run: {run_id!r}")
    meta = _read_json_if_exists(qp.meta) or {}
    cost = _read_json_if_exists(qp.cost) or {}
    return QueryResponse(
        run_id=run_id,
        success=meta.get("status") == "ok",
        outcome=meta.get("outcome"),
        cost_usd=_cost_total_usd(cost),
        elapsed_s=float(meta.get("elapsed_s", 0.0)),
        answer=_read_json_if_exists(qp.answer),
        answer_graph_view=_read_json_if_exists(qp.answer_graph_view),
        explanation=_read_text_if_exists(qp.explanation),
        intermediates={
            "trapi_query": _read_json_if_exists(qp.trapi_query),
            "validation": _read_json_if_exists(qp.validation),
            "plover_request": _read_json_if_exists(qp.plover_request),
            "plover_response_summary": _summarize_plover_response(qp.plover_response),
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
    # with real names + prices instead of the m1..m8 stub. config.yaml
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
        ]
    )


@app.post(
    "/api/v1/query",
    response_model=QueryResponse,
    dependencies=[Depends(require_api_key)],
)
def query(
    req: QueryRequest,
    guest_id: Annotated[str, Depends(require_guest_id)],
) -> QueryResponse:
    cfg = app.state.cfg
    logger = app.state.logger
    model_spec = _resolve_model(cfg, req.model)
    request_run_id, qp = _prepare_run_paths(cfg, model_spec, guest_id)

    logger.info(
        f"-> /api/v1/query  request_run_id={request_run_id}  "
        f"guest={guest_id[:8]}  "
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
        plover=app.state.plover,
        logger=logger,
        predicate_index=app.state.predicate_index,
        pubtator=app.state.pubtator,
        available_categories=app.state.available_categories,
        biolink_neighborhoods=app.state.biolink_neighborhoods,
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
async def query_stream(
    req: QueryRequest,
    guest_id: Annotated[str, Depends(require_guest_id)],
) -> StreamingResponse:
    # Server-Sent Events variant of /api/v1/query. emits a sequence of
    # JSON events while the pipeline runs so the UI can show live
    # progress, then a final 'result' event with the same payload the
    # plain /api/v1/query would have returned.
    #
    # event types we emit:
    #   log    — one pipeline log line. {level, msg, t}
    #   result — terminal success. full QueryResponse body.
    #   error  — terminal failure. {message}
    #
    # SSE wire format: each event is `data: <json>\n\n`. the browser
    # EventSource API parses this natively; our fetch+ReadableStream
    # client splits on the blank line.
    cfg = app.state.cfg
    logger: logging.Logger = app.state.logger
    model_spec = _resolve_model(cfg, req.model)
    request_run_id, qp = _prepare_run_paths(cfg, model_spec, guest_id)

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
                emit({
                    "type": "log",
                    "level": record.levelname,
                    "msg": record.getMessage(),
                    "t": round(time.monotonic() - t0, 3),
                })

    handler = _SSEHandler()
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)

    def worker() -> None:
        # run the pipeline synchronously inside the thread. on success
        # we emit a 'result' event with the same shape /api/v1/query
        # would return. on failure (an unexpected exception from inside
        # run_grounded, not a normal pipeline 'failed' status) we emit
        # an 'error' event so the UI shows the cause.
        try:
            logger.info(
                f"-> /api/v1/query/stream  request_run_id={request_run_id}  "
                f"guest={guest_id[:8]}  "
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
                plover=app.state.plover,
                logger=logger,
                predicate_index=app.state.predicate_index,
                pubtator=app.state.pubtator,
                available_categories=app.state.available_categories,
                biolink_neighborhoods=app.state.biolink_neighborhoods,
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
def _resolve_model(cfg: Any, model_id: str) -> ModelSpec:
    # a bogus id should 422 at the boundary, not blow up halfway
    # through the pipeline.
    spec: ModelSpec | None = next((m for m in cfg.models if m.id == model_id), None)
    if spec is None:
        raise HTTPException(422, f"unknown model id: {model_id!r}")
    return spec


def _prepare_run_paths(
    cfg: Any, model_spec: ModelSpec, guest_id: str,
) -> tuple[str, QuestionPaths]:
    # fresh artifact folder per HTTP request. ISO-8601 UTC timestamp
    # plus a short uuid lets concurrent requests coexist without
    # clobbering each other's files. runs are nested under the
    # caller's guest_id so the sidebar listing for one browser stays
    # isolated from another's (capability-read still works via
    # _find_run_dir_any_guest below — see GET /api/v1/runs/{id}).
    request_run_id = f"{utc_stamp()}_{uuid.uuid4().hex[:8]}"
    guest_results_root = cfg.paths.results / guest_id
    run_dir = make_run_dir(guest_results_root, request_run_id)
    run_root = make_run_root(run_dir, model_spec.id, model_spec.slug)
    # grounded is currently the only condition the service exposes;
    # ungrounded is benchmark-only and doesn't reach this layer.
    qp = QuestionPaths.under(run_root.root / "grounded", q_id="adhoc")
    return request_run_id, qp


def _build_query_response(request_run_id: str, result: Any, qp: QuestionPaths) -> QueryResponse:
    # read back what the pipeline wrote to disk. files that don't exist
    # because the run failed before that stage are returned as None;
    # the caller can tell from the status / outcome fields.
    return QueryResponse(
        run_id=request_run_id,
        success=result.status == "ok",
        outcome=result.outcome,
        cost_usd=result.cost_total_usd,
        elapsed_s=result.elapsed_s,
        answer=_read_json_if_exists(qp.answer),
        answer_graph_view=_read_json_if_exists(qp.answer_graph_view),
        explanation=_read_text_if_exists(qp.explanation),
        intermediates={
            "trapi_query": _read_json_if_exists(qp.trapi_query),
            "validation": _read_json_if_exists(qp.validation),
            "plover_request": _read_json_if_exists(qp.plover_request),
            "plover_response_summary": _summarize_plover_response(qp.plover_response),
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


def _summarize_plover_response(path: Path) -> dict[str, Any] | None:
    # raw PloverDB responses can run to megabytes; the full body would
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


# PloverDB meta_KG → {(subject_cat, object_cat): [valid predicates]}.
# called once at start-up. the meta_KG body has ~10k category-triples;
# inverting it into a dict lookup makes the per-query "what predicates
# are valid for (Disease, PhenotypicFeature)?" question a single dict
# access. predicates are sorted alphabetically so the LLM sees a
# deterministic list (same prompt across calls = better caching).
def _build_predicate_index(meta_kg: dict[str, Any]) -> dict[tuple[str, str], list[str]]:
    index: dict[tuple[str, str], set[str]] = {}
    for edge in meta_kg.get("edges") or []:
        s = edge.get("subject")
        o = edge.get("object")
        p = edge.get("predicate")
        if not s or not o or not p:
            continue
        index.setdefault((s, o), set()).add(p)
    return {k: sorted(v) for k, v in index.items()}


def _build_category_set(meta_kg: dict[str, Any]) -> list[str]:
    # extract every Biolink category PloverDB actually carries — from
    # both the nodes block (one entry per indexed category) and the
    # edges block (subject/object categories that appear in at least
    # one supported predicate triple). returned sorted so the LLM
    # always sees the same order (prompt caching).
    cats: set[str] = set()
    for c in meta_kg.get("nodes") or {}:
        if isinstance(c, str) and c.startswith("biolink:"):
            cats.add(c)
    for edge in meta_kg.get("edges") or []:
        for k in ("subject", "object"):
            v = edge.get(k)
            if isinstance(v, str) and v.startswith("biolink:"):
                cats.add(v)
    return sorted(cats)


# history-walking helpers used by GET /api/v1/runs and /api/v1/runs/{id}.
# the on-disk layout is owned by trace.py — these functions assume that
# layout and break loudly if it changes (which is what we want).
def _find_run_dir_any_guest(results_root: Path, run_id: str) -> Path | None:
    # capability-style lookup for GET /api/v1/runs/{id}. any visitor
    # with a known run_id can re-open the run regardless of which
    # guest namespace it was created under — that's how shareable URLs
    # work without sign-in. cost is O(num guest namespaces); fine for
    # thesis-demo scale, graduates to a DB lookup later. order is
    # arbitrary — run_ids are timestamp-prefixed + 8-hex-nonce so
    # collisions across namespaces are practically impossible.
    if not results_root.is_dir():
        return None
    target = f"RUN_{run_id}"
    for guest_dir in results_root.iterdir():
        if not guest_dir.is_dir():
            continue
        candidate = guest_dir / target
        if candidate.is_dir():
            return candidate
    return None


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
    guest_id: str,
    limit: int,
    offset: int = 0,
) -> list[RunSummary]:
    # newest-first listing of completed runs for one guest namespace.
    # malformed folders (a crashed run that never wrote meta.json) are
    # skipped rather than failing the whole listing — better UX, and
    # the user can spot the gap by the timestamp.
    #
    # offset enables the sidebar's infinite scroll: each successive
    # ?limit=50&offset=N call returns the next page of older runs.
    # crashed-folder skipping is applied AFTER offset/limit so callers
    # get a stable count of N rows per page regardless of how many
    # malformed folders are scattered through the directory.
    guest_root = results_root / guest_id
    if not guest_root.is_dir():
        return []
    summaries: list[RunSummary] = []
    run_dirs = sorted(
        (d for d in guest_root.iterdir() if d.is_dir() and d.name.startswith("RUN_")),
        key=lambda d: d.name,
        reverse=True,
    )
    skipped = 0
    for run_dir in run_dirs:
        run_id = run_dir.name.removeprefix("RUN_")
        qp = _find_question_paths_in_run(run_dir)
        if qp is None:
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
        # name: "<id>_<safe_slug>". split on the first underscore.
        model_folder = qp.root.parent.parent.name
        model_id, _, model_slug = model_folder.partition("_")
        summaries.append(RunSummary(
            run_id=run_id,
            started_utc=meta.get("started_utc", run_id),
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
