# PloverAI

AI-powered chat interface for PloverDB (RTX-KG2.10.2c).

## Architecture

Two independent services, one repo.

```
[browser]  ─►  Next.js static UI  (frontend/, served by nginx)
                    │
                    │  POST /api/v1/query
                    ▼
                FastAPI service   (pipeline/code/api.py, uvicorn)
                    │
                    ▼
                pipeline.run_grounded()
                    │
                    ├─► PloverDB           (TRAPI query → graph subset)
                    ├─► Name Resolution    (text → candidate CURIEs)
                    ├─► Node Normalization (CURIE → canonical CURIE)
                    └─► OpenRouter         (LLM calls per stage)

[ARAX]     ─────────────────────────────► same /api/v1/query endpoint
```

The pipeline is a standalone HTTP service. The Next.js UI is one
client; any external Translator tool (e.g. ARAX) is another.

## Setup

### Backend (Python service)

```bash
cd pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # then fill in OPENROUTER_API_KEY
```

Run the always-on service locally:

```bash
PLOVERAI_API_KEY=dev-key-change-me \
  uvicorn pipeline.code.api:app --reload --port 8000
```

Or run the gold benchmark via the CLI runner — see
`pipeline/README.md`.

### Frontend (Next.js UI)

```bash
cd frontend
cp .env.local.example .env.local     # adjust if the API runs elsewhere
npm install
npm run dev                          # http://localhost:3000
```

`NEXT_PUBLIC_API_KEY` in `.env.local` must equal `PLOVERAI_API_KEY`
on the Python side.

## Layout

- `pipeline/` — Python backend: pipeline engine, gold question set,
  benchmark runner, FastAPI service, per-run artifacts.
- `frontend/` — Next.js (App Router, TypeScript, Tailwind v4). Built
  as a static export for nginx — no Node runtime in production.

## Production deploy (AWS EC2)

Config templates live under [deploy/](deploy/): nginx vhost,
`ploverai-api` systemd unit, bootstrap + update scripts, and env-file
examples.

Architecture summary: backend runs as a `ploverai-api` systemd unit
bound to `127.0.0.1:8000`. Frontend is a static export rsync'd into
`/var/www/ploverai/out/`. nginx terminates TLS, enforces single
shared-credential basic auth, rate-limits `/api/*`, and reverse-
proxies to the FastAPI service. Same origin in production, no CORS.

## Code rules

This is research code that will end up in an academic paper. Brief
version:

- Python: ruff + vulture + mypy strict, all clean before commit.
- TypeScript: `tsc --noEmit` + ESLint clean, build succeeds.
- Pin every dependency exactly. No docstrings / JSDoc. Lowercase
  comments. Descriptive names. Numbers, not adjectives, in logs.
