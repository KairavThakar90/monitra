import React, { useEffect, useRef } from "react";
import { useSelector } from "react-redux";
import { describeQueryError } from "../api/queryError";
import type { QueryErrorKind } from "../api/queryError";
import { store } from "../store";
import { retryFailedQueries } from "../store/retryFailed";

/**
 * The page-independent answer to "something did not load".
 *
 * Most screens read their data through RTK Query and many of them never looked
 * at `isError`: a failed load drew an empty table, a row of zeros or "no
 * projects", and nothing said anything had gone wrong. This watches the one
 * place every failure lands -- the API cache -- so that a failed read is
 * announced and retryable wherever it happens, without each page having to
 * remember to.
 *
 * It also retries, quietly and boundedly: a transient failure (no answer, a
 * timeout, a 5xx) is re-sent at 5 s, 15 s and 45 s after it first appears, then
 * left to the user's Retry button and to the focus/reconnect/poll refetches
 * the app already has. Three attempts per episode; an episode ends when
 * nothing is failing. A 401/403 or any other 4xx is never retried by it.
 */
export const AUTO_RETRY_DELAYS_MS = [5_000, 15_000, 45_000] as const;

interface FailedQuery { status?: string; error?: unknown }

/**
 * "<failed>|<worst kind>|<pending>" -- a string, so the selector is stable
 * between renders. `pending` matters: while a retry is in flight the failed
 * queries are momentarily not failed, and that must not look like the end of
 * the episode (it would reset the attempt count and retry for ever).
 */
export const summariseFailures = (state: {
  api?: {
    queries?: Record<string, FailedQuery | undefined>;
    subscriptions?: Record<string, Record<string, unknown> | undefined>;
  };
}): string => {
  let count = 0;
  let pending = 0;
  let transient = false;
  let auth = false;
  const subscriptions = state.api?.subscriptions ?? {};
  for (const [cacheKey, entry] of Object.entries(state.api?.queries ?? {})) {
    // Only what a mounted screen is showing. A failed query the user has since
    // navigated away from stays in the cache for minutes; it is not a
    // problem with anything on screen, and re-sending it would be waste.
    if (Object.keys(subscriptions[cacheKey] ?? {}).length === 0) continue;
    if (entry?.status === "pending") pending += 1;
    if (!entry || entry.status !== "rejected") continue;
    const info = describeQueryError(entry.error);
    // An ended session is announced by the sign-in flow, not by this.
    if (info.kind === "auth") { auth = true; continue; }
    count += 1;
    if (info.transient) transient = true;
  }
  const kind: QueryErrorKind | "none" = count === 0 ? (auth ? "auth" : "none") : transient ? "network" : "client";
  return `${count}|${kind}|${pending}`;
};

export const LoadFailureNotice: React.FC<{ enabled?: boolean }> = ({ enabled = true }) => {
  const summary = useSelector(summariseFailures);
  const [countText, kind, pendingText] = summary.split("|");
  const count = Number(countText);
  const pending = Number(pendingText);
  const attempt = useRef(0);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    const clear = () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = null;
    };
    if (!enabled || (count === 0 && pending === 0)) {
      attempt.current = 0;      // nothing failed and nothing is being retried: the episode is over
      clear();
      return clear;
    }
    if (count === 0) return clear;  // a retry is in flight; wait for its outcome
    if (kind !== "network" || timer.current !== null || attempt.current >= AUTO_RETRY_DELAYS_MS.length) {
      return clear;
    }
    timer.current = window.setTimeout(() => {
      timer.current = null;
      attempt.current += 1;
      retryFailedQueries(store.dispatch, store.getState);
    }, AUTO_RETRY_DELAYS_MS[attempt.current]);
    return clear;
    // `summary` changes when a retry fails again, which re-arms the next delay.
  }, [enabled, count, kind, pending, summary]);

  if (!enabled || count === 0) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="load-failure-notice"
      className="fixed bottom-4 left-1/2 z-50 flex max-w-[92vw] -translate-x-1/2 items-center gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-[13px] font-semibold text-amber-800 shadow-lg"
    >
      <span>
        {kind === "network"
          ? "Connection temporarily unavailable — some information could not be refreshed."
          : "Some information could not be loaded."}
      </span>
      <button
        type="button"
        onClick={() => {
          attempt.current = 0;
          retryFailedQueries(store.dispatch, store.getState);
        }}
        className="rounded-lg border border-amber-300 bg-white px-3 py-1 text-[12px] font-bold text-amber-800 transition hover:bg-amber-100"
      >
        Retry
      </button>
    </div>
  );
};
