"use client";

// the whole app: a full-width research workbench. a top bar names the
// service and the versions it runs on and switches between two views:
//   Ask  — ask a question, watch the run, read its result (AskView);
//   Runs — every run on disk as a table (RunsView).
// this component owns the data: service info, models, gold questions,
// the runs list, the running query's stream, and the result on screen.
// any run is reachable at /?run=<run_id> (a query parameter, not a path,
// because the app is a static export).

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  getInfo,
  getModels,
  getQuestions,
  getRun,
  getRuns,
  streamQuery,
  type ExampleQuestion,
  type ModelInfo,
  type QueryResponse,
  type RunSummary,
  type ServiceInfo,
  type StreamEvent,
} from "@/lib/api";
import { AskView } from "@/components/AskView";
import { RunsView } from "@/components/RunsView";
import type { LogLine } from "@/components/Inspector";
import { ThemeSwitch } from "@/components/ThemeSwitch";
import { EMPTY_LIVE, reduceLive, type LiveState } from "@/lib/liveState";
import { prettyDate } from "@/lib/format";
import { useTheme } from "@/lib/theme";

type View = "ask" | "runs";

const RUNS_PAGE_SIZE = 50;

function readRunIdFromUrl(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("run");
}

// keeps the address bar on the run being shown, so it can be shared.
function syncRunUrl(runId: string | null) {
  if (typeof window === "undefined") return;
  const target = runId ? `${window.location.pathname}?run=${encodeURIComponent(runId)}` : window.location.pathname;
  if (window.location.pathname + window.location.search !== target) window.history.replaceState(null, "", target);
}

