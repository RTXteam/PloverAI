# trace.py — owns the on-disk artifact layout for a benchmark run.
# rule: never overwrite anything. every (model, run_timestamp) is its
# own folder; every question inside that is its own folder. re-running
# a (model, question) pair creates a brand new run_timestamp folder
# rather than silently editing the previous one.

from __future__ import annotations

# json: stdlib. all artifacts except explanation.txt are JSON. we use
# json.dumps directly here (not a wrapper) so the output stays
# diffable in code editors.
import json

# dataclasses: stdlib. the path / ledger types are dataclasses so they
# print well and aren't accidentally mutated by callers.
from dataclasses import dataclass, field

# pathlib.Path: stdlib. all on-disk locations are Path, never str.
from pathlib import Path

# typing.Any: stdlib. cost ledger entries are heterogenous dicts that
# get JSON-serialised verbatim, so Any is the honest type here.
from typing import Any


@dataclass
class RunPaths:
    # one of these per (model, run_timestamp). owns the run-level
    # metadata file (run.json) — per-question files live in QuestionPaths.
    root: Path                       # benchmark/results/<model>/<run>/
    run_meta: Path                   # root/run.json
    log_file: Path | None = None     # set by the runner once logging is wired


@dataclass
class QuestionPaths:
    # one of these per (model, run_timestamp, condition, question).
    # explanation.md is the only non-JSON file because its content is
    # a structured Markdown document (Answer / Evidence / Confidence /
    # Limitations); the UI renders it directly. everything else is JSON.
    root: Path                       # <run_root>/<condition>/<q_id>/
    question: Path                   # frozen copy of the gold record
    prompt: Path                     # what we sent to the LLM, per stage
    nameres: Path                    # Stage 3: NameRes lookup result (top-N candidates)
    candidate_probes: Path           # Stage 4 setup: per-candidate edge-density probe
                                     # against the answer category (one entry per top-K
                                     # NameRes candidate; informs Stage 4's pick so the
                                     # LLM prefers CURIEs with non-zero KG2c coverage)
    nodenorm: Path                   # Stage 6 (pinned) + Stage 12 (answers)
    predicate_probe: Path            # Stage 8 setup: chosen-CURIE predicate-density probe
                                     # (per-predicate edge counts from the pinned CURIE to the
                                     # answer category, in either direction; informs the
                                     # LLM's predicate choice in Stage 8). this is the SAME
                                     # data as candidate_probes[chosen_curie] — duplicated as
                                     # its own file for backward compatibility with readers
                                     # that look up the chosen-CURIE probe by file name.
    trapi_query: Path                # LLM-built TRAPI query graph
    validation: Path                 # reasoner-validator report
    plover_request: Path             # exact body POSTed to PloverDB
    plover_response: Path            # raw response from PloverDB
    reduced_data: Path               # PloverDB response after Strategy B
                                     # reduction; this is what Stage 11
                                     # actually sees, and what the
                                     # faithfulness evaluator grades
                                     # answers against
    reduction_metadata: Path         # per-predicate kept/dropped counts +
                                     # the strategy + N used. lets the
                                     # benchmark correlate answer quality
                                     # with reduction stats
    answer: Path                     # CURIEs the LLM picked from the response
    answer_graph_view: Path          # Stage 13: research-grade node-link view
                                     # (pinned + answer nodes + edges with provenance)
                                     # for frontend graph rendering
    explanation: Path                # structured Markdown summary
    cost: Path                       # per-stage cost ledger + totals
    meta: Path                       # status + error + elapsed_s

    @classmethod
    def under(cls, parent: Path, q_id: str) -> QuestionPaths:
        # parent here is <run_root>/<condition>/. the question folder is
        # created on demand; callers don't have to mkdir explicitly.
        d = parent / q_id
        d.mkdir(parents=True, exist_ok=True)
        return cls(
            root=d,
            question=d / "question.json",
            prompt=d / "prompt.json",
            nameres=d / "nameres.json",
            candidate_probes=d / "candidate_probes.json",
            nodenorm=d / "nodenorm.json",
            predicate_probe=d / "predicate_probe.json",
            trapi_query=d / "trapi_query.json",
            validation=d / "validation.json",
            plover_request=d / "plover_request.json",
            plover_response=d / "plover_response.json",
            reduced_data=d / "reduced_data.json",
            reduction_metadata=d / "reduction_metadata.json",
            answer=d / "answer.json",
            answer_graph_view=d / "answer_graph_view.json",
            explanation=d / "explanation.md",
            cost=d / "cost.json",
            meta=d / "meta.json",
        )


@dataclass
class CostLedger:
    # accumulates LLM cost across the multiple stages of one question
    # (TRAPI build, answer pick, explanation). each entry is already a
    # dict so we can dump it straight to cost.json without conversion.
    entries: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        stage: str,
        model_id: str,
        model_slug: str,
        input_tokens: int,
        output_tokens: int,
        input_usd: float,
        output_usd: float,
        total_usd: float,
        latency_s: float,
    ) -> None:
        self.entries.append({
            "stage": stage,
            "model_id": model_id,
            "model_slug": model_slug,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_usd": input_usd,
            "output_usd": output_usd,
            "total_usd": total_usd,
            "latency_s": round(latency_s, 3),
        })

    def total_usd(self) -> float:
        # mypy can't see that entries["total_usd"] is always a float
        # (the dict is `dict[str, Any]`), so we cast through float() to
        # keep the return type honest.
        return round(float(sum(float(e["total_usd"]) for e in self.entries)), 6)

    def total_tokens(self) -> tuple[int, int]:
        ti = int(sum(int(e["input_tokens"]) for e in self.entries))
        to = int(sum(int(e["output_tokens"]) for e in self.entries))
        return ti, to


def make_run_dir(results_root: Path, run_timestamp: str) -> Path:
    # one folder per invocation of the runner. all models that ran in
    # this invocation live as siblings inside it. format:
    # outputs/RUN_<utc_timestamp>/
    d = results_root / f"RUN_{run_timestamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_run_root(
    run_dir: Path,
    model_id: str,
    model_slug: str,
) -> RunPaths:
    # one folder per (run, model). lives INSIDE the run folder, so the
    # full path is outputs/RUN_<ts>/<model_id>_<safe_slug>/. a glance at
    # disk tells you which models were run together (siblings) and what
    # each one is (folder name) without opening run.json.
    safe_slug = model_slug.replace("/", "_").replace(":", "_")
    folder = f"{model_id}_{safe_slug}"
    root = run_dir / folder
    root.mkdir(parents=True, exist_ok=True)
    return RunPaths(root=root, run_meta=root / "run.json")


def write_json(path: Path, data: Any) -> None:
    # pretty-print so a human can diff two runs in a code editor.
    # ensure_ascii=False keeps unicode (e.g. Greek letters in disease
    # names) readable instead of \u-escaped.
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def write_text(path: Path, text: str) -> None:
    path.write_text(text)
