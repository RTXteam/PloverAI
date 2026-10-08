// small formatters shared by the workbench views.

// "2026-04-29T18-36-28Z" (the pipeline's UTC stamp) -> "Apr 29 · 18:36",
// with the year only when it is not the current one.
export function prettyDate(stamp: string): string {
  const m = stamp.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})Z/);
  if (!m) return stamp;
  const [, year, mon, day, hh, mm] = m;
  const monthName = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][Number(mon) - 1];
  const sameYear = String(new Date().getUTCFullYear()) === year;
  return sameYear ? `${monthName} ${Number(day)} · ${hh}:${mm}` : `${monthName} ${Number(day)} ${year} · ${hh}:${mm}`;
}

export function usd(value: number): string {
  return `$${value.toFixed(4)}`;
}

export function seconds(value: number): string {
  return `${value.toFixed(1)} s`;
}

// a run's result in four kinds (pipeline.py STATUS_* / OUTCOME_*):
//   answered — ARAX answered and the LLM picked answers;
//   empty    — the run worked but there was nothing to pick;
//   refused  — the pipeline stopped on purpose and said why;
//   failed   — an error: invalid query, a service or the LLM failed.
export type Tone = "answered" | "empty" | "refused" | "failed";

const REFUSED = new Set([
  "out_of_scope",
  "entity_empty",
  "nameres_failed",
  "no_candidate_match",
  "low_confidence_resolution",
  "query_declined",
]);

export function toneOf(status: string, outcome: string | null): Tone {
  if (REFUSED.has(status) || REFUSED.has(outcome ?? "")) return "refused";
  if (status !== "ok") return "failed";
  return outcome === "answered" || outcome === null ? "answered" : "empty";
}

export const TONE_CLASS: Record<Tone, string> = {
  answered: "text-emerald-700 dark:text-emerald-400",
  empty: "text-zinc-500 dark:text-zinc-400",
  refused: "text-amber-700 dark:text-amber-400",
  failed: "text-red-700 dark:text-red-400",
};

export function outcomeText(status: string, outcome: string | null): string {
  return (outcome ?? status).replaceAll("_", " ");
}
