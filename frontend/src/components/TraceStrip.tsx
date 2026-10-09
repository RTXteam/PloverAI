"use client";

// the pipeline trace under the query band: eleven steps in order, each
// with its kind (LLM, service, code), what it produced and how long it
// took (lib/pipelineTrace.ts). the step running now is marked and timed;
// a run that stopped shows where and why. hovering a step opens a card
// with its details; clicking it opens the step's prompt and artifacts in
// the inspector's pipeline tab.

import { useState } from "react";
import type { StepKind, StepState, TraceStep } from "@/lib/pipelineTrace";

type Hover = { step: TraceStep; left: number; top: number } | null;

const KIND_TAG: Record<StepKind, string> = { LLM: "LLM", service: "service", code: "code" };

export function TraceStrip({ steps, onOpen }: { steps: TraceStep[]; onOpen?: (anchor: string) => void }) {
  const [hover, setHover] = useState<Hover>(null);
  if (steps.length === 0) return null;
  return (
    <div className="relative shrink-0 border-b border-zinc-200 dark:border-zinc-800">
      <ol className="scroll-thin flex overflow-x-auto" aria-label="Pipeline steps">
        {steps.map((step, i) => (
          <li key={step.key} className="flex min-w-[7.5rem] flex-1 items-stretch">
            <button
              type="button"
              onClick={onOpen ? () => onOpen(step.anchor) : undefined}
              onMouseEnter={(e) => {
                const box = e.currentTarget.getBoundingClientRect();
                setHover({ step, left: Math.min(box.left, window.innerWidth - 300), top: box.bottom + 4 });
              }}
              onMouseLeave={() => setHover(null)}
              onFocus={(e) => {
                const box = e.currentTarget.getBoundingClientRect();
                setHover({ step, left: Math.min(box.left, window.innerWidth - 300), top: box.bottom + 4 });
              }}
              onBlur={() => setHover(null)}
              aria-current={step.state === "active" ? "step" : undefined}
              className={`group relative flex min-w-0 flex-1 flex-col items-start gap-0.5 px-3 py-1.5 text-left ${
                onOpen ? "" : "cursor-default"
              }`}
            >
              {/* the step running now is underlined, like the open tab
                  of the top bar; a fill would be a box cut off by the
                  chevrons between the steps. */}
              <span
                aria-hidden
                className={`absolute inset-x-3 bottom-0 h-[2px] rounded-full transition-opacity ${
                  step.state === "active" ? "bg-sky-600 opacity-100 dark:bg-sky-400" : "opacity-0"
                }`}
              />
              <span className="flex w-full items-center gap-1.5">
                <StepMark state={step.state} />
                <span
                  className={`truncate text-[11.5px] font-medium ${STATE_TEXT[step.state]} ${
                    onOpen ? "decoration-zinc-300 underline-offset-2 group-hover:underline dark:decoration-zinc-600" : ""
                  }`}
                >
                  {step.label}
                </span>
              </span>
              <span className="w-full truncate pl-[17px] text-[11px] text-zinc-600 dark:text-zinc-400">
                {step.detail ?? (step.state === "pending" ? " " : step.state === "active" ? "running…" : "")}
              </span>
              <span className="pl-[17px] text-[10.5px] tabular-nums text-zinc-400 dark:text-zinc-500">
                {step.seconds !== null ? `${step.seconds < 10 ? step.seconds.toFixed(1) : Math.round(step.seconds)} s` : " "}
              </span>
            </button>
            {i < steps.length - 1 && (
              <span aria-hidden className="flex items-center text-zinc-300 dark:text-zinc-700">
                <svg width="6" height="22" viewBox="0 0 6 22">
                  <path d="M0.5 0.5 5.5 11 0.5 21.5" fill="none" stroke="currentColor" />
                </svg>
              </span>
            )}
          </li>
        ))}
      </ol>
      {hover && (
        <div
          role="tooltip"
          className="pointer-events-none fixed z-40 w-72 rounded border border-zinc-200 bg-white p-2.5 text-[11.5px] leading-snug text-zinc-700 shadow-lg dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-300"
          style={{ left: Math.max(8, hover.left), top: hover.top }}
        >
          <div className="flex items-center gap-1.5">
            <StepMark state={hover.step.state} />
            <span className="font-medium text-zinc-900 dark:text-zinc-100">{hover.step.label}</span>
            <span className="text-zinc-400">· {KIND_TAG[hover.step.kind]} · stage {hover.step.anchor}</span>
          </div>
          {hover.step.detail && <div className="mt-1 break-words">{hover.step.detail}</div>}
          {hover.step.more.map((line) => (
            <div key={line} className="mt-0.5 break-words text-zinc-500">
              {line}
            </div>
          ))}
          <div className="mt-1.5 text-[10.5px] text-zinc-400">
            {STATE_NOTE[hover.step.state]}
            {onOpen ? " Click to open its prompt and artifacts." : ""}
          </div>
        </div>
      )}
    </div>
  );
}

const STATE_TEXT: Record<StepState, string> = {
  done: "text-zinc-800 dark:text-zinc-200",
  active: "text-sky-800 dark:text-sky-300",
  pending: "text-zinc-400 dark:text-zinc-600",
  stopped: "text-amber-700 dark:text-amber-400",
};

const STATE_NOTE: Record<StepState, string> = {
  done: "Done.",
  active: "Running now.",
  pending: "Not reached.",
  stopped: "The run stopped here.",
};

function StepMark({ state }: { state: StepState }) {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden className="shrink-0">
      {state === "done" ? (
        <>
          <circle cx="6" cy="6" r="5.5" fill="var(--viz-ink-2)" />
          <path d="M3.4 6.2 5.2 8 8.7 4.3" fill="none" stroke="var(--viz-surface)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </>
      ) : state === "active" ? (
        <>
          <circle cx="6" cy="6" r="5.25" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-sky-600 dark:text-sky-400" />
          <circle cx="6" cy="6" r="2.25" fill="currentColor" className="animate-pulse text-sky-600 dark:text-sky-400" />
        </>
      ) : state === "stopped" ? (
        <>
          <circle cx="6" cy="6" r="5.25" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-amber-600" />
          <path d="M4 4 8 8M8 4 4 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" className="text-amber-600" />
        </>
      ) : (
        <circle cx="6" cy="6" r="5.25" fill="none" stroke="var(--viz-rule)" strokeWidth="1.5" />
      )}
    </svg>
  );
}
