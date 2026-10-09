# Frontend

Next.js 16 (App Router, React 19, TypeScript, Tailwind v4) research
workbench for PloverAI: full width, no centred column. It calls the
Python service at `pipeline/code/api.py` over HTTP and never talks to
ARAX, Retriever, NameRes, NodeNorm or OpenRouter directly. Every
question goes to ARAX. There is no reasoner toggle in the UI.

## What it does

A top bar names the versions the service runs on (ARAX, Tier 0,
Biolink, TRAPI), holds a System panel with the endpoints, and switches
between two views.

1. **Ask.** From top to bottom:
   - the question bar: the question, 20 example questions (plain
     natural language, from `pipeline/examples.yaml`), the model, Run;
     the PloverAI name and icon go back to the start page;
   - the query band ("Interpreted as"): the one-hop query the pipeline
     built, drawn as two nodes and an edge (`any Drug — treats → type 2
     diabetes mellitus MONDO:0005148 · Disease`), so a wrong entity,
     predicate or direction is visible at once. When code rewrote the
     query for ARAX (`ask_arax_to_reason`), the sent form is shown under
     the edge. On the right: the run's result, model, time, cost, run id
     and the PDF / Markdown / JSON exports, or while a query runs, ARAX's
     own progress line (`arax_client.progress_words`) and a timer;
   - the trace: the pipeline as eleven steps (Scope, Entity, Lookup,
     Pick, Normalize, Query, Validate, ARAX, Answers, Evidence, Explain),
     each with what it produced and how long it took
     (`lib/pipelineTrace.ts`). While a query runs it is read from the
     streamed log lines, so it shows the step running now. Hover a step
     for tokens, cost and details; click it to open its prompt and
     artifacts in the inspector;
   - three panes:
     - **Answers**: ARAX's ranked answers (top 30 of all it returned)
       with rank, CURIE, score and number of facts, the ones the LLM
       picked marked. Sortable; hovering a row lights that answer's
       paths in the graph, choosing one isolates them. Hovering the graph
       marks the rows of the answers under the pointer, so the graph
       prints no ranks of its own.
     - **Reasoning graph** (Cytoscape.js). Two layouts: *layered*, the
       default, is deterministic and left to right: answers in ARAX
       rank order on the left, the question's entity on the right, the
       entities between in columns by distance
       (`lib/layeredLayout.ts`), with a heading over each column. Links
       are smooth curves routed between the columns' entities, ordered for
       few crossings; links making the same statement about the same
       entity merge into one line with one arrow and one label. Nodes move
       only up and down their column there. *Force* is fcose. Every node
       is a circle; its colour and the icon inside (`lib/nodeIcons.ts`)
       give the kind of entity. Arrows carry the predicate, link width
       is the number of facts, line style the strongest evidence. A
       predicate label runs along its own line, like a road name on a
       map, with a halo of the page colour, and shows only where it fits
       clear of nodes and other labels (text-mined links show theirs on
       hover). Hovering lights a path in dark grey and lays out the labels
       of the lit paths alone. Lines wear a casing of the page colour, so
       crossings stay apart. The toolbar holds a layout switch (layered /
       force), a labels switch, zoom, re-layout, and SVG / PNG export;
       legend and caption below. While a query runs, the graph grows in place: the
       question's entity, a dashed "ghost" for what ARAX is asked, the
       picked answers, then the paths.
     - **Inspector**, in tabs: the explanation (Summary / Why each
       answer / How strong is the evidence, every [F#] a chip whose card
       shows the fact's path and evidence; a placeholder while the run is
       going), the evidence list (hover a fact to light its link; hover
       the graph to mark its facts), the
       selected node or link, the query (the LLM's TRAPI, the sent
       TRAPI, the validator's verdict), all 15 pipeline stages with
       prompts and artifacts, the raw artifacts, and the live log.
       While a question runs the inspector shows the log, following new
       lines unless the reader has scrolled up; when the answer is in,
       it opens the explanation (or the pipeline, if the run ended
       without one). In the step strip the running step is underlined.
2. **Runs.** Every run on disk as a table: started, question, model,
   result (answered, no answer, refused, failed), time, cost. Filters
   by text, model and result, a running total of cost, and older pages
   on demand. Opening a row shows the run in Ask. Any run is reachable
   at `/?run=<run_id>`. The history is shared: every visitor sees
   every run on the server, also on the public site
   (`PLOVERAI_PUBLIC=1` on the service), where the model picker shows
   only the public models. The list is fetched again each time the tab
   opens.
3. **Exports**: JSON (full envelope + QueryResponse), Markdown and PDF.
   Markdown and PDF include the reasoning figure and a table of facts
   with their evidence links, plus the pipeline overview, per-stage
   prompts and artifacts, and the cost ledger.

## Local development

Two processes, one terminal each.

```bash
# terminal 1: Python service, from the repository root
# (the code imports as pipeline.code.*). the key must match the UI's
export PLOVERAI_API_KEY="$(sed -n 's/^NEXT_PUBLIC_API_KEY=//p' frontend/.env.local)"
pipeline/.venv/bin/uvicorn pipeline.code.api:app --reload --port 8000

# terminal 2: Next.js dev server
cd frontend
cp .env.local.example .env.local        # first time only; adjust if your API runs elsewhere
npm install
npm run dev
```

Open `http://localhost:3000` once the service logs `meta_KG cached`.
Ask a question. `NEXT_PUBLIC_API_KEY` in `.env.local` must equal the
service's `PLOVERAI_API_KEY`. The export line above copies it.
An ARAX question usually takes tens of seconds, because ARAX reasons
before it answers.

## Production build

The app is configured for **static export** (`output: "export"` in
`next.config.ts`). Production deployment is just a folder of HTML/CSS/JS
served by nginx, with no Node runtime on the EC2.

```bash
npm run build      # emits ./out/ ready to rsync to the server
```

On the EC2, nginx serves `out/` at `/` and proxies `/api/*` to the
local FastAPI service. Same origin, no CORS to configure.

## Configuration

- `NEXT_PUBLIC_API_BASE`: base URL of the Python service. Empty
  string in production (same-origin), `http://localhost:8000` in dev.
- `NEXT_PUBLIC_API_KEY`: baked into the JS bundle, so every visitor
  can read it. In dev it must equal the service's `PLOVERAI_API_KEY`.
  In production leave it empty: nginx adds the key itself (see
  `deploy/README.md`).

When the service refuses a question (a public-site limit, a model that
is not offered), the UI shows its reason and, from `Retry-After`, when
to try again.

## Layout

```
frontend/
├── src/
│   ├── app/                          next.js app router
│   │   ├── layout.tsx                root layout, global font, dark mode, favicon icons
│   │   ├── page.tsx                  renders <Workbench />
│   │   └── globals.css               tailwind v4 entry, --viz-* figure tokens
│   ├── components/
│   │   ├── Workbench.tsx             the app: top bar, Ask / Runs views, data and the
│   │   │                             query stream; ?run=<id> deep links
│   │   ├── AskView.tsx               question bar, query band, trace, the three panes
│   │   ├── AnswersTable.tsx          ARAX's ranked answers, picked ones marked
│   │   ├── TraceStrip.tsx            the eleven pipeline steps with hover cards
│   │   ├── ReasoningGraph.tsx        Cytoscape.js figure, layered or force layout,
│   │   │                             SVG / PNG export, selection details
│   │   ├── FigureLegend.tsx          legend and caption of the figure
│   │   ├── Inspector.tsx             right pane: explanation, evidence, selection,
│   │   │                             query, pipeline, raw, log
│   │   ├── FactEvidence.tsx          citation chips, hover cards, evidence list
│   │   ├── MarkdownAnswer.tsx        explanation through react-markdown; [F#], PMID
│   │   │                             and CURIE citations become links
│   │   ├── PipelineStages.tsx        the 15 stages with prompts and artifacts, raw artifacts
│   │   ├── RunsView.tsx              the runs table with filters
│   │   ├── JsonView.tsx              line-numbered JSON code block
│   │   ├── ModelDropdown.tsx         model picker
│   │   ├── QuestionsDropdown.tsx     example-question picker
│   │   ├── SelectMenu.tsx            small select for toolbars (sort, filters)
│   │   └── ThemeSwitch.tsx           light / dark / system
│   └── lib/
│       ├── api.ts                    typed client for /api/v1/*
│       ├── liveState.ts              stream events → steps, live graph, ghost answer
│       ├── queryGraph.ts             reads a one-hop TRAPI query graph for display
│       ├── layeredLayout.ts          deterministic left-to-right positions, link routes
│       ├── nodeIcons.ts              one icon per kind of entity
│       ├── pipelineTrace.ts          the eleven steps, from a run or from the log stream
│       ├── factText.ts               how a fact reads: statement, source, evidence, chip tag
│       ├── reasoningLinks.ts         reasoning graph → one link per pair of nodes
│       ├── vizPalette.ts             figure colours and encodings
│       ├── figureExport.ts           stand-alone SVG / PNG of the reasoning figure
│       ├── format.ts                 dates, money, seconds, run result kinds
│       ├── linkify.ts                shared [F#] + PMID + CURIE linkifiers
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
`// @ts-expect-error`, or `// eslint-disable` to silence a warning.
Fix the cause, or write a one-line comment explaining why the
suppression is necessary.

## Code style (brief)

- No JSDoc preambles. Clear names + inline comments only when the
  *why* is non-obvious.
- Comments mostly lowercase, with uppercase where it earns it
  (proper nouns, acronyms, sentence starts in multi-line blocks).
- Strict TypeScript. No `any`. Use `unknown` plus a type guard
  for free-form JSON.
- Don't over-componentize. Extract a wrapper component when the
  same pattern shows up three times, not before.
