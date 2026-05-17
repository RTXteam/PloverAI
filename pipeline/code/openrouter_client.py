# openrouter_client.py — talks to OpenRouter's chat-completions endpoint.
# this is the only place in the pipeline that knows the OpenRouter HTTP
# shape. everything above this layer (pipeline.py, runner.py) sees a
# small, typed Python interface (chat() -> LLMReply).

from __future__ import annotations

# json: stdlib. used to render the 1st 400 chars of an error body in
# logs so a 4xx / 5xx is debuggable without re-running.
import json

# logging: stdlib. the logger is injected by the runner so per-run log
# files capture every API call this client makes.
import logging

# os: stdlib. we read OPENROUTER_API_KEY from the environment. the
# runner pre-loads pipeline/.env via python-dotenv before constructing
# this client, so by the time we get here the key is in os.environ.
import os

# time.perf_counter: stdlib. high-resolution timer for per-call latency.
import time

# dataclasses: stdlib. LLMReply is a frozen dataclass that the rest of
# the pipeline destructures.
from dataclasses import dataclass

# typing.Any: stdlib. the raw OpenRouter response is a deeply nested
# dict we keep verbatim for the trace folder.
from typing import Any

# httpx: third-party. modern HTTP client. we use it (instead of requests)
# for proper timeout handling, connection pooling, and explicit error
# types. one Client per OpenRouterClient instance reuses TCP across the
# 160-instance benchmark.
import httpx

# Config / ModelSpec come from our config module — they tell us the
# base URL, the request timeout, and which model slug to send.
from .config import Config, ModelSpec

# CostBreakdown / usd: our own cost arithmetic. nothing else owns it,
# so we always go through this helper for consistency.
from .cost import CostBreakdown, usd


class OpenRouterError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMReply:
    content: str               # the assistant text we got back
    raw: dict[str, Any]        # full JSON body, kept for the trace
    input_tokens: int
    output_tokens: int
    cost: CostBreakdown
    latency_s: float


class OpenRouterClient:
    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            # fail at construction time, not mid-run. the .env loader has
            # already had its chance by the time the client is built.
            raise OpenRouterError(
                "OPENROUTER_API_KEY is not set. add it to pipeline/.env or export it."
            )
        self._cfg = cfg
        self._log = logger
        # one httpx.Client reuses the connection pool across all calls
        # in the run. the OpenRouter referer/title headers are advisory
        # — they show up in OpenRouter's analytics dashboard.
        self._http = httpx.Client(
            base_url=cfg.endpoints.openrouter,
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/RTXteam/PloverAI",
                "X-Title": "PloverAI Benchmark",
            },
            timeout=cfg.generation.request_timeout_s,
        )

    def close(self) -> None:
        self._http.close()

    def chat(
        self,
        *,
        model: ModelSpec,
        system: str,
        user: str,
        stage: str,
    ) -> LLMReply:
        # one chat-completion call. temperature/max_tokens come from the
        # YAML config so a tweak there changes every model uniformly.
        # tools are never enabled — every external lookup the pipeline
        # does (NameRes, NodeNorm, validator, PloverDB) is wired in
        # Python, not via the model's function-calling. that keeps the
        # benchmark testing the model's reasoning, not its tool-use.
        body: dict[str, Any] = {
            "model": model.slug,
            "temperature": self._cfg.generation.temperature,
            "max_tokens": self._cfg.generation.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        # the [stage|model] prefix matches the pattern the runner uses
        # for progress lines, so log files stay scannable.
        self._log.info(
            f"[bold cyan]→ openrouter[/]  stage=[yellow]{stage}[/]  "
            f"model=[magenta]{model.slug}[/]  in_chars={len(system) + len(user)}"
        )

        t0 = time.perf_counter()
        try:
            resp = self._http.post("/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise OpenRouterError(f"network error calling OpenRouter: {e}") from e
        dt = time.perf_counter() - t0

        if resp.status_code != 200:
            # log the body so 401 / 429 / 5xx is debuggable from the log alone.
            self._log.error(
                f"OpenRouter returned {resp.status_code}: {resp.text[:400]}"
            )
            raise OpenRouterError(
                f"OpenRouter HTTP {resp.status_code} for stage={stage}, model={model.slug}"
            )

        data: dict[str, Any] = resp.json()
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise OpenRouterError(
                f"unexpected OpenRouter response shape: {json.dumps(data)[:400]}"
            ) from e

        # token usage: OpenRouter mostly mirrors the OpenAI shape. we
        # default to 0 if a provider didn't report it, rather than crash.
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))
        cost = usd(model, in_tok, out_tok)

        self._log.info(
            f"[bold green]✓ openrouter[/]  stage=[yellow]{stage}[/]  "
            f"in={in_tok}tok  out={out_tok}tok  "
            f"cost=${cost.total_usd:.6f}  latency={dt:.2f}s"
        )

        return LLMReply(
            content=content,
            raw=data,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost=cost,
            latency_s=dt,
        )
