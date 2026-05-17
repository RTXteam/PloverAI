// thin client for the PloverAI Python service. one function per
// endpoint we actually call from the UI. all configuration comes from
// NEXT_PUBLIC_* env vars so the same bundle runs against localhost in
// dev and the EC2 host in production.

// guest identity: per-browser UUID stored in localStorage. sent on
// every request as X-Guest-Id so the server can namespace the
// sidebar. see ./guest.ts for the trade-offs.
import { getGuestId } from "./guest";

// types mirror the FastAPI response models. keep them narrow — we
// only declare fields the UI actually reads. anything else stays in
// an `unknown` blob.

export type QueryRequest = {
  question: string;
  model: string;
};

export type PloverResponseSummary = {
  n_results: number;
  n_nodes: number;
  n_edges: number;
};

export type StagePromptEntry = {
  system?: string;
  user?: string;
  user_truncated?: string;
  response?: {
    reasoning?: string | null;
    finish_reason?: string | null;
    refusal?: string | null;
    model_returned?: string | null;
    input_tokens?: number;
    output_tokens?: number;
    latency_s?: number;
  };
};

export type QueryIntermediates = {
  trapi_query?: unknown;
  validation?: unknown;
  plover_request?: unknown;
  plover_response_summary?: PloverResponseSummary | null;
  nameres?: unknown;
  nodenorm?: unknown;
  // Stage 4 setup: per-candidate edge-density probe. one entry per
  // NameRes top-K candidate, recording how many edges that CURIE has
  // in KG2c to the answer category. injected into Stage 4's prompt so
  // the LLM picks CURIEs with non-zero coverage instead of perfect-
  // label-but-empty ones.
  candidate_probes?: unknown;
  // Stage 8 setup: per-CURIE predicate-density probe for the CHOSEN
  // candidate (the one Stage 4 picked). same data as the candidate_probes
  // entry keyed by chosen_curie — duplicated so the existing Stage 8
  // UI row keeps working without changes.
  predicate_probe?: unknown;
  cost?: unknown;
  prompts?: Record<string, StagePromptEntry> | null;
};

// Stage 13 answer_graph_view shape — mirrors the Python emitter in
// pipeline.py::_build_answer_graph_view + _enrich_edges_with_pubtator.
// kept narrow: only the fields the GraphView component renders.
export type AnswerGraphNode = {
  curie: string;
  label: string | null;
  category: string | null;
  role: "pinned" | "answer";
};

export type SupportingTextSnippet = {
  pmid: string;
  date: string | null;
  sentence: string | null;
};

export type PubTatorVerified = {
  co_mention_pmids: string[];
  subject_only_pmids: string[];
  object_only_pmids: string[];
  missing_pmids: string[];
  co_mention_rate: number;
  verified: boolean;
};

export type AnswerGraphEdge = {
  id: string;
  source: string;
  target: string;
  predicate: string | null;
  knowledge_level: string | null;
  primary_knowledge_source: string | null;
  supporting_publications: string[];
  supporting_text_snippets: SupportingTextSnippet[];
  pubtator_verified: PubTatorVerified | null;
};

export type AnswerGraphView = {
  pinned_node: AnswerGraphNode;
  answer_nodes: AnswerGraphNode[];
  edges: AnswerGraphEdge[];
  pubtator_metrics?: {
    verified: number;
    unverified: number;
    not_applicable: number;
    total_edges: number;
    rate: number | null;
  };
  pubtator_call_summary?: {
    called: boolean;
    reason?: string;
    pmids_requested?: number;
    pmids_annotated?: number;
    pmids_missing?: number;
    latency_s?: number;
    error?: string;
  };
};

export type QueryResponse = {
  run_id: string;
  success: boolean;
  outcome: string | null;
  cost_usd: number;
  elapsed_s: number;
  answer: Record<string, unknown> | null;
  answer_graph_view: AnswerGraphView | null;
  explanation: string | null;
  intermediates: QueryIntermediates;
};

export type ModelInfo = {
  id: string;
  slug: string;
  provider: string;
  tier: "frontier" | "budget" | string;
  price_in: number;
  price_out: number;
};

export type ServiceInfo = {
  service: string;
  version: string;
  started_utc: string;
  endpoints: Record<string, string>;
  kg_version: string;
  biolink_version: string;
  trapi_version: string;
};

export type GoldQuestion = {
  id: string;
  nl_question: string;
  answer_category: string;
  pinned_entity_label: string;
};

