# Benchmark

Input data for the benchmark: the question sets and their gold
answers.

## Contents

- [`golden_questions/translator/`](golden_questions/translator/): 81
  expert-labelled questions converted from NCATS Translator Tests, each
  in two phrasings (162 records). The held-out test set.
- [`golden_questions/evidence/`](golden_questions/evidence/): 19
  curated one-hop questions (q1..q19) with a pinned entity, the gold
  predicate and answer category, and verified answers. q1..q10 are dev,
  q11..q19 are test.
- [`golden_questions/irrelevant/`](golden_questions/irrelevant/): 10
  out-of-scope questions that Stage 1 must refuse.

See [golden_questions/README.md](golden_questions/README.md) for the
record formats and what was measured on which graph.

## Read-only at runtime

This folder is input to the pipeline. The pipeline never writes here.
Run outputs go to `../code/outputs/RUN_<timestamp>/`. See
[`../code/README.md`](../code/README.md) for the per-question flow and
the disk layout.

## Rules

- Prompts are tuned only on dev questions. On test questions only
  crashes and plumbing get fixed, never answer quality.
- The model roster (5 frontier, 5 budget, 1 free dev model) is in
  `../config.yaml`. The dev model is for pilots only and is not part of
  the grid.
- `temperature=0` where the provider supports it. The OpenAI models
  ignore it, so a subset of questions is repeated.
