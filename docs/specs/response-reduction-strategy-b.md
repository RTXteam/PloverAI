# Spec: Response reduction (Strategy B)

Status: DRAFT — awaiting user redline before implementation.

## 1. Intent

Replace the current "truncate the PloverDB body to 200 000 characters and
hope" approach in `pipeline.py` Stage 11 (answer_pick) and Stage 15
(explain) with a principled reduction that:

1. **Preserves predicate diversity.** Every distinct predicate present in
   the PloverDB response should be represented in the LLM's view, so the
   LLM cannot miss whole relationship types because of JSON ordering.
2. **Ranks edges by curation strength.** Within each predicate group,
   stronger evidence (`knowledge_assertion`, `manual_agent`) appears before
   weaker (`prediction`, `text_mining_agent`). The LLM's attention bias
   favours the top of the prompt; reduction should put the best evidence
   there.
3. **Keeps full TRAPI Attributes for retained edges.** Publications,
   supporting_text, qualifiers — everything an answer would need to be
   cited. No "tabular" lossy transformation.
4. **Records what was dropped, deterministically.** Each run writes a
   `reduced_data` artifact so the faithfulness metric for RQ3 can compare
   LLM claims against what the LLM *saw*, not against the full PloverDB
   response.
5. **Tightens Stage 15.** The explanation step receives only the edges
   the LLM picked at Stage 11, not the full PloverDB body. This is a
   structural anti-hallucination measure: the LLM cannot cite edges that
   are not in its prompt.

