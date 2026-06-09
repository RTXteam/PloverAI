"use client";

// left sidebar: collapsible drawer with run history + system info.
// the drawer collapses to a narrow rail (icon + dates only) and
// expands to show full run details, animated with framer-motion.
// also hosts the "New chat" button at the top and the light/dark/
// system theme switch at the bottom.

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import type { RunSummary, ServiceHealth, ServiceInfo } from "@/lib/api";
import { useTheme } from "@/lib/theme";
import { ThemeSwitch } from "./ThemeSwitch";

type Props = {
  info: ServiceInfo | null;
  runs: RunSummary[];
  // infinite-scroll plumbing: `runsHasMore` tells the list whether to
  // mount the sentinel + spinner at the bottom; `onLoadMoreRuns` fires
  // when the sentinel scrolls into view (debounced by the parent's
  // `runsLoadingMore` flag so we don't double-fire while a fetch is
  // in flight).
  runsHasMore: boolean;
  runsLoadingMore: boolean;
  onLoadMoreRuns: () => void;
  selectedRunId: string | null;
  onSelectRun: (runId: string) => void;
  onRefresh: () => void;
  onNewChat: () => void;
  // per-service liveness for the System panel status dots (empty until the
  // first /services/health poll returns).
  serviceHealth: ServiceHealth[];
};

export function Sidebar({
  info,
  runs,
  runsHasMore,
  runsLoadingMore,
  onLoadMoreRuns,
  selectedRunId,
  onSelectRun,
  onRefresh,
  onNewChat,
  serviceHealth,
}: Props) {
  const { theme, setTheme } = useTheme();
  const [collapsed, setCollapsed] = useState(false);
  const width = collapsed ? 56 : 320;

  return (
    <motion.aside
      animate={{ width }}
      transition={{ duration: 0.22, ease: "easeOut" }}
      className="shrink-0 sticky top-0 h-screen border-r border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 overflow-hidden flex flex-col"
    >
      <div className="flex items-center justify-between gap-2 px-3 py-3 border-b border-zinc-200 dark:border-zinc-800">
        <AnimatePresence>
          {!collapsed && (
            <motion.div
              key="title"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="text-sm font-semibold tracking-tight"
            >
              PloverAI
            </motion.div>
          )}
        </AnimatePresence>
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className="p-1.5 rounded-md hover:bg-zinc-200 dark:hover:bg-zinc-800 text-zinc-600 dark:text-zinc-400"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {collapsed ? <IconExpand /> : <IconCollapse />}
        </button>
      </div>

      <AnimatePresence mode="wait">
        {collapsed ? (
          <CollapsedRail
            key="collapsed"
            runs={runs}
            selectedRunId={selectedRunId}
            onSelectRun={onSelectRun}
          />
        ) : (
          <ExpandedSidebar
            key="expanded"
            info={info}
            runs={runs}
            runsHasMore={runsHasMore}
            runsLoadingMore={runsLoadingMore}
            onLoadMoreRuns={onLoadMoreRuns}
            selectedRunId={selectedRunId}
            onSelectRun={onSelectRun}
            onRefresh={onRefresh}
            onNewChat={onNewChat}
            serviceHealth={serviceHealth}
            theme={theme}
            onThemeChange={setTheme}
          />
        )}
      </AnimatePresence>
    </motion.aside>
  );
}

type ExpandedProps = Props & {
  theme: import("@/lib/theme").Theme;
  onThemeChange: (t: import("@/lib/theme").Theme) => void;
};

