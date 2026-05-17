"use client";

// segmented control with three options: light / dark / system.
// the highlighted background slides with framer-motion's layoutId
// trick — one shared element animates between three positions.

import { motion } from "framer-motion";
import type { ReactElement } from "react";
import type { Theme } from "@/lib/theme";

const OPTIONS: { id: Theme; label: string; icon: ReactElement }[] = [
  { id: "light", label: "Light", icon: <SunIcon /> },
  { id: "dark", label: "Dark", icon: <MoonIcon /> },
  { id: "system", label: "Auto", icon: <ScreenIcon /> },
];

type Props = {
  theme: Theme;
  onChange: (t: Theme) => void;
};

export function ThemeSwitch({ theme, onChange }: Props) {
  return (
    <div
      role="radiogroup"
      aria-label="Theme"
      className="relative flex w-full rounded-md border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-0.5"
    >
      {OPTIONS.map((opt) => {
        const active = opt.id === theme;
        return (
          <button
            key={opt.id}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onChange(opt.id)}
            className="relative flex-1 flex items-center justify-center gap-1.5 py-1.5 text-xs font-medium z-10"
            title={opt.label}
          >
            {active && (
              <motion.span
                layoutId="theme-switch-indicator"
                className="absolute inset-0 rounded bg-zinc-100 dark:bg-zinc-800 -z-10"
                transition={{ type: "spring", stiffness: 400, damping: 32 }}
              />
            )}
            <span className={active ? "text-zinc-900 dark:text-zinc-100" : "text-zinc-500"}>
              {opt.icon}
            </span>
            <span className={active ? "text-zinc-900 dark:text-zinc-100" : "text-zinc-500"}>
              {opt.label}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function SunIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
      <path d="M10 4a1 1 0 011 1v1a1 1 0 11-2 0V5a1 1 0 011-1zm4.243 1.757a1 1 0 011.414 1.414l-.707.707a1 1 0 11-1.414-1.414l.707-.707zM16 10a1 1 0 011-1h1a1 1 0 110 2h-1a1 1 0 01-1-1zm-1.757 4.243a1 1 0 011.414 1.414l-.707.707a1 1 0 01-1.414-1.414l.707-.707zM10 14a1 1 0 011 1v1a1 1 0 11-2 0v-1a1 1 0 011-1zm-4.243-.343a1 1 0 010 1.414l-.707.707a1 1 0 01-1.414-1.414l.707-.707a1 1 0 011.414 0zM4 10a1 1 0 01-1 1H2a1 1 0 110-2h1a1 1 0 011 1zm.343-4.243a1 1 0 011.414 0l.707.707A1 1 0 015.05 7.879l-.707-.707a1 1 0 010-1.414zM10 7a3 3 0 100 6 3 3 0 000-6z" />
    </svg>
  );
}
function MoonIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
      <path d="M17.293 13.293A8 8 0 016.707 2.707a8.001 8.001 0 1010.586 10.586z" />
    </svg>
  );
}
function ScreenIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
      <path
        fillRule="evenodd"
        d="M3 5a2 2 0 012-2h10a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2V5zm5 10a1 1 0 011-1h2a1 1 0 110 2H9a1 1 0 01-1-1z"
        clipRule="evenodd"
      />
    </svg>
  );
}