export type RunSummary = {
  run_id: string;
  started_utc: string;
  model_id: string;
  model_slug: string;
  question: string;
  status: string;
  outcome: string | null;
  cost_usd: number;
  elapsed_s: number;
};

// SSE event shapes from POST /api/v1/query/stream. one of these per
// `data:` line in the wire stream.
export type StreamEvent =
  | { type: "log"; level: string; msg: string; t: number }
  | { type: "result"; data: QueryResponse }
  | { type: "error"; message: string };

// base URL of the FastAPI service. in production both UI and API are
// behind the same nginx vhost, so this can be the empty string and
// the browser will hit the same origin — set NEXT_PUBLIC_API_BASE=""
// in that case. in dev the API runs on a different port.
const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000").replace(/\/+$/, "");

// public API key, scoped to the UI's expected calls. this is "public"
// in the sense that any user of the deployed UI ends up seeing it in
// devtools — the real protection at the API edge is nginx rate
// limiting and an OpenRouter spend cap, not secrecy of this token.
const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  return { "X-API-Key": API_KEY, "X-Guest-Id": getGuestId(), ...(extra ?? {}) };
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "GET",
    headers: authHeaders(),
    signal,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`api ${res.status}: ${detail || res.statusText}`);
  }
  return (await res.json()) as T;
}

export async function getModels(signal?: AbortSignal): Promise<ModelInfo[]> {
  const body = await getJson<{ models: ModelInfo[] }>("/api/v1/models", signal);
  return body.models;
}

export async function getInfo(signal?: AbortSignal): Promise<ServiceInfo> {
  return getJson<ServiceInfo>("/api/v1/info", signal);
}

export async function getQuestions(signal?: AbortSignal): Promise<GoldQuestion[]> {
  const body = await getJson<{ questions: GoldQuestion[] }>("/api/v1/questions", signal);
  return body.questions;
}

export async function getRuns(
  limit = 50,
  offset = 0,
  signal?: AbortSignal,
): Promise<RunSummary[]> {
  // offset enables the sidebar's infinite scroll: bootstrap uses
  // offset=0, each successive "load more" trigger uses offset = the
  // already-loaded count. backend pagination is positional (no cursor)
  // which is fine because runs only append, never reshuffle.
  const body = await getJson<{ runs: RunSummary[] }>(
    `/api/v1/runs?limit=${limit}&offset=${offset}`,
    signal,
  );
  return body.runs;
}

export async function getRun(runId: string, signal?: AbortSignal): Promise<QueryResponse> {
  return getJson<QueryResponse>(`/api/v1/runs/${encodeURIComponent(runId)}`, signal);
}

// non-streaming variant — kept for ARAX and for the OpenAPI explorer.
// the UI itself uses streamQuery so the user can watch progress.
export async function postQuery(req: QueryRequest, signal?: AbortSignal): Promise<QueryResponse> {
  const res = await fetch(`${API_BASE}/api/v1/query`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(req),
    signal,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`api ${res.status}: ${detail || res.statusText}`);
  }
  return (await res.json()) as QueryResponse;
}

// streaming variant. invokes `onEvent` for every SSE event the server
// emits while the pipeline runs. resolves with the final QueryResponse
// (also passed via the last 'result' event) or rejects on 'error'.
export async function streamQuery(
  req: QueryRequest,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<QueryResponse> {
  const res = await fetch(`${API_BASE}/api/v1/query/stream`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json", Accept: "text/event-stream" }),
    body: JSON.stringify(req),
    signal,
  });
  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => "");
    throw new Error(`api ${res.status}: ${detail || res.statusText}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult: QueryResponse | null = null;
  let streamError: string | null = null;

  // SSE framing: messages are separated by a blank line. each message
  // is a series of `field: value\n` lines; we only emit `data:` lines
  // so we only have to look for those.
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sepIndex: number;
    while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
      const rawEvent = buffer.slice(0, sepIndex);
      buffer = buffer.slice(sepIndex + 2);
      const dataLine = rawEvent
        .split("\n")
        .find((line) => line.startsWith("data:"));
      if (!dataLine) continue;
      const json = dataLine.slice("data:".length).trim();
      if (!json) continue;

      const event = JSON.parse(json) as StreamEvent;
      onEvent(event);
      if (event.type === "result") finalResult = event.data;
      if (event.type === "error") streamError = event.message;
    }
  }

  if (streamError) throw new Error(streamError);
  if (!finalResult) throw new Error("stream ended without a result event");
  return finalResult;
}
