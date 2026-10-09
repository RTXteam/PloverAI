"use client";

// every run on disk as one table: when, the question, the model, how it
// ended, time and cost. filters work on the runs loaded so far; "load
// more" fetches the next page. opening a row shows it in the Ask view.

import { useMemo, useState } from "react";
import { SelectMenu } from "@/components/SelectMenu";
import type { RunSummary } from "@/lib/api";
import { TONE_CLASS, outcomeText, prettyDate, seconds, toneOf, usd, type Tone } from "@/lib/format";

const TONES: { id: Tone | "all"; label: string }[] = [
  { id: "all", label: "all results" },
  { id: "answered", label: "answered" },
  { id: "empty", label: "no answer" },
  { id: "refused", label: "refused" },
  { id: "failed", label: "failed" },
];

export function RunsView({
  runs,
  hasMore,
  loadingMore,
  onLoadMore,
  onRefresh,
  onOpen,
  selectedRunId,
}: {
  runs: RunSummary[];
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  onRefresh: () => void;
  onOpen: (runId: string) => void;
  selectedRunId: string | null;
}) {
  const [text, setText] = useState("");
  const [model, setModel] = useState("all");
  const [tone, setTone] = useState<Tone | "all">("all");

  const models = useMemo(() => [...new Set(runs.map((r) => r.model_id))].sort(), [runs]);
  const shown = useMemo(() => {
    const needle = text.trim().toLowerCase();
    return runs.filter(
      (r) =>
        (!needle || r.question.toLowerCase().includes(needle)) &&
        (model === "all" || r.model_id === model) &&
        (tone === "all" || toneOf(r.status, r.outcome) === tone),
    );
  }, [runs, text, model, tone]);
  const totalCost = shown.reduce((sum, r) => sum + r.cost_usd, 0);
  const answered = shown.filter((r) => toneOf(r.status, r.outcome) === "answered").length;


  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-zinc-200 px-4 py-2 text-[12px] dark:border-zinc-800">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Filter questions"
          className="w-72 rounded border border-zinc-300 bg-white px-2 py-1 dark:border-zinc-700 dark:bg-zinc-900"
        />
        <SelectMenu
          value={model}
          options={[{ value: "all", label: "all models" }, ...models.map((m) => ({ value: m, label: m }))]}
          onChange={setModel}
          title="Show the runs of one model"
          label="Model"
        />
        <SelectMenu
          value={tone}
          options={TONES.map((t) => ({ value: t.id, label: t.label }))}
          onChange={(v) => setTone(v as Tone | "all")}
          title="Show the runs with one outcome"
          label="Outcome"
        />
        <span className="flex-1" />
        <span className="tabular-nums text-zinc-500 dark:text-zinc-400">
          {shown.length} of {runs.length}
          {hasMore ? "+" : ""} runs · {answered} answered · {usd(totalCost)}
        </span>
        <button
          type="button"
          onClick={onRefresh}
          className="rounded border border-zinc-300 px-2 py-1 hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800"
        >
          Refresh
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <table className="w-full table-fixed border-collapse text-[12.5px]">
          <colgroup>
            <col className="w-36" />
            <col />
            <col className="w-44" />
            <col className="w-40" />
            <col className="w-20" />
            <col className="w-20" />
          </colgroup>
          <thead className="sticky top-0 bg-white text-left text-[10.5px] uppercase tracking-wide text-zinc-500 dark:bg-zinc-950 dark:text-zinc-400">
            <tr className="border-b border-zinc-200 dark:border-zinc-800">
              <th className="px-4 py-1.5 font-medium">started (UTC)</th>
              <th className="px-2 py-1.5 font-medium">question</th>
              <th className="px-2 py-1.5 font-medium">model</th>
              <th className="px-2 py-1.5 font-medium">result</th>
              <th className="px-2 py-1.5 text-right font-medium">time</th>
              <th className="px-4 py-1.5 text-right font-medium">cost</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr
                key={r.run_id}
                onClick={() => onOpen(r.run_id)}
                className={`cursor-pointer border-b border-zinc-100 dark:border-zinc-900 ${
                  r.run_id === selectedRunId ? "bg-sky-50 dark:bg-sky-950/40" : "hover:bg-zinc-50 dark:hover:bg-zinc-900"
                }`}
              >
                <td className="whitespace-nowrap px-4 py-1.5 font-mono text-[11.5px] tabular-nums text-zinc-500">{prettyDate(r.started_utc)}</td>
                <td className="truncate px-2 py-1.5 text-zinc-900 dark:text-zinc-100" title={r.question}>
                  {r.question}
                </td>
                <td className="truncate px-2 py-1.5 font-mono text-[11.5px] text-zinc-600 dark:text-zinc-400" title={r.model_slug}>
                  {r.model_id} · {r.model_slug.split("/").pop()}
                </td>
                <td className={`px-2 py-1.5 ${TONE_CLASS[toneOf(r.status, r.outcome)]}`}>{outcomeText(r.status, r.outcome)}</td>
                <td className="px-2 py-1.5 text-right font-mono text-[11.5px] tabular-nums text-zinc-600 dark:text-zinc-400">
                  {seconds(r.elapsed_s)}
                </td>
                <td className="px-4 py-1.5 text-right font-mono text-[11.5px] tabular-nums text-zinc-600 dark:text-zinc-400">
                  {usd(r.cost_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {shown.length === 0 && (
          <p className="px-4 py-6 text-[12px] text-zinc-500 dark:text-zinc-400">No runs match these filters.</p>
        )}
        {hasMore && (
          <div className="px-4 py-3">
            <button
              type="button"
              onClick={onLoadMore}
              disabled={loadingMore}
              className="rounded border border-zinc-300 px-2.5 py-1 text-[12px] hover:bg-zinc-100 disabled:opacity-60 dark:border-zinc-700 dark:hover:bg-zinc-800"
            >
              {loadingMore ? "Loading…" : "Load older runs"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
