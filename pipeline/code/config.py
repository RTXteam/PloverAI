# config.py — typed loader for pipeline/config.yaml and the gold
# question files. every module that needs a model spec, an endpoint
# URL, or a path goes through here. nothing else in the pipeline
# reads YAML or JSON directly for configuration purposes.

from __future__ import annotations

# json: stdlib. used to parse the per-question gold files under
# benchmark/golden_questions/evidence/ (q*.json) and translator/
# (tr*.json). we only ever read them; writes go through trace.py.
import json

# dataclasses: stdlib. all configuration objects are frozen dataclasses
# so a typo on a field name fails fast and so the whole config is
# hashable/printable for free.
from dataclasses import dataclass

# pathlib.Path: stdlib. all paths in this codebase are pathlib paths,
# never raw strings, so we get cross-platform joining and easy `.exists()`.
from pathlib import Path

# typing.Any: stdlib. we only use Any for dicts loaded from YAML/JSON
# whose shape is documented in the source files themselves.
from typing import Any

# yaml: PyYAML. parses pipeline/config.yaml — the single source of
# truth for models, prices, endpoints, and on-disk paths. third-party,
# pinned in requirements.txt.
import yaml


# pipeline root resolves to .../PloverAI/pipeline/. this file lives at
# pipeline/code/config.py, so we climb two parents to reach it.
# everything the backend reads or writes is relative to this folder,
# so the YAML stays portable and the project root stays tidy.
# resolving from this file's location means scripts can be run from
# any working directory.
PIPELINE_ROOT = Path(__file__).resolve().parent.parent

# the YAML config sits at the pipeline root (next to requirements.txt,
# pyproject.toml, etc.), NOT inside code/. keeping the literal path
# here (and only here) means anyone moving the config file only edits
# one line.
CONFIG_PATH = PIPELINE_ROOT / "config.yaml"


@dataclass(frozen=True)
class ModelSpec:
    # one row of the models: table from config.yaml.
    id: str            # m0..m10 — short label used in folder names
    tier: str          # "frontier" | "budget" | "dev" (dev = pilots only)
    slug: str          # full OpenRouter model string we POST in the body
    provider: str      # anthropic / google / openai / xai / moonshotai / zai / nvidia
    price_in: float    # USD per 1M input tokens
    price_out: float   # USD per 1M output tokens


@dataclass(frozen=True)
class Endpoints:
    # all external services the pipeline talks to. each value is a
    # BASE URL; the corresponding client appends the path it needs
    # (e.g. the Retriever client appends /query and /meta_knowledge_graph).
    # centralised here so swapping providers / staging mirrors is a
    # one-line change in config.yaml.
    arax: str        # base, e.g. https://arax.ncats.io/api/arax/v1.4
    retriever: str   # base, e.g. https://retriever.ci.transltr.io
    openrouter: str  # base, e.g. https://openrouter.ai/api/v1
    nameres: str     # base, e.g. https://name-resolution-sri.renci.org
    nodenorm: str    # base, e.g. https://nodenormalization-sri.renci.org
    pubtator: str    # base, e.g. https://www.ncbi.nlm.nih.gov/research/pubtator3-api


@dataclass(frozen=True)
class Generation:
    # LLM sampling parameters. shared across every stage of every model.
    temperature: float
    max_tokens: int
    request_timeout_s: int


@dataclass(frozen=True)
class Reduction:
    # response reduction (Stage 11 pre-step, see code/reduction.py).
    # top_k_edges is the only knob: how many ranked edges the LLM is
    # allowed to see. it lives here rather than in reduction.py so the
    # cut size is a recorded experimental parameter, not a literal.
    top_k_edges: int


@dataclass(frozen=True)
class Arax:
    # ARAX mode knobs (see the arax: block in config.yaml). recorded
    # parameters of the method, like reduction.top_k_edges.
    timeout_s: int
    top_k_answers: int
    paths_per_answer: int


@dataclass(frozen=True)
class Retriever:
    # Tier 0 access through Retriever (see the retriever: block in
    # config.yaml).
    tiers: tuple[int, ...]
    probe_candidates: int
    probe_timeout_s: int


@dataclass(frozen=True)
class PublicLimits:
    # what the service allows on the open internet, where there is no
    # login (see the public: block in config.yaml). only used when
    # PLOVERAI_PUBLIC=1; enforced by public_guard.PublicGuard.
    models: tuple[str, ...]
    questions_per_ip_per_hour: int
    questions_per_day: int
    concurrent_runs: int
    concurrent_runs_per_ip: int


@dataclass(frozen=True)
class Paths:
    # absolute filesystem locations derived from the YAML's relative paths.
    questions: Path    # benchmark/golden_questions/evidence/ (directory of q*.json)
    translator_questions: Path  # benchmark/golden_questions/translator/ (directory of tr*.json)
    examples: Path     # examples.yaml, the web UI's example questions
    results: Path      # code/outputs/<model>/<run>/...
    logs: Path         # code/logs/<run>.log


