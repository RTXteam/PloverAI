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

// the View Transitions API is not in every browser we support, so it is
// feature-detected through a cast rather than typed as always-present.
// `unknown` first: lib.dom already declares startViewTransition in newer
// TS versions with a wider signature, and a plain intersection would
// clash with it.
type ViewTransitionApi = { startViewTransition?: (cb: () => void) => unknown };

// returns the transition starter only when a cross-fade is actually
// appropriate. hidden tabs are excluded because a transition captures a
// screenshot of the page — starting one on a background tab (an OS theme
// change arriving while the tab is hidden) can leave the stale snapshot
// on screen until the tab is focused again.
function viewTransitionStarter(): ((cb: () => void) => unknown) | null {
  if (typeof window === "undefined" || typeof document === "undefined") return null;
  if (document.visibilityState !== "visible") return null;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return null;
  const doc = document as unknown as ViewTransitionApi;
  const start = doc.startViewTransition;
  return typeof start === "function" ? start.bind(doc) : null;
}

// runs `flip` inside a root view transition when one is available, else
// runs it straight. ONLY safe to call from event paths (a click, a
// storage event, an OS theme change) — never from a render or from a
// useSyncExternalStore snapshot, which must stay pure.
function withThemeTransition(flip: () => void): void {
  const start = viewTransitionStarter();
  if (!start) {
    flip();
    return;
  }
  // a transition the browser skips (another one began, or there is
  // nothing to animate) rejects its promises; that is not an error here.
  const transition = start(flip) as
    | { ready?: Promise<unknown>; finished?: Promise<unknown>; updateCallbackDone?: Promise<unknown> }
    | undefined;
  for (const done of [transition?.ready, transition?.finished, transition?.updateCallbackDone]) {
    done?.catch(() => undefined);
  }
}

// event-path DOM sync used by the store subscribers. no-ops when the
// class is already what it should be, which is what keeps the two
// useSyncExternalStore subscriptions (theme + resolvedTheme) from
// starting two overlapping transitions for one event.
function syncDOMAnimated(theme: Theme): void {
  if (typeof document === "undefined") return;
  const dark = theme === "dark" || (theme === "system" && systemDark());
  if (document.documentElement.classList.contains("dark") === dark) return;
  withThemeTransition(() => {
    applyToDOM(theme);
  });
}

// useSyncExternalStore plumbing for the theme preference. one
// subscriber listens for cross-tab `storage` events and for OS theme
// changes when in `system` mode.
function subscribeTheme(notify: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  // both handlers are event paths, so they may drive the animated DOM
  // sync. without it a cross-tab write or an OS flip in `system` mode
  // updated React state but left html.dark stale.
  const onStorage = (e: StorageEvent) => {
    if (e.key !== STORAGE_KEY) return;
    syncDOMAnimated(readStored());
    notify();
  };
  window.addEventListener("storage", onStorage);
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  // only matters while we're in `system` mode, but subscribing always
  // keeps the wiring simple; if the user is on light/dark explicitly
  // syncDOMAnimated sees the class is already right and does nothing.
  const onSystemChange = () => {
    syncDOMAnimated(readStored());
    notify();
  };
  mq.addEventListener("change", onSystemChange);
  return () => {
    window.removeEventListener("storage", onStorage);
    mq.removeEventListener("change", onSystemChange);
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
    // `storage` events don't fire on the same tab that wrote them, so we
    // update the DOM directly, then dispatch a synthetic event so the
    // useSyncExternalStore subscribers re-read. both go INSIDE the view
    // transition callback so the class flip and the React re-render land
    // in the same captured frame — one cross-fade for the whole page.
    withThemeTransition(() => {
      applyToDOM(t);
      window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
    });
  }, []);

  return { theme, resolvedTheme, setTheme };
}