function ExpandedSidebar({
  info,
  runs,
  runsHasMore,
  runsLoadingMore,
  onLoadMoreRuns,
  selectedRunId,
  onSelectRun,
  onRefresh,
  onNewChat,
  serviceHealth,
  theme,
  onThemeChange,
}: ExpandedProps) {
  // sentinel-based infinite scroll: the small div at the bottom of the
  // runs list reports when it intersects the scroll container. that's
  // our cue to fetch the next page. IntersectionObserver is more
  // reliable than wiring an onScroll handler (no layout-thrash, no
  // throttling needed, works correctly with sticky / nested containers).
  const sentinelRef = useRef<HTMLLIElement>(null);
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    if (!runsHasMore) return; // no more pages → no observer
    const observer = new IntersectionObserver(
      (entries) => {
        // only fire on "enters viewport" transitions; `runsLoadingMore`
        // gate is enforced in the parent's loadMoreRuns so we don't
        // double-fire while a fetch is in flight.
        if (entries[0]?.isIntersecting) onLoadMoreRuns();
      },
      { threshold: 0.1 },
    );
    observer.observe(el);
    return () => observer.disconnect();
    // `runs.length` is in the dep list so a fresh sentinel
    // (re-created after the list grows) re-attaches the observer.
  }, [onLoadMoreRuns, runsHasMore, runs.length]);
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.15 }}
      className="flex-1 flex flex-col min-h-0"
    >
      {/* "New chat" sits above the history list — primary action. */}
      <div className="px-3 pt-3 pb-2">
        <motion.button
          whileHover={{ scale: 1.01 }}
          whileTap={{ scale: 0.98 }}
          type="button"
          onClick={onNewChat}
          className="w-full flex items-center justify-center gap-2 rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 hover:bg-zinc-100 dark:hover:bg-zinc-800 px-3 py-2 text-sm font-medium text-zinc-800 dark:text-zinc-200"
        >
          <PlusIcon />
          New chat
        </motion.button>
      </div>

      <div className="flex items-center justify-between px-3 pt-3 pb-2">
        <h2 className="text-xs uppercase tracking-wide text-zinc-500 font-medium">History</h2>
        <button
          type="button"
          onClick={onRefresh}
          className="text-xs text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200"
        >
          refresh
        </button>
      </div>
      <ul className="flex-1 overflow-y-auto px-2 pb-2 space-y-1">
        {runs.length === 0 && (
          <li className="text-xs text-zinc-500 px-2 py-3">no runs yet — ask a question.</li>
        )}
        {runs.map((r) => (
          <RunRow
            key={r.run_id}
            run={r}
            active={r.run_id === selectedRunId}
            onSelect={() => onSelectRun(r.run_id)}
          />
        ))}
        {/* sentinel + spinner for infinite scroll. only mounts while
            more pages exist; once the backend returns a short page we
            unmount and show a small "end of history" line so the user
            knows they've reached the bottom. */}
        {runs.length > 0 && runsHasMore && (
          <li
            ref={sentinelRef}
            className="flex items-center justify-center gap-2 px-2 py-3 text-xs text-zinc-500"
          >
            {runsLoadingMore ? (
              <>
                <SidebarSpinner />
                <span>loading older runs…</span>
              </>
            ) : (
              <span className="opacity-60">scroll for more</span>
            )}
          </li>
        )}
        {runs.length > 0 && !runsHasMore && (
          <li className="text-center text-[10px] uppercase tracking-wider text-zinc-400 dark:text-zinc-600 py-3">
            end of history
          </li>
        )}
      </ul>

      {info && <SystemInfoPanel info={info} serviceHealth={serviceHealth} />}

      {/* theme switch sits at the very bottom — secondary control. */}
      <div className="border-t border-zinc-200 dark:border-zinc-800 px-3 py-3">
        <ThemeSwitch theme={theme} onChange={onThemeChange} />
      </div>
    </motion.div>
  );
}

function CollapsedRail({
  runs,
  selectedRunId,
  onSelectRun,
}: {
  runs: RunSummary[];
  selectedRunId: string | null;
  onSelectRun: (id: string) => void;
}) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.15 }}
      className="flex-1 overflow-y-auto py-2"
    >
      {runs.slice(0, 16).map((r) => {
        const active = r.run_id === selectedRunId;
        return (
          <button
            key={r.run_id}
            type="button"
            onClick={() => onSelectRun(r.run_id)}
            title={`${prettyDate(r.started_utc)} · ${r.model_id} · ${r.question}`}
            className={`block w-full text-center text-[10px] font-mono py-2 ${active ? "bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300" : "text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-900"}`}
          >
            {r.model_id}
          </button>
        );
      })}
    </motion.div>
  );
}

function RunRow({
  run,
  active,
  onSelect,
}: {
  run: RunSummary;
  active: boolean;
  onSelect: () => void;
}) {
  const ok = run.status === "ok";
  return (
    <motion.li
      layout
      whileHover={{ x: 2 }}
      transition={{ duration: 0.12 }}
    >
      <button
        type="button"
        onClick={onSelect}
        className={`w-full text-left rounded-md px-2.5 py-2 text-xs border ${active ? "border-blue-300 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/40" : "border-transparent hover:border-zinc-200 dark:hover:border-zinc-800 hover:bg-white dark:hover:bg-zinc-900"}`}
      >
        <div className="flex items-center gap-2 mb-1">
          <span
            className={`inline-block h-1.5 w-1.5 rounded-full shrink-0 ${ok ? "bg-emerald-500" : "bg-red-500"}`}
            aria-hidden
          />
          <span className="font-mono text-[10px] text-zinc-500 tabular-nums">
            {prettyDate(run.started_utc)}
          </span>
          <span className="font-mono text-[10px] text-zinc-500 ml-auto">{run.model_id}</span>
        </div>
        <div className="line-clamp-2 text-zinc-800 dark:text-zinc-200 leading-snug">
          {run.question}
        </div>
        <div className="mt-1 flex items-center gap-2 text-[10px] text-zinc-500 font-mono">
          <span>{run.elapsed_s.toFixed(1)}s</span>
          <span>·</span>
          <span>${run.cost_usd.toFixed(4)}</span>
          {run.outcome && (
            <>
              <span>·</span>
              <span className="truncate">{run.outcome}</span>
            </>
          )}
        </div>
      </button>
    </motion.li>
  );
}

