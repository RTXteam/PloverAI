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
   - `code.runner` — CLI for batch benchmark runs.
   - `code.api` — FastAPI service for the always-on use
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
python -m code.runner --smoke           # m5 + q1, one shot
python -m code.runner --dry-run         # show the plan, no work
python -m code.runner                   # full benchmark
```

### HTTP service (always-on)

```bash
source .venv/bin/activate
PLOVERAI_API_KEY=dev-key-change-me \
  uvicorn code.api:app --reload --port 8000
```

- `GET /health` — liveness
- `POST /api/v1/query` (headers `X-API-Key`, `X-Guest-Id`) — one NL
  question in, one full pipeline trace out. `X-Guest-Id` is a
  client-minted UUID v4 used to namespace this run on disk so the
  caller's sidebar listing stays isolated from other visitors'.
- `POST /api/v1/query/stream` (same headers as above) — Server-Sent
  Events variant; emits one event per pipeline log line, then a
  terminal `result` or `error` event.
- `GET /api/v1/runs` (headers `X-API-Key`, `X-Guest-Id`) — sidebar
  listing scoped to the caller's `X-Guest-Id`.
- `GET /api/v1/runs/{run_id}` (header `X-API-Key`) — capability
  lookup; any caller with a known `run_id` can re-open the run,
  regardless of which guest namespace created it. This is what makes
  shareable URLs work without sign-in.
- `GET /docs` — auto-generated OpenAPI explorer

`X-Guest-Id` must match the UUID v4 pattern; malformed values are
rejected with 400. On disk, runs land at
`outputs/<guest_id>/RUN_<utc_ts>_<nonce>/<model>/grounded/adhoc/`.

## Before every commit

```bash
.venv/bin/ruff    check code
.venv/bin/vulture code --min-confidence 80 --exclude .venv
.venv/bin/mypy    --strict --explicit-package-bases \
                  --config-file pyproject.toml .
```

All three must pass clean. Don't silence warnings — fix the cause.