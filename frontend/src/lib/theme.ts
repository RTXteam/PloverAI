"use client";

// theme state: light / dark / system. uses useSyncExternalStore so the
// store (localStorage + matchMedia) can legitimately differ between
// server and client — React knows this is an external-store pattern
// and tolerates the hydration mismatch instead of erroring.

import { useCallback, useSyncExternalStore } from "react";

export type Theme = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "ploverai:theme";

// runs as an inline script (via next/script beforeInteractive) before
// React hydrates. reads the stored preference, falls back to the OS,
// and toggles the .dark class on <html> — Tailwind v4 picks that up
// via the @custom-variant in globals.css.
export const fouclessThemeBootstrap = `
(function () {
  try {
    var stored = localStorage.getItem(${JSON.stringify(STORAGE_KEY)});
    var system = window.matchMedia('(prefers-color-scheme: dark)').matches;
    var dark = stored === 'dark' || (stored !== 'light' && system);
    document.documentElement.classList.toggle('dark', dark);
  } catch (e) { /* private mode etc — fall through to default light */ }
})();
`.trim();

function readStored(): Theme {
  if (typeof window === "undefined") return "system";
  const v = window.localStorage.getItem(STORAGE_KEY);
  return v === "light" || v === "dark" ? v : "system";
}

function systemDark(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function applyToDOM(theme: Theme): ResolvedTheme {
  if (typeof window === "undefined") return "light";
  const dark = theme === "dark" || (theme === "system" && systemDark());
  document.documentElement.classList.toggle("dark", dark);
  return dark ? "dark" : "light";
}

// useSyncExternalStore plumbing for the theme preference. one
// subscriber listens for cross-tab `storage` events and for OS theme
// changes when in `system` mode.
function subscribeTheme(notify: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const onStorage = (e: StorageEvent) => {
    if (e.key === STORAGE_KEY) notify();
  };
  window.addEventListener("storage", onStorage);
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  // only fires while we're in `system` mode, but subscribing always
  // keeps the wiring simple; if the user is on light/dark explicitly
  // the OS change is irrelevant and harmless.
  mq.addEventListener("change", notify);
  return () => {
    window.removeEventListener("storage", onStorage);
    mq.removeEventListener("change", notify);
  };
}

const getServerSnapshot = (): Theme => "system";

export function useTheme(): {
  theme: Theme;
  resolvedTheme: ResolvedTheme;
  setTheme: (t: Theme) => void;
} {
  // server snapshot is always "system" so SSR is deterministic. React
  // will reconcile to the real stored value on the client without a
  // hydration error — this is the contract of useSyncExternalStore.
  const theme = useSyncExternalStore(subscribeTheme, readStored, getServerSnapshot);

  // resolvedTheme is derived from the same store + the matchMedia
  // result. computed via the same useSyncExternalStore so the
  // "system follows OS" case stays reactive without a separate effect.
  const resolvedTheme = useSyncExternalStore(
    subscribeTheme,
    (): ResolvedTheme => {
      const t = readStored();
      return t === "dark" || (t === "system" && systemDark()) ? "dark" : "light";
    },
    (): ResolvedTheme => "light",
  );

  const setTheme = useCallback((t: Theme) => {
    if (t === "system") {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, t);
    }
    // `storage` events don't fire on the same tab that wrote them,
    // so we need to update the DOM directly. then dispatch a synthetic
    // event so the useSyncExternalStore subscriber re-reads.
    applyToDOM(t);
    window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
  }, []);

  return { theme, resolvedTheme, setTheme };
}
