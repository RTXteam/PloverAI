# Gold Questions

One-hop questions with their gold query and labelled or verified
answers. 100 questions in total: 19 curated and 81 converted from
Translator Tests.

**Which graph.** Every set here was built and checked while PloverAI
still queried PloverDB (RTX-KG2c). The gold entities, predicates and
labels do not depend on the graph. Three things do, and must be
re-checked on Tier 0 before they are used in results: the one-hop
reachability stored in `tr*.json`, the `validation` block of each
`q*.json`, and the "should abstain" gold of q14, q15, q18 and q19.

## `evidence/` (curated, q1..q19)

One self-contained record per question: NL question, pinned-entity
record, gold answer category and predicate, `validation` provenance,
`verification_guide`, and `verified_answers` (gold answer CURIEs with
evidence anchors). The loader (`code/config.py`, `load_questions`)
reads every `q*.json` here in alphanumeric order, so a new file is
picked up automatically.

- q1..q10 are split `dev`: prompts may be tuned on them.
- q11..q19 are split `test` (held out).
  - q11..q13 add three relation types: drug to metabolising enzyme,
    exposure to disease, and disease to genes.
  - q14 and q15 are no-answer questions: the correct one-hop query
    returned nothing in KG2c, while looser predicates return plausible
    facts. They carry `expected_behavior: abstain`.
  - q16 is a contraindication question that has answers, the pair of
    q15. q17 is disease to anatomical location.
  - q18 (trofinetide side effects) and q19 (metaxalone's molecular
    target) are two more no-answer traps. For q19 the true target is
    also unknown in the literature, which matters when reading
    abstention rates.

## `translator/` (NCATS Translator Tests)

The Translator Tests assets (github.com/NCATSTranslator/Tests),
converted by `code/translator_assets.py`. Split `test` (held out).

- Each `tr*.json` is one expert-labelled question with its TopAnswer,
  Acceptable and NeverShow answers. Every answer carries its NodeNorm
  clique and its one-hop reachability (gold query, predicate without
  qualifiers, any predicate), measured on KG2c at conversion time on
  2026-09-28. The converter now probes Retriever (Tier 0) instead, so a
  re-run refreshes these numbers.
- Each question is asked in two phrasings: a fixed template, and a
  hand-checked paraphrase from `paraphrases.json`.
- `manifest.json` records the source commit, the counts and the
  excluded questions.

## `irrelevant/`

10 out-of-scope questions for the Stage 1 scope check. See
`irrelevant/README.md`. They are not part of the model grid.
