"use client";

// pill-style dropdown that lives inside the unified chat-input box.
// opens DOWNWARD (below the trigger). selecting a row hands the
// question text up to the parent so the textarea prefills.

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import type { GoldQuestion } from "@/lib/api";

type Props = {
  questions: GoldQuestion[];
  onSelect: (question: GoldQuestion) => void;
  disabled?: boolean;
};

export function QuestionsDropdown({ questions, onSelect, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

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

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => !disabled && setOpen((v) => !v)}
        disabled={disabled || questions.length === 0}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 rounded-full border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 hover:bg-zinc-100 dark:hover:bg-zinc-800 px-3 py-1.5 text-xs font-medium text-zinc-700 dark:text-zinc-300 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        <PlusIcon />
        Gold questions
        <Chevron open={open} />
      </button>

      <AnimatePresence>
        {open && (
          <motion.ul
            role="listbox"
            initial={{ opacity: 0, y: -6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.14, ease: "easeOut" }}
            className="absolute left-0 top-full mt-2 z-30 w-[28rem] max-w-[calc(100vw-3rem)] rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-950 shadow-xl overflow-hidden max-h-96 overflow-y-auto"
          >
            <li className="px-3 py-2 text-[10px] uppercase tracking-wide text-zinc-500 border-b border-zinc-200 dark:border-zinc-800 sticky top-0 bg-white dark:bg-zinc-950">
              Pick one of the gold benchmark questions
            </li>
            {questions.map((q) => (
              <li
                key={q.id}
                role="option"
                aria-selected={false}
                onClick={() => {
                  onSelect(q);
                  setOpen(false);
                }}
                className="cursor-pointer px-3 py-2.5 text-sm border-b border-zinc-100 dark:border-zinc-900 last:border-b-0 hover:bg-zinc-50 dark:hover:bg-zinc-900"
              >
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-400 dark:text-zinc-600 shrink-0">
                    {q.id}
                  </span>
                  <span className="text-zinc-900 dark:text-zinc-100 leading-snug">{q.nl_question}</span>
                </div>
                {(q.pinned_entity_label || q.answer_category) && (
                  <div className="text-[11px] text-zinc-500 dark:text-zinc-400 mt-1 ml-7 font-mono">
                    {q.pinned_entity_label && <span>{q.pinned_entity_label}</span>}
                    {q.pinned_entity_label && q.answer_category && <span> · </span>}
                    {q.answer_category && <span>→ {q.answer_category.replace(/^biolink:/, "")}</span>}
                  </div>
                )}
              </li>
            ))}
          </motion.ul>
        )}
      </AnimatePresence>
    </div>
  );
}

function PlusIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
      <path
        fillRule="evenodd"
        d="M10 4a1 1 0 011 1v4h4a1 1 0 110 2h-4v4a1 1 0 11-2 0v-4H5a1 1 0 110-2h4V5a1 1 0 011-1z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <motion.svg
      animate={{ rotate: open ? 180 : 0 }}
      transition={{ duration: 0.15 }}
      width="11"
      height="11"
      viewBox="0 0 20 20"
      fill="currentColor"
      aria-hidden
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