function SystemInfoPanel({
  info,
  serviceHealth,
}: {
  info: ServiceInfo;
  serviceHealth: ServiceHealth[];
}) {
  const statusByName = new Map(serviceHealth.map((s) => [s.name, s]));
  return (
    <div className="border-t border-zinc-200 dark:border-zinc-800 px-3 py-3 text-[11px] font-mono space-y-1.5 text-zinc-600 dark:text-zinc-400">
      <h2 className="text-xs uppercase tracking-wide text-zinc-500 font-medium font-sans mb-2">
        System
      </h2>
      <Row label="service" value={`${info.service} v${info.version}`} />
      <Row label="up since" value={prettyDate(info.started_utc)} />
      <Row label="KG" value={info.kg_version} />
      <Row label="Biolink" value={info.biolink_version} />
      <Row label="TRAPI" value={info.trapi_version} />
      <div className="pt-1.5 border-t border-zinc-200 dark:border-zinc-800 mt-2 space-y-1">
        {Object.entries(info.endpoints).map(([name, url]) => {
          const h = statusByName.get(name);
          return (
            <a
              key={name}
              href={url}
              target="_blank"
              rel="noreferrer"
              className="flex items-center justify-between gap-2 hover:text-blue-600 dark:hover:text-blue-400"
            >
              <span className="flex items-center gap-1.5 shrink-0">
                <StatusDot status={h?.status} />
                <span className="text-zinc-500">{name}</span>
              </span>
              <span className="truncate">{url.replace(/^https?:\/\//, "")}</span>
            </a>
          );
        })}
      </div>
    </div>
  );
}

function StatusDot({ status }: { status?: string }) {
  // green = reachable, amber = degraded (5xx), red = down, grey = not yet
  // checked. live dots blink slowly to read as a real-time indicator; the
  // title gives the plain-language status on hover.
  const { color, label, blink } =
    status === "ok"
      ? { color: "bg-emerald-500", label: "active", blink: true }
      : status === "degraded"
        ? { color: "bg-amber-500", label: "degraded", blink: true }
        : status === "down"
          ? { color: "bg-red-500", label: "unreachable", blink: true }
          : { color: "bg-zinc-300 dark:bg-zinc-600", label: "checking", blink: false };
  return (
    <span
      title={label}
      className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${color} ${blink ? "animate-slow-blink" : ""}`}
    />
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-zinc-500">{label}</span>
      <span className="truncate">{value}</span>
    </div>
  );
}

// parses "2026-04-29T18-36-28Z" (our utc stamp format) and renders
// "Apr 29 · 18:36" — compact, no year clutter, still unambiguous.
function prettyDate(stamp: string): string {
  const m = stamp.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})Z/);
  if (!m) return stamp;
  const [, year, mon, day, hh, mm] = m;
  const monthName = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][Number(mon) - 1];
  const now = new Date();
  const sameYear = String(now.getUTCFullYear()) === year;
  return sameYear
    ? `${monthName} ${Number(day)} · ${hh}:${mm}`
    : `${monthName} ${Number(day)} ${year} · ${hh}:${mm}`;
}

// small 3x3 spinning ring used by the infinite-scroll sentinel while a
// load-more fetch is in flight. matches the style of the main result-
// panel SendSpinner so the visual vocabulary across the app stays
// consistent.
function SidebarSpinner() {
  return (
    <span
      className="inline-block h-3 w-3 rounded-full border-2 border-zinc-400 border-t-transparent animate-spin"
      aria-label="loading older runs"
    />
  );
}

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
      <path
        fillRule="evenodd"
        d="M10 4a1 1 0 011 1v4h4a1 1 0 110 2h-4v4a1 1 0 11-2 0v-4H5a1 1 0 110-2h4V5a1 1 0 011-1z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function IconCollapse() {
  return (
    <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
      <path
        fillRule="evenodd"
        d="M12.707 5.293a1 1 0 010 1.414L9.414 10l3.293 3.293a1 1 0 01-1.414 1.414l-4-4a1 1 0 010-1.414l4-4a1 1 0 011.414 0z"
        clipRule="evenodd"
      />
    </svg>
  );
}
function IconExpand() {
  return (
    <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
      <path
        fillRule="evenodd"
        d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z"
        clipRule="evenodd"
      />
    </svg>
  );
}