@dataclass(frozen=True)
class Config:
    endpoints: Endpoints
    models: list[ModelSpec]
    generation: Generation
    reduction: Reduction
    arax: Arax
    retriever: Retriever
    public: PublicLimits
    paths: Paths

    def model(self, model_id: str) -> ModelSpec:
        # tiny lookup helper. raising explicitly here means a typo in
        # `--models m9` fails at the CLI boundary, not deep inside a run.
        for m in self.models:
            if m.id == model_id:
                return m
        raise KeyError(
            f"unknown model id '{model_id}' "
            f"(known: {[m.id for m in self.models]})"
        )


def load_config() -> Config:
    raw: dict[str, Any] = yaml.safe_load(CONFIG_PATH.read_text())
    eps = Endpoints(**raw["endpoints"])
    gen = Generation(**raw["generation"])
    red = Reduction(**raw["reduction"])
    arax = Arax(**raw["arax"])
    retriever_raw = raw["retriever"]
    retriever = Retriever(
        tiers=tuple(retriever_raw["tiers"]),
        probe_candidates=retriever_raw["probe_candidates"],
        probe_timeout_s=retriever_raw["probe_timeout_s"],
    )
    public_raw = raw["public"]
    public = PublicLimits(
        models=tuple(public_raw["models"]),
        questions_per_ip_per_hour=public_raw["questions_per_ip_per_hour"],
        questions_per_day=public_raw["questions_per_day"],
        concurrent_runs=public_raw["concurrent_runs"],
        concurrent_runs_per_ip=public_raw["concurrent_runs_per_ip"],
    )

    # paths in the YAML are pipeline-relative (resolved against
    # PIPELINE_ROOT below) so the YAML file itself stays portable
    # between machines and the project root stays uncluttered.
    p = raw["paths"]
    paths = Paths(
        questions=PIPELINE_ROOT / p["questions"],
        translator_questions=PIPELINE_ROOT / p["translator_questions"],
        examples=PIPELINE_ROOT / p["examples"],
        results=PIPELINE_ROOT / p["results"],
        logs=PIPELINE_ROOT / p["logs"],
    )

    models = [ModelSpec(**m) for m in raw["models"]]
    return Config(
        endpoints=eps, models=models, generation=gen, reduction=red, arax=arax,
        retriever=retriever, public=public, paths=paths,
    )


def load_examples(cfg: Config) -> list[str]:
    # the web UI's example questions, in their order: plain strings, no
    # gold record behind them (see pipeline/examples.yaml).
    raw = yaml.safe_load(cfg.paths.examples.read_text()) or {}
    return [str(q).strip() for q in raw.get("examples") or [] if str(q).strip()]


def load_questions(cfg: Config) -> list[dict[str, Any]]:
    # the gold set is a directory of per-question JSON files
    # (q1.json, q2.json, ...), each self-contained. we sort by the
    # number so q10 comes after q9 (the filenames are not zero-padded).
    qdir = cfg.paths.questions
    if not qdir.is_dir():
        raise RuntimeError(
            f"paths.questions must point at the per-question directory "
            f"(got {qdir!r}); expected a directory of q*.json files"
        )
    # natural sort: q1, q2, ..., q9, q10  (not q1, q10, q2)
    paths = sorted(
        qdir.glob("q*.json"),
        key=lambda p: int(p.stem[1:]) if p.stem[1:].isdigit() else 1_000_000,
    )
    records = [json.loads(p.read_text()) for p in paths]
    # the per-question files carry `question_id`; the pipeline and
    # runner address questions by `id` (the same key ad-hoc questions
    # use), so alias it here once instead of teaching every call site
    # both names. an explicit `id` in the file wins.
    for rec in records:
        if "id" not in rec:
            rec["id"] = rec["question_id"]
    return records


# the two phrasings every converted Translator question carries, in the
# order they run, and the id suffix each phrasing gets.
PHRASING_SUFFIXES: dict[str, str] = {"template": "t", "paraphrase": "p"}


def _natural_key(path: Path) -> tuple[str, int]:
    # "tr10" sorts after "tr9": split the stem into its letter prefix
    # and its trailing number.
    stem = path.stem
    digits = len(stem) - len(stem.rstrip("0123456789"))
    prefix, number = stem[: len(stem) - digits], stem[len(stem) - digits:]
    return prefix, int(number) if number else -1


def load_translator_questions(directory: Path) -> list[dict[str, Any]]:
    # one tr*.json file = one question in up to two phrasings. each
    # phrasing becomes its own record (tr01t, tr01p) because each is
    # its own benchmark cell; group_id ties the pair back together for
    # the robustness comparison.
    if not directory.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("tr*.json"), key=_natural_key):
        base = json.loads(path.read_text())
        for phrasing, suffix in PHRASING_SUFFIXES.items():
            text = (base.get("phrasings") or {}).get(phrasing)
            if not text:
                continue
            records.append({
                **base,
                "id": f"{base['question_id']}{suffix}",
                "group_id": base["question_id"],
                "phrasing": phrasing,
                "nl_question": text,
            })
    return records


def load_benchmark_questions(cfg: Config) -> list[dict[str, Any]]:
    # everything the runner and scorer operate on: the hand-curated
    # q*.json set followed by the converted Translator questions. the
    # UI's question dropdown keeps using load_questions (curated only).
    return load_questions(cfg) + load_translator_questions(cfg.paths.translator_questions)
