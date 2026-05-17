# Frontend

Next.js (App Router, TypeScript, Tailwind) chat UI for PloverAI.
Calls the Python service at `pipeline/code/api.py` over HTTP — does
not talk to PloverDB, NameRes, NodeNorm, or OpenRouter directly.

## What it does

1. **Default mode**: user types an NL biomedical question →
   `POST /api/v1/query/stream` (SSE) → the page renders the graph-
   grounded answer (structured `Answer / Evidence / Confidence /
   Limitations` block with one mini-graph per picked answer entity),
   plus a 15-row Pipeline-stages card with kind chips (LLM / service
   / function), candidate-density probe + predicate-density probe
   summaries, the LLM-built TRAPI query, and a Raw-artifacts panel
   for everything else (NameRes, NodeNorm, PloverDB request/response,
   PubTator verification, cost ledger).
2. **Run permalinks**: any past run is reachable at `/?run=<run_id>`.
   The sidebar pre-loads 50 runs at bootstrap, infinite-scrolls older
   pages on demand, and history.replaceState's the URL on every
   selection so the address bar is always shareable.
3. **Exports**: every result can be saved as JSON (full envelope +
   QueryResponse), Markdown (thesis-grade transcript: linkified prose,
   answer-graph table + per-edge provenance, 15-row pipeline overview,
   per-stage detail blocks with system+user prompts and intermediate
   JSON, density-probe tables, cost ledger), or PDF (opens a self-
   contained printable HTML doc with rendered markdown + the live
   mini-graph SVGs captured from the page).
4. **TRAPI mode** *(planned)*: paste a TRAPI query graph JSON →
   same response shape, raw response highlighted.

## Local development

Two processes, one terminal each.

```bash
# terminal 1 — Python service
cd ../pipeline
source .venv/bin/activate
uvicorn pipeline.code.api:app --reload --port 8000

# terminal 2 — Next.js dev server
cd frontend
cp .env.local.example .env.local        # adjust if your API runs elsewhere
npm install
npm run dev
```

Open `http://localhost:3000`. Ask a question. The dev API key in
`.env.local.example` must match `PLOVERAI_API_KEY` in `pipeline/.env`.

## Production build

The app is configured for **static export** (`output: "export"` in
`next.config.ts`). Production deployment is just a folder of HTML/CSS/JS
served by nginx — no Node runtime on the EC2.

```bash
npm run build      # emits ./out/ ready to rsync to the server
```

On the EC2, nginx serves `out/` at `/` and proxies `/api/*` to the
local FastAPI service. Same origin, no CORS to configure.

## Configuration

- `NEXT_PUBLIC_API_BASE` — base URL of the Python service. Empty
  string in production (same-origin); `http://localhost:8000` in dev.
- `NEXT_PUBLIC_API_KEY` — must equal the server-side
  `PLOVERAI_API_KEY`. Public by nature (visible in devtools) — the
  real safety net is server-side rate limiting and an OpenRouter
  spend cap.

## Layout

```
frontend/
├── src/
│   ├── app/                          next.js app router
│   │   ├── layout.tsx                root layout, global font, dark mode, favicon icons
│   │   ├── page.tsx                  3-line wrapper renders <ChatShell />
│   │   └── globals.css               tailwind v4 entry
│   ├── components/
│   │   ├── ChatShell.tsx             owns shell state; reads ?run=<id> on mount,
│   │   │                             syncs URL on selection via history.replaceState
│   │   ├── Sidebar.tsx               run-history list with IntersectionObserver-driven
│   │   │                             infinite scroll + load-more spinner
│   │   ├── ResultPanel.tsx           hero meta + structured answer + 15-row pipeline-
│   │   │                             stages card + raw-artifacts panel
│   │   ├── StructuredAnswer.tsx      per-answer evidence cards with mini-graph SVGs
│   │   ├── MarkdownAnswer.tsx        explanation rendered through linkifyCitations +
│   │   │                             linkifyCURIEs + react-markdown
│   │   ├── GraphView.tsx             full-size star-layout answer graph
│   │   ├── JsonView.tsx              line-numbered JSON code block
│   │   ├── ModelDropdown.tsx         model picker
│   │   ├── QuestionsDropdown.tsx     gold-question picker
│   │   ├── ThemeSwitch.tsx           light/dark/system toggle
│   │   └── TrapiCard.tsx             collapsible TRAPI query block
│   └── lib/
│       ├── api.ts                    typed client for /api/v1/*
│       ├── linkify.ts                shared PMID + CURIE linkifiers
│       ├── export.ts                 JSON / Markdown / PDF exporters with marked
│       └── theme.ts                  FOUC-less theme bootstrap helper
├── next.config.ts                    static export config
├── package.json
└── tsconfig.json
```

## Before every commit

```bash
npx tsc --noEmit        # strict type-check, no emit
npm run lint            # ESLint + Next.js rules
npm run build           # confirm the static export still produces out/
```

All three must pass clean. Don't reach for `// @ts-ignore`,
`// @ts-expect-error`, or `// eslint-disable` to silence a warning —
fix the cause, or write a one-line comment explaining why the
suppression is necessary.

## Code style (brief)

- No JSDoc preambles. Clear names + inline comments only when the
  *why* is non-obvious.
- Comments mostly lowercase, with uppercase where it earns it
  (proper nouns, acronyms, sentence starts in multi-line blocks).
- Strict TypeScript. No `any` — use `unknown` plus a type guard
  for free-form JSON.
- Don't over-componentize. Extract a wrapper component when the
  same pattern shows up three times, not before.