export default function Workbench() {
  const [view, setView] = useState<View>("ask");
  const [info, setInfo] = useState<ServiceInfo | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [examples, setExamples] = useState<ExampleQuestion[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runsHasMore, setRunsHasMore] = useState(true);
  const [runsLoadingMore, setRunsLoadingMore] = useState(false);
  const [modelId, setModelId] = useState("");
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [live, setLive] = useState<LiveState>(EMPTY_LIVE);
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);

  const refreshRuns = useCallback(async () => {
    try {
      const page = await getRuns(RUNS_PAGE_SIZE, 0);
      setRuns(page);
      setRunsHasMore(page.length === RUNS_PAGE_SIZE);
    } catch {
      // the history failing to load does not stop asking questions.
    }
  }, []);

  const loadMoreRuns = useCallback(async () => {
    if (runsLoadingMore || !runsHasMore) return;
    setRunsLoadingMore(true);
    try {
      const next = await getRuns(RUNS_PAGE_SIZE, runs.length);
      setRuns((prev) => {
        const seen = new Set(prev.map((r) => r.run_id));
        return [...prev, ...next.filter((r) => !seen.has(r.run_id))];
      });
      setRunsHasMore(next.length === RUNS_PAGE_SIZE);
    } catch {
      // try again with the button.
    } finally {
      setRunsLoadingMore(false);
    }
  }, [runs.length, runsLoadingMore, runsHasMore]);

  // start-up: everything in parallel, plus the run in the URL if any.
  useEffect(() => {
    const abort = new AbortController();
    void (async () => {
      const initialRunId = readRunIdFromUrl();
      if (initialRunId) setSelectedRunId(initialRunId);
      const [infoRes, modelsRes, runsRes, questionsRes, runRes] = await Promise.allSettled([
        getInfo(abort.signal),
        getModels(abort.signal),
        getRuns(RUNS_PAGE_SIZE, 0, abort.signal),
        getQuestions(abort.signal),
        initialRunId ? getRun(initialRunId, abort.signal) : Promise.resolve(null),
      ]);
      if (abort.signal.aborted) return;
      if (infoRes.status === "fulfilled") setInfo(infoRes.value);
      if (modelsRes.status === "fulfilled" && modelsRes.value.length > 0) {
        const ms = modelsRes.value;
        setModels(ms);
        // the cheapest benchmark model; the free dev one is slow.
        const candidates = ms.some((m) => m.tier !== "dev") ? ms.filter((m) => m.tier !== "dev") : ms;
        setModelId([...candidates].sort((a, b) => a.price_in + a.price_out - (b.price_in + b.price_out))[0].id);
      }
      if (runsRes.status === "fulfilled") {
        setRuns(runsRes.value);
        setRunsHasMore(runsRes.value.length === RUNS_PAGE_SIZE);
      }
      if (questionsRes.status === "fulfilled") setExamples(questionsRes.value);
      if (runRes.status === "fulfilled" && runRes.value) {
        setResult(runRes.value);
        setQuestion(runRes.value.question ?? "");
      } else if (initialRunId && runRes.status === "rejected") {
        const reason = runRes.reason as unknown;
        setError(`Could not load run ${initialRunId}: ${reason instanceof Error ? reason.message : String(reason)}`);
        syncRunUrl(null);
        setSelectedRunId(null);
      }
      const failed = [infoRes, modelsRes, questionsRes].find((s) => s.status === "rejected");
      if (failed && failed.status === "rejected") {
        const reason = failed.reason as unknown;
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    })();
    return () => abort.abort();
  }, []);

  function onStreamEvent(event: StreamEvent) {
    if (event.type === "log") setLogs((prev) => [...prev, { level: event.level, msg: event.msg, t: event.t }]);
    else if (event.type === "stage") setLive((prev) => reduceLive(prev, event));
    else if (event.type === "result") setResult(event.data);
    else if (event.type === "error") setError(event.message);
  }

  async function runQuery() {
    if (!modelId || !question.trim()) return;
    setView("ask");
    setError(null);
    setResult(null);
    setSelectedRunId(null);
    syncRunUrl(null);
    setLogs([]);
    setLive(EMPTY_LIVE);
    setStartedAt(Date.now());
    setLoading(true);
    try {
      const final = await streamQuery({ question, model: modelId }, onStreamEvent);
      setSelectedRunId(final.run_id);
      syncRunUrl(final.run_id);
      void refreshRuns();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  async function openRun(runId: string) {
    if (loading) return;
    setView("ask");
    setError(null);
    setLogs([]);
    setSelectedRunId(runId);
    syncRunUrl(runId);
    try {
      const r = await getRun(runId);
      setResult(r);
      setQuestion(r.question ?? "");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function clear() {
    if (loading) return;
    setQuestion("");
    setResult(null);
    setError(null);
    setLogs([]);
    setLive(EMPTY_LIVE);
    setSelectedRunId(null);
    syncRunUrl(null);
  }

  // the brand in the top bar: back to the start page, a fresh question.
  // a question still running keeps running; only the view changes.
  function goHome() {
    setView("ask");
    clear();
  }

  // the history is shared by every visitor: opening it fetches the latest.
  function showView(next: View) {
    setView(next);
    if (next === "runs") void refreshRuns();
  }

  return (
    // the window never scrolls; each pane scrolls on its own. so opening a
    // menu or a long list never shifts the page sideways.
    <div className="flex h-screen min-h-0 flex-col overflow-hidden bg-white text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      <TopBar
        info={info}
        view={view}
        onView={showView}
        onHome={goHome}
        onNew={clear}
        canClear={!loading && (result !== null || question.length > 0)}
      />
      <main className="min-h-0 flex-1">
        {view === "ask" ? (
          <AskView
            question={question}
            onQuestion={setQuestion}
            models={models}
            modelId={modelId}
            onModel={setModelId}
            examples={examples}
            loading={loading}
            startedAt={startedAt}
            onRun={() => void runQuery()}
            result={result}
            live={live}
            logs={logs}
            error={error}
          />
        ) : (
          <RunsView
            runs={runs}
            hasMore={runsHasMore}
            loadingMore={runsLoadingMore}
            onLoadMore={() => void loadMoreRuns()}
            onRefresh={() => void refreshRuns()}
            onOpen={(id) => void openRun(id)}
            selectedRunId={selectedRunId}
          />
        )}
      </main>
    </div>
  );
}

// ------------------------------------------------------------------ top bar

const REPO_URL = "https://github.com/bazarkua/ploverai";

function TopBar({
  info,
  view,
  onView,
  onHome,
  onNew,
  canClear,
}: {
  info: ServiceInfo | null;
  view: View;
  onView: (v: View) => void;
  onHome: () => void;
  onNew: () => void;
  canClear: boolean;
}) {
  const { theme, setTheme } = useTheme();
  const [systemOpen, setSystemOpen] = useState(false);
  const tab = (v: View, label: string) => (
    <button
      type="button"
      onClick={() => onView(v)}
      aria-current={view === v ? "page" : undefined}
      className={`-mb-px flex h-11 items-center border-b-2 px-1 ${
        view === v
          ? "border-sky-600 text-zinc-900 dark:border-sky-400 dark:text-zinc-100"
          : "border-transparent text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200"
      }`}
    >
      {label}
    </button>
  );
  return (
    <header className="relative flex h-11 shrink-0 items-center gap-4 border-b border-zinc-200 px-4 text-[13px] dark:border-zinc-800">
      {/* a real link, so it also opens in a new tab; a plain click goes
          home without reloading. */}
      <Link
        href="/"
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault();
          onHome();
        }}
        title="Home"
        className="flex items-center gap-2 rounded hover:opacity-80"
      >
        {/* eslint-disable-next-line @next/next/no-img-element -- the static export serves /favicon.svg as is; next/image would add a loader for one small icon */}
        <img src="/favicon.svg" alt="" width={22} height={22} className="h-[22px] w-[22px] dark:invert" />
        <span className="text-[15px] font-semibold tracking-tight">PloverAI</span>
      </Link>
      <span className="hidden truncate text-[11.5px] text-zinc-500 md:inline dark:text-zinc-400">
        {info
          ? [info.reasoner_version, info.kg_version, `Biolink ${info.biolink_version}`, `TRAPI ${info.trapi_version}`]
              .filter(Boolean)
              .join(" · ")
          : "connecting…"}
        {info?.public ? " · public site" : ""}
      </span>
      <nav className="ml-auto flex items-center gap-4" aria-label="Views">
        {tab("ask", "Ask")}
        {tab("runs", "Runs")}
      </nav>
      <button
        type="button"
        onClick={onNew}
        disabled={!canClear}
        className="inline-flex h-7 items-center whitespace-nowrap rounded border border-zinc-300 px-2.5 text-[12px] hover:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:hover:bg-zinc-800"
      >
        New question
      </button>
      <button
        type="button"
        onClick={() => setSystemOpen((v) => !v)}
        aria-expanded={systemOpen}
        className="inline-flex h-7 items-center text-[12px] text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200"
      >
        System
      </button>
      <a href="https://lab.saramsey.org/" target="_blank" rel="noreferrer" className="hidden text-[11.5px] text-zinc-500 hover:text-zinc-800 lg:inline dark:hover:text-zinc-200">
        RamseyLab
      </a>
      <a href={REPO_URL} target="_blank" rel="noreferrer" aria-label="Source repository" className="hidden text-zinc-500 hover:text-zinc-800 lg:inline dark:hover:text-zinc-200">
        <GitHubIcon />
      </a>
      <ThemeSwitch theme={theme} onChange={setTheme} />
      {systemOpen && info && <SystemPanel info={info} onClose={() => setSystemOpen(false)} />}
    </header>
  );
}

function SystemPanel({ info, onClose }: { info: ServiceInfo; onClose: () => void }) {
  const row = (label: string, value: string) => (
    <div className="flex justify-between gap-4">
      <span className="text-zinc-500">{label}</span>
      <span className="truncate">{value}</span>
    </div>
  );
  return (
    <div className="absolute right-4 top-full z-30 mt-1 w-[26rem] rounded border border-zinc-200 bg-white p-3 font-mono text-[11.5px] text-zinc-700 shadow-lg dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-300">
      <div className="mb-2 flex items-center justify-between font-sans">
        <span className="text-[11px] uppercase tracking-wide text-zinc-500">System</span>
        <button type="button" onClick={onClose} className="text-[11px] text-zinc-500 hover:underline">
          close
        </button>
      </div>
      <div className="space-y-1">
        {row("service", `${info.service} v${info.version}`)}
        {row("up since", prettyDate(info.started_utc))}
        {info.reasoner_version && row("reasoner", info.reasoner_version)}
        {row("knowledge graph", info.kg_version)}
        {row("Biolink", info.biolink_version)}
        {row("TRAPI", info.trapi_version)}
      </div>
      <div className="mt-2 space-y-1 border-t border-zinc-200 pt-2 dark:border-zinc-800">
        {Object.entries(info.endpoints).map(([name, url]) => (
          <a key={name} href={url} target="_blank" rel="noreferrer" className="flex justify-between gap-4 hover:text-sky-700 dark:hover:text-sky-400">
            <span className="text-zinc-500">{name}</span>
            <span className="truncate">{url.replace(/^https?:\/\//, "")}</span>
          </a>
        ))}
      </div>
    </div>
  );
}

function GitHubIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"
      />
    </svg>
  );
}
