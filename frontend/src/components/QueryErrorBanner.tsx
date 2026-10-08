import React from "react";
import { describeQueryError } from "../api/queryError";

/**
 * The one failed-load notice every data screen should show.
 *
 * It says what kind of failure it was (a session that has ended is not a bad
 * connection), always offers the way out for a failure that can pass, and --
 * when the screen is still showing the last data it loaded -- says so, so
 * stale numbers are never mistaken for fresh ones.
 *
 * `onRetry` is the query's own `refetch` where the screen has one.
 */
export const QueryErrorBanner: React.FC<{
  error: unknown;
  /** What failed, as the page names it: "Your dashboard", "The report". */
  what: string;
  onRetry?: () => void;
  /** True while a retry is in flight. */
  retrying?: boolean;
  /** True when the screen is still showing data from before the failure. */
  showingLastData?: boolean;
}> = ({ error, what, onRetry, retrying = false, showingLastData = false }) => {
  const info = describeQueryError(error);
  const detail =
    info.kind === "auth"
      ? info.message
      : showingLastData
        ? `${info.message} Showing the last data that loaded.`
        : `${info.message} ${what} could not be loaded.`;
  return (
    <div
      role="alert"
      data-error-kind={info.kind}
      className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-rose-200 bg-rose-50 p-4 text-[13px] font-semibold text-rose-700"
    >
      <span>{detail}</span>
      {info.transient && onRetry && (
        <button
          type="button"
          onClick={onRetry}
          disabled={retrying}
          className="rounded-lg border border-rose-300 bg-white px-3 py-1.5 text-[12px] font-bold text-rose-700 transition hover:bg-rose-100 disabled:opacity-60"
        >
          {retrying ? "Retrying…" : "Retry"}
        </button>
      )}
    </div>
  );
};
