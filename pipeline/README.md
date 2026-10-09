# Pipeline

The pipeline turns a natural-language biomedical question into an
answer from ARAX, the reasoner of the NCATS Biomedical Data Translator,
and explains ARAX's reasoning with every claim cited to a fact.
ARAX reasons over the Translator's Tier 0 knowledge graph. The pipeline
reads the same graph from Retriever for its meta knowledge graph and
its entity probes.

For the stage-by-stage walkthrough see [code/README.md](code/README.md).
For the question sets see [benchmark/README.md](benchmark/README.md).

## What this folder gives you

1. **Entity resolution**: extract the focal entity from the question,
   resolve it to a canonical CURIE via RENCI Name Resolution and Node
   Normalization, and check the first candidates against Tier 0 on
   Retriever so the pick has facts in the graph.
2. **TRAPI query construction**: an LLM builds a one-hop TRAPI query
   graph, with predicates taken from Tier 0's meta knowledge graph.
   For treatment and gene-regulation questions a deterministic step
   (`arax_reasoning.ask_arax_to_reason`) then puts the query in the
   form ARAX reasons on: `knowledge_type: inferred`, the chemical as
   subject, and a `biolink:ChemicalEntity` answer node.
   `reasoner-validator` checks the query before it is sent.
3. **ARAX execution**: POST to `https://arax.ncats.io/api/arax/v1.4/query`
   with `stream_progress: true`. ARAX's log lines arrive while it works
   (the UI shows them in plain words), and the last line is the TRAPI
   response: ranked answers, each backed by support graphs of evidence.
   The whole query has a 600 s budget (`arax.timeout_s`) and a 120 s
   per-read timeout.
4. **Answers, reasoning graph and explanation**: the LLM picks answers
   from ARAX's ranked list. The pipeline traces each answer's paths to
   the question's entity, numbers the facts on them F1..Fn, and reads
   what each fact rests on (approvals, FDA applications, drug labels,
   registered trials, PMIDs, PubMed co-mention). The LLM then writes a
   narrative that cites the facts as `[F#]`.
5. **Two entry points**:
   - `pipeline.code.runner`: CLI for batch benchmark runs.
   - `pipeline.code.api`: FastAPI service for the always-on use
     case (the Next.js UI, or anything else hitting `/api/v1/query`).

## Key constraints

- The query graph stays one hop (two nodes, one edge). ARAX does the
  multi-hop reasoning behind it.
- All entity resolution goes through Name Resolution → Node
  Normalization. No shortcuts.
- Every external call is logged: URL or model, parameters, response
  size, token counts, USD cost, latency.
- Every run writes to a fresh ISO-8601 UTC artifact folder under
  `code/outputs/`. Nothing overwrites prior runs.

## Run

### CLI runner (batch benchmark)

Run from the repository root (the code imports as `pipeline.code.*`):

```bash
source pipeline/.venv/bin/activate
python -m pipeline.code.runner --model m8 --question "What drugs treat rheumatoid arthritis?"
python -m pipeline.code.runner --smoke             # pilot model + q1, one shot
python -m pipeline.code.runner --dry-run           # show the plan, no work
python -m pipeline.code.runner                     # full benchmark, ARAX
python -m pipeline.code.runner --reasoner lookup   # one-hop Tier 0 comparison
```

`--reasoner arax` (the default) sends Stage 10 to ARAX. `--reasoner
lookup` sends the same query to Retriever as a one-hop Tier 0 lookup
and leaves the reasoning to the LLM. Each writes to its own condition
folder (`arax/`, `lookup/`).

### HTTP service (always-on)

```bash
source pipeline/.venv/bin/activate      # from the repository root
PLOVERAI_API_KEY=dev-key-change-me \
  uvicorn pipeline.code.api:app --port 8000
```

- `GET /health`: liveness
- `POST /api/v1/query` and `POST /api/v1/query/stream` (header
  `X-API-Key`): body `{question, model}`, optional `reasoner`
  (`"arax"` default, or `"lookup"`). One question in, one full
  pipeline trace out. The stream variant sends server-sent events
  while the run is in progress; each stream carries only its own
  question's log lines, also when several questions run at once.
- `GET /api/v1/info`: ARAX version, Tier 0 graph (Retriever version),
  Biolink and TRAPI versions (as ARAX and Retriever report them in
  their `/openapi.json` at start-up), the service endpoints, and
  `public`
- `GET /api/v1/models`: the models a question may use
- `GET /api/v1/questions`: the web UI's 20 example questions
  (`examples.yaml`; not the benchmark's gold set)
- `GET /api/v1/runs` and `GET /api/v1/runs/{run_id}`: the run history,
  newest first, and one run in full
- `GET /docs`: auto-generated OpenAPI explorer

**Public site mode.** With `PLOVERAI_PUBLIC=1` in `pipeline/.env` the
service serves only the models in the `public:` block of
`config.yaml` (m8), only ARAX, and limits questions per address and per
day (`code/public_guard.py`). Refusals are 403, 429 or 503 with
`Retry-After`. The run history stays shared: `/api/v1/runs` lists
every run on the server, whoever asked it. Off by default, so a laptop
run is unchanged. The launch configuration `api-public`
starts it locally.

## Environment

`pipeline/.venv` on Python 3.12.11, built from `requirements.txt`
(direct dependencies pinned with `==`, including the dev tools and
pytest). From the repository root:

```bash
python3.12 -m venv pipeline/.venv
pipeline/.venv/bin/pip install -r pipeline/requirements.txt
```

## Before every commit

From the repository root:

```bash
pipeline/.venv/bin/ruff    check pipeline/code
pipeline/.venv/bin/vulture pipeline/code --min-confidence 80 --exclude .venv
pipeline/.venv/bin/mypy    --strict --explicit-package-bases \
                           --config-file pipeline/pyproject.toml pipeline
pipeline/.venv/bin/python -m pytest pipeline/code/tests -q
```

All four must pass clean. Don't silence warnings. Fix the cause. The
tests make no LLM calls and need no network.