The reduction is **not adaptive**. The same algorithm runs regardless of
response size. If the response is small the reduction is effectively a
no-op (top-N caps don't bind); if it is large the reduction takes the
top-N per predicate. Either way, the same code path, the same artifact
shape, the same faithfulness contract.

## 2. Public function

```python
def reduce_plover_response(
    plover_body: dict[str, Any],
    *,
    top_n_per_predicate: int,
    logger: logging.Logger,
) -> ReductionResult:
    ...
```

`ReductionResult` is a frozen dataclass:

```python
@dataclass(frozen=True)
class ReductionResult:
    reduced_body: dict[str, Any]      # same shape as plover_body, fewer
                                      # results / edges. safe to json.dumps
                                      # into a Stage 11 prompt.
    metadata: ReductionMetadata       # bookkeeping for the artifact +
                                      # faithfulness eval.
```

`ReductionMetadata`:

```python
@dataclass(frozen=True)
class ReductionMetadata:
    strategy_applied: str             # always "B" in this version
    top_n_per_predicate: int          # the N actually used
    original_result_count: int
    original_edge_count: int
    original_node_count: int
    reduced_result_count: int
    reduced_edge_count: int
    reduced_node_count: int
    predicate_groups: list[str]       # predicate URIs, in the order the
                                      # reduction iterated them. sorted
                                      # alphabetically — see §4.5.
    edges_kept_per_group: dict[str, int]
    edges_dropped_per_group: dict[str, int]
```

## 3. Operating unit: results, not loose edges

Important framing decision: TRAPI's `results` array IS the candidate-
answer list. Each result binds query-graph nodes to specific
knowledge-graph nodes, and query-graph edges to one-or-more knowledge-
graph edges (the `edge_bindings`). Dropping a knowledge-graph edge that
a result depends on would silently break that result.

Therefore Strategy B operates on **results**, not on raw
`knowledge_graph.edges`:

1. Each result has at most one one-hop edge (we are a one-hop pipeline).
2. We group RESULTS by the predicate of their primary edge.
3. We rank RESULTS within each group.
4. We take top-N RESULTS per group.
5. We rebuild `knowledge_graph` as the union of nodes + edges referenced
   by the surviving results, plus auxiliary graphs the same way.

This is cleaner than reducing the loose edge list because the answer is
always picked from `results`, not from `knowledge_graph.edges` directly.

## 4. Algorithm

### 4.1 Inputs

- `plover_body`: the TRAPI `prep.body` dict written to
  `qp.plover_response`. We treat it as read-only.
- `top_n_per_predicate`: positive integer. Default 10 in v1.
- `logger`: standard project logger.

### 4.2 Read the relevant blocks

```
message       = plover_body["message"]
results       = message.get("results", []) or []
kg            = message.get("knowledge_graph", {}) or {}
kg_nodes      = kg.get("nodes", {}) or {}
kg_edges      = kg.get("edges", {}) or {}
aux_graphs    = message.get("auxiliary_graphs", {}) or {}
query_graph   = message.get("query_graph", {}) or {}  # passthrough
```

If `results` is empty OR `kg_edges` is empty: return a no-op result that
echoes the body unchanged and records all `original_*` counts equal to
the corresponding `reduced_*` counts. Strategy "B" is still recorded as
applied.

### 4.3 Per-result enrichment

In TRAPI 1.5, `edge_bindings` lives under
`result["analyses"][i]["edge_bindings"]`, not directly on the result.
Each analysis is one path through the query graph using one resource
(e.g. `infores:rtx-kg2`). A single result CAN bind multiple kg edges
to the same qg edge — this happens when several KG2c sources
independently assert the same fact (e.g. an edge from SemMedDB AND an
edge from an automated source both pointing CFTR → cystic fibrosis).

For each result we compute:

- `bound_edge_ids`: every kg edge id the result binds, across every
  analysis and every binding-key, in insertion order, deduplicated.
  If the result has no resolvable bindings at all, the result is
  dropped (TRAPI-malformed, logged at WARNING).
- `representative_edge_id`: of all `bound_edge_ids` that exist in
  `kg_edges`, the one whose individual sort key (§4.5) is smallest
  (i.e. strongest). The result inherits this edge's sort key and
  predicate for grouping purposes.

**Why "strongest edge represents the result."** A result that binds
both a 20-PMID text-mining edge AND a 1-PMID automated edge is
*better-corroborated* than a result that binds only the 1-PMID edge.
Letting the strong edge represent the result rewards multi-source
corroboration. When a result survives the top-N, ALL of its
`bound_edge_ids` (not just the representative) are kept in the
reduced `knowledge_graph.edges` block, so the full provenance is
preserved.
- `primary_predicate`: `kg_edges[primary_edge_id]["predicate"]`. If the
  edge is missing from `kg_edges` the result is dropped — that is a
  TRAPI-malformedness signal, logged at WARNING.
- `(rank_kl, rank_at, n_pubs, edge_id)`: the four-key sort tuple. See §4.5.

### 4.4 Group by predicate

```
groups: dict[str, list[ResultRow]] = {}
for row in enriched_results:
    groups.setdefault(row.primary_predicate, []).append(row)
```

### 4.5 Rank within each group

The sort tuple is `(rank_kl, -n_pubs, rank_at, edge_id)`, ascending:

| Position | Meaning | Stronger value comes first |
|----------|---------|-----------------------------|
| 0 | `rank_kl` — knowledge_level rank | smaller integer = stronger |
| 1 | `-n_pubs` — negative count of `supporting_publications` | more pubs = more negative = first |
| 2 | `rank_at` — agent_type rank | smaller integer = stronger |
| 3 | `edge_id` — alphabetical | tie-break for full determinism |

**Why `n_pubs` outranks `agent_type`.** Publication count is a
continuous signal of cross-source corroboration — 20 supporting PMIDs
means 20 independent papers mention the relationship, regardless of
which extraction tool tagged the edge. `agent_type` is a binary label
about *who/what* produced the assertion (a manual curator vs an
automated tool vs a text-miner) and within a single `knowledge_level`
group it discriminates much less than n_pubs does.

This ordering was confirmed empirically against a failing CFTR ↔
cystic fibrosis case. In KG2c, every gene-disease edge has
`knowledge_level=prediction`, so the primary key ties for the whole
predicate group. The textbook association (CFTR ↔ cystic fibrosis,
20 PMIDs, `text_mining_agent`) was being dropped by Strategy B in
favour of single-PMID `automated_agent` edges to unrelated diseases,
because `agent_type` ranked ahead of `n_pubs`. Promoting `n_pubs`
restored the canonical answer to the top-N.

`rank_kl` mapping (lower is stronger):

| Attribute value | Rank |
|---|---|
| `knowledge_assertion` | 0 |
| `logical_entailment` | 1 |
| `prediction` | 2 |
| `statistical_association` | 3 |
| `observation` | 4 |
| `not_provided` (or attribute missing entirely) | 5 |
| anything else | 6 |

`rank_at` mapping:

| Attribute value | Rank |
|---|---|
| `manual_agent` | 0 |
| `automated_agent` | 1 |
| `text_mining_agent` | 2 |
| `computational_model` | 3 |
| `not_provided` (or attribute missing) | 4 |
| anything else | 5 |

`n_pubs` is the length of the edge's `biolink:publications` attribute
value list, or 0 if the attribute is missing or its value is not a list.

### 4.6 Take top-N per group

```
kept_results: list[ResultRow] = []
for predicate in sorted(groups.keys()):    # deterministic order
    group = sorted(groups[predicate], key=lambda r: r.sort_key)
    kept_results.extend(group[:top_n_per_predicate])
    edges_kept_per_group[predicate] = min(len(group), top_n_per_predicate)
    edges_dropped_per_group[predicate] = max(0, len(group) - top_n_per_predicate)
```

We iterate `sorted(groups.keys())` so the output ordering is
deterministic across runs. This is important for prompt caching at the
OpenRouter layer — identical reduced bodies have identical prompt
prefixes.

### 4.7 Rebuild the knowledge_graph and auxiliary_graphs

The reduced TRAPI message contains only the entities referenced by
surviving results.

```
retained_edge_ids: set[str] = {row.primary_edge_id for row in kept_results}
retained_node_ids: set[str] = set()
for row in kept_results:
    # collect every node id mentioned by the result's node_bindings
    for node_bindings_for_qg_node in row.node_bindings.values():
        for binding in node_bindings_for_qg_node:
            retained_node_ids.add(binding["id"])

retained_aux_graph_ids: set[str] = set()
for edge_id in retained_edge_ids:
    edge = kg_edges[edge_id]
    for attr in edge.get("attributes", []):
        if attr.get("attribute_type_id") == "biolink:support_graphs":
            value = attr.get("value")
            if isinstance(value, list):
                retained_aux_graph_ids.update(v for v in value if isinstance(v, str))
```

The reduced `kg_nodes` is `{nid: kg_nodes[nid] for nid in retained_node_ids if nid in kg_nodes}`.

The reduced `kg_edges` is `{eid: kg_edges[eid] for eid in retained_edge_ids}`.

The reduced `aux_graphs` is the same filter against `retained_aux_graph_ids`.

### 4.8 Assemble the reduced body

```
reduced_body = {
    **plover_body,
    "message": {
        **message,
        "query_graph": query_graph,
        "knowledge_graph": {
            "nodes": reduced_kg_nodes,
            "edges": reduced_kg_edges,
        },
        "results": [row.original for row in kept_results],
        "auxiliary_graphs": reduced_aux_graphs,
    },
}
```

Any top-level fields in `plover_body` outside `message` (e.g.
`workflow`, `schema_version`) are preserved verbatim.

### 4.9 Logging

Single INFO line at exit:

```
[bold cyan]→ reduction[/]  strategy=B  predicates=<P>  results=<O>→<R>  edges=<O>→<R>  nodes=<O>→<R>  top_n=<N>
```

No WARNING logs are emitted for the normal case. The malformed-TRAPI
case (a result whose `primary_edge_id` is not in `kg_edges`) emits one
WARNING per such result with the result's id and the missing edge id.

## 5. Where the reduction is invoked

### 5.1 Stage 11 (answer_pick) — `pipeline.py` around line 1641

Before:

```python
user_msg_4 = (
    f"User question: {nl_question}\n\n"
    f"TRAPI response (whole message):\n"
    f"{json.dumps(prep.body, ensure_ascii=False)[:200_000]}\n"
)
```

After:

```python
reduction = reduce_plover_response(
    prep.body,
    top_n_per_predicate=cfg.reduction.top_n_per_predicate,
    logger=logger,
)
write_json(qp.reduced_data, reduction.reduced_body)
write_json(qp.reduction_metadata, asdict(reduction.metadata))
user_msg_4 = (
    f"User question: {nl_question}\n\n"
    f"TRAPI response (reduced — top {reduction.metadata.top_n_per_predicate} "
    f"results per predicate, ranked by knowledge_level then agent_type):\n"
    f"{json.dumps(reduction.reduced_body, ensure_ascii=False)}\n"
)
```

The 200 000 character truncation is **deleted**, not kept as a safety
net. If the reduced body is still too large for the model's context that
is a data-collection signal we want to expose, not paper over. The
benchmark eval will count such failures.

### 5.2 Stage 15 (explain) — `pipeline.py` around line 1875

Before:

```python
user_msg_5 = (
    f"User question: {nl_question}\n\n"
    f"Selected answers (Stage 11):\n{json.dumps(answer_obj, ensure_ascii=False)}\n\n"
    f"TRAPI response (whole message):\n"
    f"{json.dumps(prep.body, ensure_ascii=False)[:200_000]}\n"
)
```

After:

```python
user_msg_5 = (
    f"User question: {nl_question}\n\n"
    f"Selected answers (Stage 11):\n{json.dumps(answer_obj, ensure_ascii=False)}\n\n"
    f"Picked-edge view (use ONLY these edges to ground citations):\n"
    f"{json.dumps(answer_graph_view, ensure_ascii=False)}\n"
)
```

The explanation step has no access to non-picked edges. This is the
structural anti-hallucination measure.

## 6. New artifact files

Two new files written to `qp.root` (alongside the existing
`plover_response.json`, `answer.json`, etc.):

- `reduced_data.json` — the reduced TRAPI body Stage 11 saw. This is the
  reference used by the faithfulness metric.
- `reduction_metadata.json` — the `ReductionMetadata` dataclass dumped to
  JSON. Includes per-predicate kept/dropped counts so the benchmark
  analysis can correlate answer quality with reduction stats.

`QuestionPaths` in `trace.py` gains two fields (`reduced_data`,
`reduction_metadata`). Existing tests in `test_*.py` that touch
`QuestionPaths.under(...)` are unaffected because field addition is
backward-compatible.

## 7. Configuration

`pipeline/config.yaml` gains one section:

```yaml
reduction:
  # see docs/specs/response-reduction-strategy-b.md for the algorithm.
  # top_n is the number of results kept per predicate group after
  # ranking by (knowledge_level, agent_type, n_publications, edge_id).
  # 10 is the v1 default. the benchmark sweeps {5, 10, 15, 20} to
  # determine the answer-quality vs cost trade-off empirically.
  top_n_per_predicate: 10
```

`pipeline/code/config.py` gains a `ReductionConfig` dataclass and
attaches it to `Config.reduction`. The loader defaults `top_n_per_predicate`
to 10 if the section is missing, so existing user configs continue to
load.

The benchmark sweeps this knob via per-run config overrides. The
intended sweep is {5, 10, 15, 20} on a fixed subset of gold questions
to characterise the answer-quality vs token-cost curve. Whichever value
wins the sweep becomes the published default.

## 8. Edge cases & failure modes

| Case | Handling |
|---|---|
| `results` is empty | no-op, reduction returns body unchanged, metadata shows `original == reduced` |
| `kg_edges` is empty but `results` is non-empty | TRAPI-malformed; reduction returns body unchanged, single WARNING log |
| A result's `primary_edge_id` is not in `kg_edges` | that result is dropped, one WARNING per occurrence with the missing edge id and the result id |
| `kg_edges[eid]["predicate"]` is missing | the result is grouped under `"<unknown>"`; that predicate goes through the same top-N selection |
| Two results bind to the same primary edge id | both are independently scored; if both survive, the deduped edge appears once in `reduced_kg_edges` |
| `attributes` block missing entirely on an edge | `rank_kl = 5`, `rank_at = 4`, `n_pubs = 0` |
| `top_n_per_predicate <= 0` | `ValueError` at the function boundary |
| `plover_body` not a dict | `TypeError` at the function boundary |

The reduction itself never raises for missing TRAPI fields; it logs and
degrades. The function-boundary `ValueError` / `TypeError` cases are
programmer-error guards, not runtime-degradation cases.

## 9. Faithfulness contract

This is the contract the evaluation harness relies on:

- A claim made by the LLM in the answer or explanation is **faithful**
  if every entity it cites and every relationship it asserts is grounded
  in `reduced_data.json`.
- A claim is **unsupported** if it cannot be grounded there.
- A claim being faithful w.r.t. `reduced_data` says nothing about
  whether the full PloverDB response also supports it. Faithfulness is
  measured against what the LLM *saw*, by design.

The benchmark eval already reads `meta.json` and the per-question
artifact dir; adding `reduced_data.json` to that dir is sufficient to
make this contract enforceable. No eval-code change is in scope for
this spec.

## 10. Unit-test plan (no code yet — list of tests to write)

In `pipeline/code/tests/test_response_reduction.py`:

### Happy-path tests
- `test_no_op_when_results_empty` — returns body unchanged, strategy="B"
- `test_no_op_when_edges_empty` — same, plus one WARNING
- `test_single_predicate_single_result_keeps_one` — degenerate input
- `test_single_predicate_many_results_keeps_top_n`
- `test_multiple_predicates_each_group_capped` — verifies grouping
- `test_predicates_iterated_in_sorted_order` — verifies determinism
- `test_attributes_preserved_for_retained_edges` — full attribute round-trip

### Ranking tests
- `test_knowledge_assertion_beats_prediction`
- `test_manual_agent_beats_text_mining_agent_within_same_kl`
- `test_more_publications_beats_fewer_within_same_kl_and_at`
- `test_edge_id_alphabetical_breaks_total_ties`
- `test_missing_kl_attribute_treated_as_not_provided`
- `test_unknown_kl_value_ranks_last`

### Graph rebuild tests
- `test_kg_nodes_filtered_to_referenced_only`
- `test_kg_edges_filtered_to_retained_only`
- `test_auxiliary_graphs_filtered_to_referenced_only`
- `test_query_graph_passthrough_unchanged`
- `test_top_level_fields_outside_message_preserved`

### Malformed-input tests
- `test_result_with_missing_primary_edge_dropped_with_warning`
- `test_two_results_same_edge_both_scored_independently`
- `test_zero_top_n_raises_value_error`
- `test_non_dict_body_raises_type_error`

### Metadata tests
- `test_metadata_counts_consistent_before_and_after`
- `test_edges_dropped_per_group_sums_to_total_dropped`
- `test_strategy_field_always_B`

Each test constructs the smallest valid TRAPI fragment necessary. No
network, no fixtures from disk. All TRAPI shapes used are documented in
TRAPI 1.5 (https://github.com/NCATSTranslator/ReasonerAPI) so the tests
also serve as a typed specification.

## 11. Resolved decisions

These were discussed and decided before implementation began:

1. **`top_n_per_predicate` default = 10**, with the benchmark sweeping
   {5, 10, 15, 20} to find the empirical sweet spot. v1 ships with 10.
2. **No size-based routing or tabular fallback.** One algorithm, one
   code path. If a future query exceeds the LLM's context window after
   reduction, that surfaces as a data point in the benchmark, not as a
   silent degradation in production.
3. **Stage 11 reduction and Stage 15 picked-edges-only land in the same
   PR.** They are one conceptual change (the LLM only sees what's being
   scored at each stage). Splitting them would create an awkward
   intermediate state where Stage 15 saw a larger view than Stage 11.
4. **No backwards-compat fallback in the eval harness.** No prior
   benchmark runs exist; `reduced_data.json` becomes required from
   day 1.
5. **Artifact storage is not a concern.** The benchmark is run a small
   number of times by hand, not on every commit.

## 12. Out of scope for this spec

- Any change to the eval / benchmark harness.
- Any change to the frontend.
- Any change to PubTator handling.
