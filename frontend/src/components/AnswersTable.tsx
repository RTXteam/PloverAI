"use client";

// ARAX's ranked answers as a table, the left pane of the workbench: rank,
// answer and CURIE, ARAX's score, the number of facts behind it, and
// whether the LLM picked it (Stage 11). picked answers are the ones the
// reasoning graph draws; choosing a row isolates its paths there, and
// hovering one lights them up. the other way round, the rows of the
// answers on what the graph's pointer is over are marked here, so the
// graph needs no tooltip of its own for an answer.
// while a query runs, only the picked answers are known, without ranks.

import { useEffect, useMemo, useRef, useState } from "react";
import { SelectMenu } from "@/components/SelectMenu";

export type RankedAnswer = {
  rank: number | null;
  curie: string;
  name: string;
  score: number | null;
  facts: number | null;
};

type SortKey = "rank" | "score" | "facts";

const SORTS: { value: SortKey; label: string }[] = [
  { value: "rank", label: "by rank" },
  { value: "score", label: "by score" },
  { value: "facts", label: "by facts" },
];

export function AnswersTable({
  rows,
  total,
  picked,
  selected,
  onSelect,
  linked,
  onHover,
  placeholder,
}: {
  rows: RankedAnswer[];
  // how many answers ARAX returned in all (rows hold its top k).
  total: number | null;
  picked: ReadonlySet<string>;
  selected: string | null;
  onSelect: (curie: string | null) => void;
  // the answers whose paths the graph's pointer is over.
  linked?: ReadonlySet<string>;
  onHover?: (curie: string | null) => void;
  placeholder: string;
}) {
  const [sort, setSort] = useState<SortKey>("rank");
  const [pickedOnly, setPickedOnly] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);

  // a row lit from the graph is scrolled into view if it is not.
  const firstLinked = linked && linked.size > 0 ? [...linked][0] : null;
  useEffect(() => {
    if (!firstLinked || !bodyRef.current) return;
    const row = bodyRef.current.querySelector<HTMLElement>(`tr[data-curie="${CSS.escape(firstLinked)}"]`);
    row?.scrollIntoView({ block: "nearest" });
  }, [firstLinked]);

  const shown = useMemo(() => {
    const value = (row: RankedAnswer) =>
      sort === "rank" ? (row.rank ?? Infinity) : -((sort === "score" ? row.score : row.facts) ?? -Infinity);
    return rows
      .filter((row) => !pickedOnly || picked.has(row.curie))
      .sort((a, b) => value(a) - value(b) || (a.rank ?? Infinity) - (b.rank ?? Infinity));
  }, [rows, sort, pickedOnly, picked]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-8 shrink-0 items-center gap-3 overflow-hidden whitespace-nowrap border-b border-zinc-200 px-3 text-[11.5px] text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
        <span className="truncate tabular-nums" title="answers ARAX ranked · answers the LLM picked">
          {total !== null ? `${total} ranked` : `${rows.length} answers`} · {picked.size} picked
        </span>
        <span className="flex-1" />
        <label className="inline-flex shrink-0 items-center gap-1" title="show only the answers the LLM picked">
          <input type="checkbox" checked={pickedOnly} onChange={(e) => setPickedOnly(e.target.checked)} className="h-3 w-3" />
          picked
        </label>
        <SelectMenu value={sort} options={SORTS} onChange={setSort} title="Sort the table" label="Sort answers" />
      </div>
      {rows.length === 0 ? (
        <p className="px-3 py-4 text-[12px] text-zinc-500 dark:text-zinc-400">{placeholder}</p>
      ) : (
        <div ref={bodyRef} className="scroll-thin min-h-0 flex-1 overflow-y-auto" onMouseLeave={() => onHover?.(null)}>
          <table className="w-full table-fixed border-collapse text-[12px]">
            <colgroup>
              <col className="w-9" />
              <col />
              <col className="w-12" />
              <col className="w-10" />
            </colgroup>
            <thead className="sticky top-0 bg-white text-left text-[10.5px] uppercase tracking-wide text-zinc-500 dark:bg-zinc-950 dark:text-zinc-400">
              <tr className="border-b border-zinc-200 dark:border-zinc-800">
                <th className="px-2 py-1 text-right font-medium">#</th>
                <th className="px-2 py-1 font-medium">answer</th>
                <th className="px-2 py-1 text-right font-medium" title="ARAX's normalised score">score</th>
                <th className="px-2 py-1 text-right font-medium" title="facts ARAX returned behind this answer">facts</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((row) => {
                const isPicked = picked.has(row.curie);
                const isSelected = selected === row.curie;
                const isLinked = linked?.has(row.curie) ?? false;
                return (
                  <tr
                    key={row.curie}
                    data-curie={row.curie}
                    onClick={() => onSelect(isSelected ? null : row.curie)}
                    onMouseEnter={() => onHover?.(isPicked ? row.curie : null)}
                    aria-selected={isSelected}
                    title={[
                      `${row.rank !== null ? `#${row.rank} ` : ""}${row.name} · ${row.curie}`,
                      row.score !== null ? `ARAX score ${row.score.toFixed(3)}` : null,
                      row.facts !== null ? `${row.facts} facts behind it in ARAX's answer` : null,
                      isPicked ? "picked by the LLM, drawn in the graph" : "not picked by the LLM",
                    ]
                      .filter(Boolean)
                      .join("\n")}
                    className={`cursor-pointer border-b border-zinc-100 align-top transition-colors duration-100 dark:border-zinc-900 ${
                      isSelected
                        ? "bg-sky-50 dark:bg-sky-950/40"
                        : isLinked
                          ? "bg-zinc-100 dark:bg-zinc-800/70"
                          : "hover:bg-zinc-50 dark:hover:bg-zinc-900"
                    }`}
                  >
                    <td className="px-2 py-1.5 text-right font-mono tabular-nums text-zinc-500">{row.rank ?? "·"}</td>
                    <td className="min-w-0 px-2 py-1.5">
                      <div className="flex min-w-0 items-center gap-1.5">
                        <span
                          aria-label={isPicked ? "picked by the LLM" : undefined}
                          title={isPicked ? "picked by the LLM, drawn in the graph" : "not picked"}
                          className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${
                            isPicked ? "bg-sky-600 dark:bg-sky-400" : "bg-transparent ring-1 ring-zinc-300 dark:ring-zinc-700"
                          }`}
                        />
                        <span className={`truncate ${isPicked ? "font-medium text-zinc-900 dark:text-zinc-100" : "text-zinc-700 dark:text-zinc-300"}`}>
                          {row.name}
                        </span>
                      </div>
                      <div className="truncate pl-3 font-mono text-[10.5px] text-zinc-500">{row.curie}</div>
                    </td>
                    <td className="px-2 py-1.5 text-right font-mono tabular-nums text-zinc-600 dark:text-zinc-400">
                      {row.score !== null ? row.score.toFixed(2) : "·"}
                    </td>
                    <td className="px-2 py-1.5 text-right font-mono tabular-nums text-zinc-600 dark:text-zinc-400">
                      {row.facts ?? "·"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
