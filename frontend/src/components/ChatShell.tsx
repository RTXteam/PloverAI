"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  getInfo,
  getModels,
  getQuestions,
  getRun,
  getRuns,
  streamQuery,
  type GoldQuestion,
  type ModelInfo,
  type QueryResponse,
  type RunSummary,
  type ServiceInfo,
  type StreamEvent,
} from "@/lib/api";
import { ModelDropdown } from "@/components/ModelDropdown";
import { QuestionsDropdown } from "@/components/QuestionsDropdown";
import { Sidebar } from "@/components/Sidebar";
import { ResultPanel } from "@/components/ResultPanel";

type LogLine = { level: string; msg: string; t: number };

// permalink format: `/?run=<id>` (or just `/` when no run is selected).
// we use a query param rather than a dynamic /run/[id] path so the app
// stays compatible with `output: "export"` static export (which can't
// pre-render dynamic-segment routes whose values exist only at runtime).
// share-friendly + no Node runtime needed at deploy.
function readRunIdFromUrl(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("run");
}

// pure helper: pushes the current "viewing" state into the URL bar so
// a copy-paste of the URL re-opens the same view. uses history.replaceState
// rather than router.push to avoid re-mounting the page on every sidebar
// click — the URL update is purely cosmetic / share-friendly.
function syncRunUrl(runId: string | null) {
  if (typeof window === "undefined") return;
  const target = runId
    ? `${window.location.pathname}?run=${encodeURIComponent(runId)}`
    : window.location.pathname;
  const current = window.location.pathname + window.location.search;
  if (current !== target) {
    window.history.replaceState(null, "", target);
  }
}

