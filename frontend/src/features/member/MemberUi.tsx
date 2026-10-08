import React from "react";
import { store } from "../../store";
import { retryFailedQueries } from "../../store/retryFailed";

/**
 * The handful of presentational pieces every member page repeats.
 *
 * `EmptyState` matters more than it looks: the member pages read real,
 * per-person data, and a member who tracked nothing yesterday must see that
 * they tracked nothing — never a filler row or a demo value.
 */

export const Card: React.FC<{
  title?: string;
  action?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}> = ({ title, action, className = "", children }) => (
  <div className={`rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm ${className}`}>
    {(title || action) && (
      <div className="mb-4 flex items-center justify-between gap-3">
        {title && (
          <h3 className="text-[13px] font-bold uppercase tracking-wider text-[#64748B]">{title}</h3>
        )}
        {action}
      </div>
    )}
    {children}
  </div>
);

export const EmptyState: React.FC<{ message: string; hint?: string }> = ({ message, hint }) => (
  <div className="flex flex-col items-center justify-center gap-1 px-4 py-12 text-center">
    <svg className="mb-2 h-8 w-8 text-[#CBD5E1]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.5"
        d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"
      />
    </svg>
    <p className="text-[13px] font-semibold text-[#64748B]">{message}</p>
    {hint && <p className="max-w-md text-xs text-[#94A3B8]">{hint}</p>}
  </div>
);

export const Spinner: React.FC<{ label?: string }> = ({ label = "Loading…" }) => (
  <div className="flex items-center justify-center gap-2.5 py-12 text-[13px] font-semibold text-[#64748B]">
    <span className="h-4 w-4 animate-spin rounded-full border-2 border-[#E2E8F0] border-t-[#2563EB]" />
    {label}
  </div>
);

/**
 * A failed load. Its "Retry" re-sends every query that is currently in the
 * failed state (`retryFailedQueries`) -- one shot, never a loop -- so the many
 * screens that show this need no wiring of their own to offer a way out.
 * Pass `onRetry={null}` where a retry makes no sense (a missing record).
 */
export const ErrorNote: React.FC<{ message: string; onRetry?: (() => void) | null }> = ({ message, onRetry }) => (
  <div
    role="alert"
    className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-rose-200 bg-rose-50 p-4 text-[13px] font-semibold text-rose-700"
  >
    <span>{message}</span>
    {onRetry !== null && (
      <button
        type="button"
        onClick={onRetry ?? (() => void retryFailedQueries(store.dispatch, store.getState))}
        className="rounded-lg border border-rose-300 bg-white px-3 py-1.5 text-[12px] font-bold text-rose-700 transition hover:bg-rose-100"
      >
        Retry
      </button>
    )}
  </div>
);

/** A status badge tinted with the colour the backend stores for that status. */
export const StatusPill: React.FC<{ status?: { name: string; color?: string } | null }> = ({ status }) => {
  if (!status) return <span className="text-xs text-[#94A3B8]">—</span>;
  const color = status.color || "#64748B";
  return (
    <span
      className="inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-bold"
      style={{ backgroundColor: `${color}1A`, color }}
    >
      {status.name}
    </span>
  );
};

export const initialsOf = (name: string) => {
  if (!name) return "?";
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
};

export const Avatar: React.FC<{ name: string; color?: string; size?: number; ring?: boolean }> = ({
  name,
  color = "#2563EB",
  size = 36,
  ring = false,
}) => (
  <div
    className={`flex shrink-0 items-center justify-center rounded-full font-bold text-white ${
      ring ? "ring-2 ring-white" : ""
    }`}
    style={{ width: size, height: size, backgroundColor: color, fontSize: size * 0.36 }}
    title={name}
  >
    {initialsOf(name)}
  </div>
);

export const ProgressBar: React.FC<{ value: number; color?: string }> = ({ value, color = "#2563EB" }) => (
  <div className="h-1.5 w-full overflow-hidden rounded-full bg-[#F1F5F9]">
    <div
      className="h-full rounded-full transition-all duration-500"
      style={{ width: `${Math.min(100, Math.max(0, value))}%`, backgroundColor: color }}
    />
  </div>
);
