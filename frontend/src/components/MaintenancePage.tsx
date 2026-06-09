"use client";

import { motion } from "framer-motion";

// Shown instead of the chat when the backend reports query_enabled=false
// (currently: the OpenRouter balance dropped below the maintenance
// threshold). There is no textarea here, so query input is fully blocked,
// and the situation is explained in plain language.
export function MaintenancePage({ reason }: { reason?: string | null }) {
  return (
    <div className="min-h-screen flex items-center justify-center px-6 bg-gradient-to-b from-white to-zinc-50 dark:from-zinc-950 dark:to-black">
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3 }}
        className="w-full max-w-md text-center rounded-2xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 shadow-sm p-10 flex flex-col items-center gap-5"
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src="/favicon.svg"
          alt="PloverAI logo"
          width={56}
          height={56}
          className="h-14 w-14 dark:invert"
        />

        <span className="inline-flex items-center gap-2 text-xs font-medium px-2.5 py-1 rounded-full bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300">
          <span className="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
          Maintenance
        </span>

        <div className="flex flex-col gap-2">
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
            PloverAI is undergoing maintenance
          </h1>
          <p className="text-zinc-600 dark:text-zinc-400 leading-relaxed">
            {reason ||
              "The service is temporarily unavailable while we perform maintenance, so queries are paused for now."}{" "}
            Please check back shortly.
          </p>
        </div>

        <p className="text-xs text-zinc-400">Thanks for your patience.</p>
      </motion.div>
    </div>
  );
}