export default function ChatShell() {
  const [info, setInfo] = useState<ServiceInfo | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [goldQuestions, setGoldQuestions] = useState<GoldQuestion[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [modelId, setModelId] = useState<string>("");
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [result, setResult] = useState<QueryResponse | null>(null);
  // selectedRunId is set from the URL (?run=<id>) on first mount via
  // the bootstrap effect — see readRunIdFromUrl below. on sidebar
  // clicks and after new-query submissions we update both this state
  // AND the URL via syncRunUrl so the address bar reflects what's on
  // screen and the URL stays shareable.
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  // RUNS_PAGE_SIZE controls both the initial bootstrap fetch and each
  // successive infinite-scroll "load more" page. 50 is the sweet spot:
  // big enough that the average user never hits the sentinel during a
  // lab demo, small enough that a sidebar refresh on a slow connection
  // doesn't stall the boot-up sequence.
  const RUNS_PAGE_SIZE = 50;
  const [runsHasMore, setRunsHasMore] = useState(true);
  const [runsLoadingMore, setRunsLoadingMore] = useState(false);

  const refreshRuns = useCallback(async () => {
    try {
      // refresh = re-fetch the first page and reset the pagination
      // state. existing scroll position is preserved in the sidebar
      // since we replace `runs` wholesale; the user keeps seeing the
      // newest entries at the top.
      const rs = await getRuns(RUNS_PAGE_SIZE, 0);
      setRuns(rs);
      setRunsHasMore(rs.length === RUNS_PAGE_SIZE);
    } catch {
      // sidebar refresh failures are non-fatal — the main flow still works.
    }
  }, []);

  const loadMoreRuns = useCallback(async () => {
    if (runsLoadingMore || !runsHasMore) return;
    setRunsLoadingMore(true);
    try {
      // we use the CURRENT length as the offset rather than tracking
      // a separate page counter — robust to refreshRuns resetting the
      // list mid-scroll. on the off chance a new run lands BETWEEN
      // pages, the user sees it at the top after the next refresh,
      // not a duplicate in the middle of the list.
      const next = await getRuns(RUNS_PAGE_SIZE, runs.length);
      if (next.length === 0) {
        setRunsHasMore(false);
      } else {
        setRuns((prev) => {
          // de-duplicate defensively in case a refresh races with a
          // load-more — keep first occurrence (newest), drop later.
          const seen = new Set(prev.map((r) => r.run_id));
          const fresh = next.filter((r) => !seen.has(r.run_id));
          return [...prev, ...fresh];
        });
        setRunsHasMore(next.length === RUNS_PAGE_SIZE);
      }
    } catch {
      // a failed load-more should not break the rest of the UI;
      // the user can retry by scrolling back up and down again.
    } finally {
      setRunsLoadingMore(false);
    }
  }, [runs.length, runsLoadingMore, runsHasMore]);

  // initial bootstrap: info + models + runs in parallel. wrapped in
  // an async IIFE so all the setState calls run outside the effect's
  // synchronous body — react-hooks/set-state-in-effect would otherwise
  // flag them.
  //
  // when ?run=<id> is in the URL we ALSO fire off the per-run getRun()
  // in the same parallel batch so the result panel populates as fast
  // as the rest of the shell. this is the deep-link path: a labmate
  // pastes /?run=<id> and lands directly on the result for that run.
  useEffect(() => {
    const abort = new AbortController();
    void (async () => {
      // read once at mount; URL changes after this point flow back
      // through state via syncRunUrl + the sidebar handlers, not via
      // re-running this effect.
      const initialRunId = readRunIdFromUrl();
      if (initialRunId) setSelectedRunId(initialRunId);
      const baseCalls = [
        getInfo(abort.signal),
        getModels(abort.signal),
        getRuns(RUNS_PAGE_SIZE, 0, abort.signal),
        getQuestions(abort.signal),
      ];
      // append the deep-link load when present; we destructure positionally
      // below so the order here matters.
      const calls = initialRunId
        ? [...baseCalls, getRun(initialRunId, abort.signal)]
        : baseCalls;
      const settled = await Promise.allSettled(calls);
      if (abort.signal.aborted) return;
      const [infoRes, modelsRes, runsRes, questionsRes, deepRes] = settled;

      if (infoRes.status === "fulfilled") setInfo(infoRes.value as ServiceInfo);
      if (modelsRes.status === "fulfilled") {
        const ms = modelsRes.value as ModelInfo[];
        setModels(ms);
        if (ms.length > 0) {
          // prefer the backend-recommended model; fall back to the cheapest.
          const cheapest = [...ms].sort(
            (a, b) => (a.price_in + a.price_out) - (b.price_in + b.price_out),
          )[0];
          const defaultModel = ms.find((m) => m.recommended) ?? cheapest;
          setModelId(defaultModel.id);
        }
      }
      const runsList =
        runsRes.status === "fulfilled" ? (runsRes.value as RunSummary[]) : [];
      if (runsRes.status === "fulfilled") {
        setRuns(runsList);
        // a full page back means there are likely more on disk —
        // wait for the infinite-scroll sentinel to fire load-more.
        // a short page means we've already fetched everything.
        setRunsHasMore(runsList.length === RUNS_PAGE_SIZE);
      }
      if (questionsRes.status === "fulfilled") {
        setGoldQuestions(questionsRes.value as GoldQuestion[]);
      }
      // deep-link case: the URL was /run/<id> on first paint, so the
      // bootstrap fetched that run alongside everything else. when it
      // resolves we mirror what onSelectRun would have done — populate
      // the result panel + question textarea (the latter from the runs
      // summary list we just fetched alongside).
      if (initialRunId && deepRes && deepRes.status === "fulfilled") {
        const r = deepRes.value as QueryResponse;
        setResult(r);
        const matching = runsList.find((x) => x.run_id === initialRunId);
        if (matching) setQuestion(matching.question);
      } else if (initialRunId && deepRes && deepRes.status === "rejected") {
        // bad deep link (run doesn't exist, was pruned, typo in URL).
        // surface as an error rather than silently fall through, and
        // bounce the URL back to root so a refresh doesn't keep failing.
        const reason = deepRes.reason as unknown;
        setError(
          reason instanceof Error
            ? `Could not load run ${initialRunId}: ${reason.message}`
            : `Could not load run ${initialRunId}`,
        );
        syncRunUrl(null);
        setSelectedRunId(null);
      }

      const firstFailure = settled
        .slice(0, 4)
        .find((s) => s.status === "rejected");
      if (firstFailure && firstFailure.status === "rejected") {
        const reason = firstFailure.reason as unknown;
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    })();
    return () => abort.abort();
  }, []);

  // auto-scroll log panel as new lines arrive.
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs]);

  async function runQuery() {
    if (!modelId) return;
    setError(null);
    setResult(null);
    setSelectedRunId(null);
    syncRunUrl(null);
    setLogs([]);
    setLoading(true);
    try {
      const final = await streamQuery({ question, model: modelId }, (ev) => onStreamEvent(ev));
      setSelectedRunId(final.run_id);
      // newly-completed run gets a shareable URL via history.replaceState,
      // so the user can immediately copy the address bar and send the
      // permalink to a labmate.
      syncRunUrl(final.run_id);
      // history just got a new entry — pull the updated list.
      void refreshRuns();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  function onStreamEvent(event: StreamEvent) {
    if (event.type === "log") {
      setLogs((prev) => [...prev, { level: event.level, msg: event.msg, t: event.t }]);
    } else if (event.type === "result") {
      setResult(event.data);
    } else if (event.type === "error") {
      setError(event.message);
    }
  }

  async function onSelectRun(runId: string) {
    if (loading) return;
    setError(null);
    setLogs([]);
    setSelectedRunId(runId);
    syncRunUrl(runId);
    try {
      const r = await getRun(runId);
      setResult(r);
      const run = runs.find((x) => x.run_id === runId);
      if (run) setQuestion(run.question);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar
        info={info}
        runs={runs}
        runsHasMore={runsHasMore}
        runsLoadingMore={runsLoadingMore}
        onLoadMoreRuns={() => void loadMoreRuns()}
        selectedRunId={selectedRunId}
        onSelectRun={onSelectRun}
        onRefresh={() => void refreshRuns()}
        onNewChat={() => {
          // start fresh: clear question, result, error, logs, and the
          // active history pin. existing history rows stay where they
          // are — the user can still click back into them. also reset
          // the URL to / so the address bar reflects the empty state
          // (otherwise a refresh would re-load whatever run was last
          // pinned).
          setQuestion("");
          setResult(null);
          setError(null);
          setLogs([]);
          setSelectedRunId(null);
          syncRunUrl(null);
        }}
      />

      <main className="flex-1 min-w-0 flex flex-col">
        <div className="w-full max-w-4xl mx-auto px-6 py-10 flex flex-col gap-8">
          <header className="flex items-start justify-between gap-4">
            <div className="flex flex-col gap-2 min-w-0">
              <div className="flex items-center gap-3">
                {/* PloverAI bird logo. /favicon.svg is the same asset
                    used by the browser tab icon, so the wordmark and
                    the favicon stay visually in sync without
                    maintaining two files. eager priority because it
                    sits above-the-fold. */}
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src="/favicon.svg"
                  alt="PloverAI logo"
                  width={36}
                  height={36}
                  className="h-9 w-9 shrink-0 dark:invert"
                />
                <h1 className="text-3xl font-semibold tracking-tight">PloverAI</h1>
              </div>
              <p className="text-zinc-600 dark:text-zinc-400">
                Type a question. An{" "}
                <InlineLink href="https://openrouter.ai/">LLM</InlineLink> turns it
                into a one-hop{" "}
                <InlineLink href="https://github.com/NCATSTranslator/ReasonerAPI">
                  TRAPI
                </InlineLink>{" "}
                query, runs it against{" "}
                <InlineLink href="https://github.com/RTXteam/PloverDB">
                  PloverDB
                </InlineLink>{" "}
                (which hosts{" "}
                <InlineLink href="https://github.com/RTXteam/RTX-KG2">
                  RTX-KG2
                </InlineLink>
                ), and returns a graph-grounded answer with citations.
              </p>
            </div>
            <HeaderMeta />
          </header>

          {/* unified chat-box: rounded container holds the textarea plus
              a bottom action bar with the question and model pickers on
              the left/right and a circular Send button. styled to feel
              like a single input even though the bottom controls are
              separate elements. */}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void runQuery();
            }}
          >
            <div
              className="group rounded-2xl border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 focus-within:border-zinc-400 dark:focus-within:border-zinc-500 focus-within:shadow-sm transition-colors"
            >
              <textarea
                required
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => {
                  // submit on Enter, but allow Shift+Enter for newlines.
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    if (!loading && question.trim() && modelId) void runQuery();
                  }
                }}
                placeholder="Ask a one-hop biomedical question, or pick one of the gold questions below…"
                rows={3}
                className="w-full resize-none bg-transparent px-4 pt-4 pb-2 text-base placeholder:text-zinc-400 dark:placeholder:text-zinc-500 focus:outline-none disabled:opacity-60"
                disabled={loading}
              />

              <div className="flex items-center gap-2 px-3 pb-2.5 pt-1">
                <QuestionsDropdown
                  questions={goldQuestions}
                  disabled={loading}
                  onSelect={(q) => setQuestion(q.nl_question)}
                />

                <div className="flex-1" />

                <ModelDropdown
                  models={models}
                  value={modelId}
                  onChange={setModelId}
                  disabled={loading}
                  compact
                />

                <SendButton
                  loading={loading}
                  disabled={loading || !question.trim() || !modelId}
                />
              </div>
            </div>
          </form>

          <AnimatePresence>
            {(loading || logs.length > 0) && (
              <LogPanel logs={logs} logRef={logRef} loading={loading} />
            )}
          </AnimatePresence>

          <AnimatePresence>
            {error && (
              <motion.div
                initial={{ opacity: 0, y: -6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0 }}
                className="rounded-md border border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950 p-4 text-red-900 dark:text-red-100"
              >
                <p className="font-semibold mb-1">Error</p>
                <p className="text-sm font-mono break-words">{error}</p>
              </motion.div>
            )}
          </AnimatePresence>

          <AnimatePresence mode="wait">
            {result && (
              <ResultPanel
                key={result.run_id}
                question={question}
                model={modelId}
                r={result}
              />
            )}
          </AnimatePresence>
        </div>
      </main>
    </div>
  );
}

