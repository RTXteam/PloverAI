"use client";

// pretty-printed JSON in a code-style block with line numbers. kept
// minimal on purpose — we don't need a fully-interactive tree (the
// raw value is also available via Export → JSON), just something
// research-grade and copy-friendly.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
    <div className="relative rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950/60 overflow-hidden">
      {/* absolutely positioned so it stays put while the <pre> scrolls
          horizontally. line 1 of pretty-printed JSON is always a bare
          brace/bracket, so nothing meaningful sits under it. */}
      <div className="absolute top-1.5 right-1.5 z-10">
        {/* copies the FULL pretty-printed JSON, not just the visible
            (possibly clipped) lines. */}
        <CopyButton text={text} />
      </div>
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

type CopyState = "idle" | "copied" | "failed";

// small copy button in the corner of the block.
function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<CopyState>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // a copy can fire right before the panel unmounts (stage accordions
  // collapse), so the revert timer has to be cancelled on unmount.
  useEffect(() => {
    return () => {
      if (timer.current !== null) clearTimeout(timer.current);
    };
  }, []);

  const onCopy = useCallback(async () => {
    const ok = await writeClipboard(text);
    setState(ok ? "copied" : "failed");
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), 1600);
  }, [text]);

  const label = state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : "Copy";
  return (
    <button
      type="button"
      onClick={onCopy}
      aria-live="polite"
      title={
        state === "failed"
          ? "Clipboard unavailable in this browser — select the JSON and copy manually"
          : "Copy this JSON to the clipboard"
      }
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors ${
        state === "copied"
          ? "border-emerald-300 dark:border-emerald-800 bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300"
          : state === "failed"
            ? "border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/40 text-amber-700 dark:text-amber-300"
            : "border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-700 dark:text-zinc-300"
      }`}
    >
      {state === "copied" ? <CheckIcon /> : <ClipboardIcon />}
      {label}
    </button>
  );
}

// navigator.clipboard is undefined on insecure origins (plain-http LAN
// access to the dev server is a normal way this app gets used) and can
// also reject when the document isn't focused — both fall back to the
// legacy textarea + execCommand path.
async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through
  }
  return legacyCopy(text);
}

function legacyCopy(text: string): boolean {
  if (typeof document === "undefined") return false;
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  // off-screen but still focusable — position:fixed avoids the page
  // scrolling to the element when we select it.
  ta.style.position = "fixed";
  ta.style.top = "0";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

function ClipboardIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" aria-hidden="true">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}
