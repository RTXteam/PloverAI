"use client";

// three icon buttons: light / dark / system (follow the OS). the colours
// switch with the rest of the page inside the root view transition
// (lib/theme.ts).

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
      className="inline-flex h-7 items-center gap-0.5 rounded border border-zinc-200 bg-white p-0.5 dark:border-zinc-800 dark:bg-zinc-900"
    >
      {OPTIONS.map((opt) => {
        const active = opt.id === theme;
        return (
          <button
            key={opt.id}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={opt.label}
            onClick={() => onChange(opt.id)}
            title={opt.label}
            className={`flex h-[22px] w-[22px] items-center justify-center rounded-[3px] ${
              active ? "bg-zinc-100 text-zinc-900 dark:bg-zinc-800 dark:text-zinc-100" : "text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200"
            }`}
          >
            {opt.icon}
          </button>
        );
      })}
    </div>
  );
}

const ICON = {
  width: 14,
  height: 14,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.9,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

function SunIcon() {
  return (
    <svg {...ICON}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg {...ICON}>
      <path d="M20 14.6A8.5 8.5 0 1 1 9.4 4a6.6 6.6 0 0 0 10.6 10.6z" />
    </svg>
  );
}

function ScreenIcon() {
  return (
    <svg {...ICON}>
      <rect x="3" y="4.5" width="18" height="12" rx="2" />
      <path d="M8.5 20h7M12 16.5V20" />
    </svg>
  );
}