function LogPanel({
  logs,
  logRef,
  loading,
}: {
  logs: LogLine[];
  // structural shape that matches useRef<HTMLDivElement | null>(null);
  // avoids the React.RefObject type which is `@deprecated` in @types/react 19.
  logRef: { current: HTMLDivElement | null };
  loading: boolean;
}) {
  return (
    <motion.section
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      transition={{ duration: 0.15 }}
      className="flex flex-col gap-2"
    >
      <div className="flex items-center gap-3">
        <span className="text-sm font-medium text-zinc-700 dark:text-zinc-300">
          Pipeline progress
        </span>
        {loading && <Spinner />}
        <span className="text-xs text-zinc-500 dark:text-zinc-400 font-mono">
          {logs.length} {logs.length === 1 ? "line" : "lines"}
        </span>
      </div>
      <div
        ref={logRef}
        className="rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900/50 p-3 font-mono text-xs leading-snug max-h-72 overflow-y-auto"
      >
        {logs.length === 0 ? (
          <span className="text-zinc-500">waiting for first log line…</span>
        ) : (
          logs.map((l, i) => (
            <motion.div
              key={i}
              layout
              initial={{ opacity: 0, x: -4 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.1 }}
              className="flex gap-2 whitespace-pre-wrap"
            >
              <span className="text-zinc-400 w-14 shrink-0 tabular-nums">{l.t.toFixed(2)}s</span>
              <span className={`w-12 shrink-0 ${levelColor(l.level)}`}>{l.level}</span>
              <span className="break-words">{stripRichMarkup(l.msg)}</span>
            </motion.div>
          ))
        )}
      </div>
    </motion.section>
  );
}

