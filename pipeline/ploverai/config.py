# config.py — typed loader for pipeline/config.yaml and the gold
# question file. every module that needs a model spec, an endpoint
# URL, or a path goes through here. nothing else in the pipeline
# reads YAML or JSON directly for configuration purposes.

from __future__ import annotations

# json: stdlib. used to parse benchmark/golden_questions/questions.json
# — the gold question set. we only ever read it; writes go through
# trace.py.
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
# pipeline/ploverai/config.py, so we climb two parents to reach it.
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
    id: str            # m1..m8 — short label used in folder names
    tier: str          # "frontier" | "budget"
    slug: str          # full OpenRouter model string we POST in the body
    provider: str      # anthropic / google / openai / xai / deepseek
    price_in: float    # USD per 1M input tokens
    price_out: float   # USD per 1M output tokens


@dataclass(frozen=True)
class Endpoints:
    # all external services the pipeline talks to. each value is a
    # BASE URL; the corresponding client appends the path it needs
    # (e.g. PloverDB client appends /query and /meta_knowledge_graph).
    # centralised here so swapping providers / staging mirrors is a
    # one-line change in config.yaml.
    ploverdb: str    # base, e.g. https://kg2cploverdb.ci.transltr.io
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
class Paths:
    # absolute filesystem locations derived from the YAML's relative paths.
    questions: Path    # benchmark/golden_questions/evidence/ (directory of q*.json)
    results: Path      # code/outputs/<model>/<run>/...
    logs: Path         # code/logs/<run>.log


@dataclass(frozen=True)
class Config:
    endpoints: Endpoints
    models: list[ModelSpec]
    generation: Generation
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

    # paths in the YAML are pipeline-relative (resolved against
    # PIPELINE_ROOT below) so the YAML file itself stays portable
    # between machines and the project root stays uncluttered.
    p = raw["paths"]
    paths = Paths(
        questions=PIPELINE_ROOT / p["questions"],
        results=PIPELINE_ROOT / p["results"],
        logs=PIPELINE_ROOT / p["logs"],
    )

    models = [ModelSpec(**m) for m in raw["models"]]
    return Config(endpoints=eps, models=models, generation=gen, paths=paths)


def load_questions(cfg: Config) -> list[dict[str, Any]]:
    # the gold set is a directory of per-question JSON files
    # (q1.json … q10.json), each self-contained. we iterate in
    # alphanumeric order so q10 sorts after q9 (zero-padded would
    # be cleaner but the existing filenames are q1…q10).
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
    return [json.loads(p.read_text()) for p in paths]
