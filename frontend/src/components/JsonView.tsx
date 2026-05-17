"use client";

// pretty-printed JSON in a code-style block with line numbers. kept
// minimal on purpose — we don't need a fully-interactive tree (the
// raw value is also available via Export → JSON), just something
// research-grade and copy-friendly.

import { useMemo } from "react";

type Props = {
  value: unknown;
  // collapsed strings longer than this are clipped with an [n more lines]
  // hint, expandable via a button. set to 0 to never clip.
  maxLines?: number;
};

export function JsonView({ value, maxLines }: Props) {
  const text = useMemo(() => {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }, [value]);

  const lines = text.split("\n");
  const clipped = typeof maxLines === "number" && maxLines > 0 && lines.length > maxLines;
  const display = clipped ? lines.slice(0, maxLines) : lines;

  return (
    <div className="rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950/60 overflow-hidden">
      <pre className="text-[12px] leading-relaxed font-mono overflow-x-auto">
        <code>
          {display.map((line, i) => (
            <div key={i} className="flex">
              <span className="select-none text-zinc-400 dark:text-zinc-600 text-right w-10 pr-3 pl-2 tabular-nums shrink-0">
                {i + 1}
              </span>
              <span className="flex-1 pr-3 whitespace-pre">{line}</span>
            </div>
          ))}
        </code>
      </pre>
      {clipped && (
        <div className="px-3 py-1.5 border-t border-zinc-200 dark:border-zinc-800 text-xs text-zinc-500">
          + {lines.length - (maxLines ?? 0)} more lines. Export the run to view the full JSON.
        </div>
      )}
    </div>
  );
}
