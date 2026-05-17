# Gold Questions

One-hop TRAPI queries with verified answer sets and external evidence.

Contents:
- `questions.json` — NL question, pinned-entity record, gold TRAPI
  query graph, and at least one external-evidence anchor (CURIE +
  source citation) per question.
- `answer_sets.json` (planned) — gold answer CURIEs per question,
  obtained by executing each gold query against PloverDB with
  pagination.
- `evidence/` (planned) — per-question runlogs with API call records
  and external-evidence anchors.
