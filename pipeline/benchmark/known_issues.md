# Known Issues

Failure modes of the grounded pipeline that affect ad-hoc queries but
not the gold benchmark, plus the upstream causes we traced them to.
Each entry has a concrete reproducer.

## 1. Entity resolution fails when a phenotype is conflated into a disease equivalence class

### Symptom

For some questions of the form *"Which diseases present with X?"* —
where X is a clinical sign whose name is also used as a disease name
— the pipeline pins an entity that does not match what the question
is asking about and the answer set is wrong or empty.

The reproducible case we hit is *"Which diseases present with
seizures?"* — gold question q6 uses `HP:0001250` (Seizure phenotype)
and returns ~198 results when run directly against PloverDB; the
ad-hoc pipeline picks `HP:0002373` (Febrile Seizure) or
`HP:0002173` (Hypoglycemic seizures) depending on configuration and
returns 0 results.

### What is actually going on

Three independent services in the pipeline have different ideas
about what the "canonical" representation of the seizure concept is:

| Service | Canonical for the seizure concept | Implied Biolink type |
|---|---|---|
| **PloverDB / KG2c** | `HP:0001250` ("Seizure") | `biolink:PhenotypicFeature` |
| **RENCI Node Normalization** | `MONDO:0005027` ("epilepsy") | `biolink:Disease` |
| **RENCI Name Resolution** | only surfaces canonical IDs of equivalence classes | (inherits NodeNorm's pick) |

The equivalence class that NodeNorm builds for `MONDO:0005027`
contains 28 identifiers, including `HP:0001250`, `UMLS:C0036572`
("Seizures"), `NCIT:C2962` ("Seizure"), `DOID:1826` ("epilepsy"),
and `MESH:D004827` ("Epilepsy"). NodeNorm picks the MONDO Disease
identifier as the canonical representative for the whole class.

NameRes only ever surfaces canonical IDs. When the pipeline filters
NameRes results by `biolink_type=biolink:PhenotypicFeature` (because
the user's question uses "seizures" in a phenotype role), the entire
epilepsy equivalence class is eliminated and the search instead
returns phenotype-only concepts whose canonical type really is
`PhenotypicFeature` — those are independent subtype concepts like
*Febrile seizure*, *Hypoglycemic seizures*, *Bilateral tonic-clonic
seizure*, etc.

The bare `HP:0001250` is therefore unreachable through NameRes for
the query "seizure" or "seizures", at any rank, with or without the
type filter. We verified by pulling the top 200 candidates against
both queries (2026-05-12). PloverDB itself accepts `HP:0001250`
just fine — the gold q6 query proves that — but no entity-resolution
pipeline routed through NameRes will surface it.

### Why the benchmark is not affected

The gold record for q6 stores `HP:0001250` directly in
`pinned_entity.curie`. The benchmark scorer pins that CURIE without
going through the NameRes → NodeNorm pipeline, so the conflation
never matters for scored runs. The `validation.predicate_fix` field
on q6 in `questions.json` already documents that the entity selection
for this question required manual curation:

> `"REPLACED: Original Q6 used HP:0004401 (meconium ileus) which
> normalizes to MONDO:0054868 (Disease type). New Q6 uses HP:0001250
> (seizures)."`

That note is a record of the same class of bug — a phenotype concept
that NodeNorm canonicalizes into the Disease type, hiding the
phenotype representation the question actually needs.

### Mitigations already in the pipeline

The pipeline does six things that help on related but easier cases.
None can surface a CURIE NameRes refuses to return, but together they
unblock everything *adjacent* to this failure mode:

1. **Type-filtered NameRes lookup.** Stage 2 emits `expected_category`,
   which Stage 3 passes to NameRes as `biolink_type=...`. Prevents
   wrong-type matches like resolving "seizures" to a `Disease`-typed
   `MONDO:0007365` (BFNS1, a rare hereditary epilepsy syndrome).
2. **BMT-derived loose-neighborhood filter.** When the strict
   `expected_category` filter would exclude the right answer because
   KG2c types the concept under a sibling class (cholesterol
   biosynthesis = `GO:0006695` typed as `biolink:BiologicalProcess`,
   not `biolink:Pathway`), the pipeline retries NameRes with the
   loose neighborhood: descendants of the picked category plus
   descendants of its parent (when the parent isn't a generic umbrella
   like `BiologicalEntity`). See `biolink_helper.py`.
3. **Strict-first, loose-fallback gate.** The loose filter from #2
   only kicks in when the STRICT pass's top-K candidates all have
   zero KG2c edges to the answer category — measured live by a
   per-candidate probe against PloverDB. This preserves precision
   (HP entries stay top for `expected_category=PhenotypicFeature`
   queries) while recovering recall (cholesterol biosynthesis surfaces
   `GO:0006695` via the fallback). See `pipeline.py::run_grounded`.
4. **Wider NameRes net + local 5-tier rerank.** NameRes is asked for
   `limit=20` (not 5), then the result is re-ranked by
   (exact-label-match, exact-synonym-match, token-match, type-match,
   raw BM25 score). BM25's tendency to bury the canonical short label
   under longer same-token candidates ("Seizure" lost to "Hypoglycemic
   seizures") is corrected before Stage 4 sees the list. See
   `pipeline.py::_rerank_nameres_candidates`.
5. **Per-candidate KG2c edge-density probe (Stage 4 setup).** For
   each of the top-10 reranked candidates, the pipeline asks PloverDB
   how many edges that CURIE has to the answer category, in either
   direction. Stage 4's prompt receives the counts inline so the LLM
   prefers populated CURIEs over perfect-label-but-empty ones (e.g.
   `GO:0006695` with 101 edges over `PANTHER.PATHWAY:P00014` with 0).
   See `plover_client.py::probe_predicates` + `candidate_probes.json`.
6. **IC-based re-rank within the type-filtered pool.** Stage 5
   sorts the type-matching NameRes candidates by `information_content`
   ascending when the question asks for the general concept, so the
   most general available candidate is chosen.

These six together correctly pick the most-general PhenotypicFeature
that NameRes returns, and routinely recover concepts typed under
sibling Biolink classes. They cannot surface a CURIE that NameRes
refuses to return — which is exactly what happens for the seizure
case: `HP:0001250` is hidden inside `MONDO:0005027`'s equivalence
class with the canonical type set to `Disease`, and "seizure" /
"seizures" isn't in `MONDO:0005027`'s synonym list.

### Mitigations considered and not implemented

| Option | Description | Why we did not ship it |
|---|---|---|
| **Equivalents-aware re-pinning** | After NodeNorm canonicalizes the top NameRes hit, scan its `equivalent_identifiers` and re-pin to a CURIE whose prefix matches `expected_category`. | Does not help on the seizure case because none of the NameRes top hits for "seizure" belong to the epilepsy equivalence class. They are all distinct concepts. |
| **Synonym-shortlist lookup** | Have Stage 2 emit a shortlist of name variants (e.g. `["seizure", "epilepsy", "convulsion"]`), try each through NameRes + NodeNorm, prefer the first whose equivalence class contains a CURIE of the expected type. | Adds real complexity (multi-call NameRes, equivalence-class scoring) for marginal coverage gain. The class of cases it would fix is exactly the cases the SRI ecosystem itself does not have a clean answer for. Could be a follow-up. |
| **PloverDB-side entity lookup** | Skip NameRes / NodeNorm and use PloverDB's own indexed CURIEs. | PloverDB does not currently expose a free-text name lookup endpoint; the meta_KG provides categories and prefixes but not labels. |

### Implication for the paper

Entity resolution is the dominant failure mode for ad-hoc queries
about concepts that are *phenotype/disease-conflated* in the SRI's
normalization scheme. Examples in the same family:
seizure ↔ epilepsy, dementia ↔ Alzheimer disease,
convulsion ↔ epilepsy.

The benchmark scores are not affected because the gold pinned CURIEs
bypass the conflation. But honest reporting of the system's
real-world behaviour should describe this class of failure — the
pipeline inherits the upstream services' normalization decisions,
and those decisions favour the Disease representative over the
PhenotypicFeature representative for terms that exist in both
ontologies.

### How to reproduce (2026-05-12)

```bash
# the symptom: pipeline can't surface HP:0001250 from "seizures".
curl -s "https://name-resolution-sri.renci.org/lookup?string=seizure&limit=200" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
    print('HP:0001250 in top 200?', any(c['curie']=='HP:0001250' for c in d))"
# → HP:0001250 in top 200? False

# the cause: HP:0001250 is hidden inside MONDO:0005027's equivalence class.
curl -s "https://nodenormalization-sri.renci.org/get_normalized_nodes?curie=HP:0001250" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
    print('canonical of HP:0001250 →', d['HP:0001250']['id'])"
# → canonical of HP:0001250 → {'identifier': 'MONDO:0005027', 'label': 'epilepsy'}

# proof PloverDB does answer when HP:0001250 is pinned directly:
curl -s -X POST https://kg2cploverdb.ci.transltr.io/query \
  -H 'Content-Type: application/json' \
  -d '{"message":{"query_graph":{
        "nodes":{"n0":{"categories":["biolink:Disease"]},
                 "n1":{"ids":["HP:0001250"],"categories":["biolink:PhenotypicFeature"]}},
        "edges":{"e0":{"subject":"n0","object":"n1","predicates":["biolink:has_phenotype"]}}}}}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
    print('results:', len(d['message'].get('results') or []))"
# → results: 198
```

### References

- Gold record: `benchmark/golden_questions/questions.json` → q6,
  `validation.predicate_fix` documents the original instance of the
  same conflation class.
- SRI Translator stack:
  Name Resolution — https://name-resolution-sri.renci.org/docs
  Node Normalization — https://nodenormalization-sri.renci.org/docs
- Biolink Model `DiseaseOrPhenotypicFeature` — the formal class for
  concepts that straddle the disease/phenotype boundary —
  https://biolink.github.io/biolink-model/DiseaseOrPhenotypicFeature/
