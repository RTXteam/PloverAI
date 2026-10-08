"use client";

// a small select for the workbench's toolbars (sort order, filters), in
// place of the native <select>, whose menu looks different in every
// browser. the menu is fixed to the viewport under its trigger, so a pane
// that clips its overflow does not cut it off. Escape, a click outside,
// scrolling or resizing close it; the arrow keys move between options.

import { useEffect, useLayoutEffect, useRef, useState } from "react";

export type SelectOption<T extends string> = { value: T; label: string };

export function SelectMenu<T extends string>({
  value,
  options,
  onChange,
  title,
  label,
}: {
  value: T;
  options: SelectOption<T>[];
  onChange: (value: T) => void;
  // the tooltip on the trigger.
  title?: string;
  // the accessible name of the list.
  label: string;
}) {
  const [open, setOpen] = useState(false);
  const [place, setPlace] = useState<{ top: number; left: number; minWidth: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const current = options.find((o) => o.value === value) ?? options[0];

  useLayoutEffect(() => {
    if (!open || !triggerRef.current) return;
    const box = triggerRef.current.getBoundingClientRect();
    const width = Math.max(box.width, 128);
    setPlace({ top: box.bottom + 4, left: Math.min(box.right - width, window.innerWidth - width - 8), minWidth: width });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!triggerRef.current?.contains(target) && !listRef.current?.contains(target)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close();
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [open]);

  // focus the chosen option when the menu opens.
  useEffect(() => {
    if (!open || !place) return;
    listRef.current?.querySelector<HTMLButtonElement>('[aria-selected="true"]')?.focus();
  }, [open, place]);

  const move = (step: number) => {
    const buttons = [...(listRef.current?.querySelectorAll<HTMLButtonElement>("button") ?? [])];
    const at = buttons.indexOf(document.activeElement as HTMLButtonElement);
    buttons[(at + step + buttons.length) % buttons.length]?.focus();
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        title={title}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            setOpen(true);
          }
        }}
        className={`inline-flex h-6 shrink-0 items-center gap-1 rounded border px-1.5 text-[11.5px] leading-none ${
          open
            ? "border-zinc-400 bg-zinc-100 text-zinc-800 dark:border-zinc-600 dark:bg-zinc-800 dark:text-zinc-100"
            : "border-zinc-200 text-zinc-600 hover:border-zinc-300 hover:text-zinc-800 dark:border-zinc-700 dark:text-zinc-300 dark:hover:border-zinc-600 dark:hover:text-zinc-100"
        }`}
      >
        {current?.label}
        <svg width="9" height="9" viewBox="0 0 10 10" aria-hidden className={`text-zinc-400 transition-transform ${open ? "rotate-180" : ""}`}>
          <path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && place && (
        <ul
          ref={listRef}
          role="listbox"
          aria-label={label}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              move(e.key === "ArrowDown" ? 1 : -1);
            }
          }}
          className="fixed z-50 overflow-hidden rounded-md border border-zinc-200 bg-white py-1 text-[12px] shadow-lg dark:border-zinc-700 dark:bg-zinc-900"
          style={{ top: place.top, left: Math.max(8, place.left), minWidth: place.minWidth }}
        >
          {options.map((option) => {
            const chosen = option.value === value;
            return (
              <li key={option.value}>
                <button
                  type="button"
                  role="option"
                  aria-selected={chosen}
                  onClick={() => {
                    onChange(option.value);
                    setOpen(false);
                    triggerRef.current?.focus();
                  }}
                  className={`flex w-full items-center gap-2 whitespace-nowrap px-2.5 py-1 text-left outline-none hover:bg-zinc-100 focus:bg-zinc-100 dark:hover:bg-zinc-800 dark:focus:bg-zinc-800 ${
                    chosen ? "font-medium text-zinc-900 dark:text-zinc-100" : "text-zinc-600 dark:text-zinc-300"
                  }`}
                >
                  <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden className={chosen ? "text-sky-600 dark:text-sky-400" : "invisible"}>
                    <path d="M1.8 5.2 4 7.3 8.2 2.8" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  {option.label}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
