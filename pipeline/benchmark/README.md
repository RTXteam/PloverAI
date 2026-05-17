# Benchmark

Input data for the v15 grounded benchmark of 8 LLMs on 10 gold
one-hop TRAPI questions.

## Contents

- [`golden_questions/questions.json`](golden_questions/questions.json) — the 10 gold questions, each with a NL question, a pinned-entity record (used **only** by the offline scorer, never by the LLM), the gold TRAPI query graph, and at least one external-evidence anchor CURIE with citation.

## Read-only at runtime

This folder is **input** to the pipeline. The pipeline never writes
here. Run outputs go to `../code/outputs/RUN_<timestamp>/`. See
[`../code/README.md`](../code/README.md) for the full per-question
flow and disk layout.

## Scope

- 8 models × 10 questions = 80 grounded runs per full benchmark.
- Grounded only — no ungrounded baseline. the premise being tested
  is the value of graph grounding, so the ungrounded variant is out
  of scope for this benchmark.
- Generation: `temperature=0` where the provider supports it.
- `n = 10` is small: results are reported as descriptive paired
  evidence, not significance claims.
