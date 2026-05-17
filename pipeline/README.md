# Pipeline

The pipeline converts a natural language biomedical question into a
faithful, evidence-grounded answer using PloverDB and RTX-KG2.10.2c.

## What this folder gives you

1. **Entity resolution**: extract biomedical entity mentions from NL,
   resolve to canonical CURIEs via RENCI Name Resolution and Node
   Normalization.
2. **TRAPI query construction**: build a valid one-hop TRAPI query
   graph; validate with `reasoner-validator` before sending.
3. **PloverDB execution**: POST to `kg2cploverdb.ci.transltr.io`,
   handle pagination, errors, timeouts.
4. **Answer selection and TRAPI-to-NL explanation**: the LLM picks
   answer entities from the response (ranked by Biolink evidence
   tier) and writes a paragraph citing the supporting publications.
5. **Two entry points**:
   - `pipeline.code.runner` — CLI for batch benchmark runs.
   - `pipeline.code.api` — FastAPI service for the always-on use
     case (Next.js UI, ARAX, anything else hitting `/api/v1/query`).

## Key constraints

- PloverDB accepts ONE-HOP queries only (two nodes, one edge).
- All entity resolution goes through Name Resolution → Node
  Normalization. No shortcuts.
- Every external call is logged: URL or model, parameters, response
  size, token counts, USD cost, latency.
- Every run writes to a fresh ISO-8601 UTC artifact folder under
  `code/outputs/`. Nothing overwrites prior runs.

## Run

### CLI runner (batch benchmark)

```bash
source .venv/bin/activate
python -m pipeline.code.runner --smoke           # m5 + q1, one shot
python -m pipeline.code.runner --dry-run         # show the plan, no work
python -m pipeline.code.runner                   # full benchmark
```

### HTTP service (always-on)

```bash
source .venv/bin/activate
PLOVERAI_API_KEY=dev-key-change-me \
  uvicorn pipeline.code.api:app --reload --port 8000
```

- `GET /health` — liveness
- `POST /api/v1/query` (header `X-API-Key`) — one NL question in,
  one full pipeline trace out
- `GET /docs` — auto-generated OpenAPI explorer

## Before every commit

```bash
.venv/bin/ruff    check code
.venv/bin/vulture code --min-confidence 80 --exclude .venv
.venv/bin/mypy    --strict --explicit-package-bases \
                  --config-file pyproject.toml .
```

All three must pass clean. Don't silence warnings — fix the cause.