function Spinner() {
  return (
    <span
      className="inline-block h-3 w-3 rounded-full border-2 border-blue-500 border-t-transparent animate-spin"
      aria-label="loading"
    />
  );
}

function stripRichMarkup(s: string): string {
  return s.replace(/\[\/?[^\]]*\]/g, "");
}

function levelColor(level: string): string {
  switch (level) {
    case "ERROR":
    case "CRITICAL":
      return "text-red-600 dark:text-red-400";
    case "WARNING":
      return "text-amber-600 dark:text-amber-400";
    case "INFO":
      return "text-blue-600 dark:text-blue-400";
    default:
      return "text-zinc-500 dark:text-zinc-400";
  }
}

// circular Send button on the right edge of the chat box. shows an
// up-arrow when idle and a small pulse while the pipeline streams.
function SendButton({ loading, disabled }: { loading: boolean; disabled: boolean }) {
  return (
    <motion.button
      whileHover={!disabled ? { scale: 1.05 } : undefined}
      whileTap={!disabled ? { scale: 0.95 } : undefined}
      type="submit"
      disabled={disabled}
      aria-label={loading ? "Querying" : "Send"}
      className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900 disabled:bg-zinc-300 dark:disabled:bg-zinc-700 disabled:text-zinc-500 disabled:cursor-not-allowed transition-colors shadow-sm"
    >
      {loading ? <SendSpinner /> : <ArrowUpIcon />}
    </motion.button>
  );
}

function ArrowUpIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
      <path
        fillRule="evenodd"
        d="M10 3.5a1 1 0 01.707.293l5 5a1 1 0 11-1.414 1.414L11 6.914V15.5a1 1 0 11-2 0V6.914L5.707 10.207a1 1 0 11-1.414-1.414l5-5A1 1 0 0110 3.5z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function SendSpinner() {
  return (
    <span className="inline-block h-3.5 w-3.5 rounded-full border-2 border-current border-t-transparent animate-spin" />
  );
}

// top-right corner of the main header. lab attribution + repo link,
// both as low-key text/icon links. research-grade signature — sits
// next to the page title without distracting from the chat.
function HeaderMeta() {
  const REPO_URL = "https://github.com/RTXteam/PloverAI";
  return (
    <div className="flex items-center gap-3 shrink-0 text-[11px] text-zinc-500 dark:text-zinc-500 mt-1.5">
      <a
        href="https://lab.saramsey.org/"
        target="_blank"
        rel="noreferrer"
        className="hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors tracking-wide"
      >
        RamseyLab
      </a>
      <span className="text-zinc-300 dark:text-zinc-700">·</span>
      <span>2026</span>
      <a
        href={REPO_URL}
        target="_blank"
        rel="noreferrer"
        aria-label="Source repository"
        title="Source repository"
        className="ml-0.5 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
      >
        <GitHubIcon />
      </a>
    </div>
  );
}

// inline link inside body prose. low-key: matches surrounding text
// weight, picks up colour on hover. used for the service mentions
// in the page subtitle (LLM, TRAPI, PloverDB, RTX-KG2).
function InlineLink({ href, children }: { href: string; children: import("react").ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="underline decoration-zinc-300 dark:decoration-zinc-700 underline-offset-2 hover:text-zinc-900 dark:hover:text-zinc-100 hover:decoration-zinc-500 transition-colors"
    >
      {children}
    </a>
  );
}

function GitHubIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"
      />
    </svg>
  );
}

