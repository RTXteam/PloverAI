"use client";

// custom dropdown: full-width labels, opens DOWN, animated with
// framer-motion. the native <select> truncated long option text and
// the user wanted a proper component instead.

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import type { ModelInfo } from "@/lib/api";

type Props = {
  models: ModelInfo[];
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
  // when `compact`, the trigger renders as a pill ("Model · m5 ▾")
  // suitable for inline placement inside the chat bar. when false,
  // the trigger fills its parent and shows the full model row.
  compact?: boolean;
  // when `openUpward`, the menu anchors to the bottom of the trigger
  // and opens above. matches the chat-bar layout where the trigger
  // sits at the bottom of the input box.
  openUpward?: boolean;
};

export function ModelDropdown({ models, value, onChange, disabled, compact, openUpward }: Props) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const selected = models.find((m) => m.id === value) ?? null;

  // close on click outside + Escape. one effect, both listeners.
  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const triggerClass = compact
    ? "inline-flex items-center gap-1.5 rounded-full border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 hover:bg-zinc-100 dark:hover:bg-zinc-800 px-3 py-1.5 text-xs font-medium text-zinc-700 dark:text-zinc-300 disabled:opacity-50 disabled:cursor-not-allowed"
    : "w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-3 py-2 text-left font-mono text-sm flex items-center justify-between gap-3 hover:border-zinc-400 dark:hover:border-zinc-600 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed";

  // anchor + sizing:
  // - openUpward: above the trigger, right-aligned, fixed wide menu.
  // - compact (not openUpward): below + right-aligned, fixed wide menu
  //   so labels with prices fit even though the trigger is a small pill.
  // - default (full-width trigger): below + matches the trigger width.
  const menuClass = openUpward
    ? "absolute right-0 bottom-full mb-2 z-30 w-[36rem] max-w-[calc(100vw-3rem)] rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-950 shadow-xl overflow-hidden max-h-96 overflow-y-auto"
    : compact
      ? "absolute right-0 top-full mt-2 z-30 w-[36rem] max-w-[calc(100vw-3rem)] rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-950 shadow-xl overflow-hidden max-h-96 overflow-y-auto"
      : "absolute left-0 right-0 top-full mt-1 z-30 rounded-md border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-950 shadow-lg overflow-hidden max-h-96 overflow-y-auto";

  const enterY = openUpward ? 6 : -4;

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => !disabled && setOpen((v) => !v)}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={triggerClass}
      >
        {compact ? (
          <CompactTrigger model={selected} />
        ) : selected ? (
          <ModelLabel m={selected} />
        ) : (
          <span className="text-zinc-500">loading…</span>
        )}
        <Chevron open={open} />
      </button>

      <AnimatePresence>
        {open && (
          <motion.ul
            role="listbox"
            initial={{ opacity: 0, y: enterY }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: enterY }}
            transition={{ duration: 0.12, ease: "easeOut" }}
            className={menuClass}
          >
            {models.map((m) => {
              const active = m.id === value;
              return (
                <li
                  key={m.id}
                  role="option"
                  aria-selected={active}
                  onClick={() => {
                    onChange(m.id);
                    setOpen(false);
                  }}
                  className={`cursor-pointer px-3 py-2.5 font-mono text-sm border-b border-zinc-100 dark:border-zinc-900 last:border-b-0 ${active ? "bg-blue-50 dark:bg-blue-950/40" : "hover:bg-zinc-50 dark:hover:bg-zinc-900"}`}
                >
                  <ModelLabel m={m} highlight={active} />
                </li>
              );
            })}
          </motion.ul>
        )}
      </AnimatePresence>
    </div>
  );
}

function CompactTrigger({ model }: { model: ModelInfo | null }) {
  if (!model) return <span className="text-zinc-500">Model</span>;
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="text-zinc-500">Model</span>
      <span className="font-mono text-zinc-900 dark:text-zinc-100">{model.id}</span>
    </span>
  );
}

function ModelLabel({ m, highlight }: { m: ModelInfo; highlight?: boolean }) {
  const name = m.slug.includes("/") ? m.slug.split("/").slice(1).join("/") : m.slug;
  return (
    <span className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
      <span className={`font-semibold ${highlight ? "text-blue-700 dark:text-blue-300" : ""}`}>{m.id}</span>
      <span>{name}</span>
      <TierBadge tier={m.tier} />
      {m.recommended && <RecommendedBadge />}
      <span className="text-zinc-500 dark:text-zinc-400 text-xs">
        ${m.price_in.toFixed(2)} in · ${m.price_out.toFixed(2)} out per 1M
      </span>
    </span>
  );
}

function RecommendedBadge() {
  return (
    <span className="text-[10px] uppercase tracking-wide font-medium px-1.5 py-0.5 rounded bg-blue-100 text-blue-800 dark:bg-blue-950/60 dark:text-blue-300">
      recommended
    </span>
  );
}

function TierBadge({ tier }: { tier: string }) {
  const cls =
    tier === "frontier"
      ? "bg-purple-100 text-purple-800 dark:bg-purple-950/60 dark:text-purple-300"
      : tier === "budget"
        ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300"
        : "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300";
  return (
    <span className={`text-[10px] uppercase tracking-wide font-medium px-1.5 py-0.5 rounded ${cls}`}>
      {tier}
    </span>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <motion.svg
      animate={{ rotate: open ? 180 : 0 }}
      transition={{ duration: 0.15 }}
      width="14"
      height="14"
      viewBox="0 0 20 20"
      fill="currentColor"
      aria-hidden="true"
      className="text-zinc-500 shrink-0"
    >
      <path
        fillRule="evenodd"
        d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z"
        clipRule="evenodd"
      />
    </motion.svg>
  );
}
