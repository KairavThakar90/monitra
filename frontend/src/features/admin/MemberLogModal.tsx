import React, { useEffect } from "react";
import { useGetActivityLogsQuery } from "../../store/api/activityLogsApi";
import { formatISTDate, formatISTTime12, istTodayISO } from "../../utils/duration";
import { entryActionLabel, moduleStyle, sourceLabel } from "./activityLogFormat";

/**
 * One member's activity for the current IST day, in a modal.
 *
 * Opened from the Members page's "View Log" button. It reads the same
 * `GET /activity-logs` the Logs page does, narrowed to one member and one day,
 * so what it shows -- and who may open it -- is decided by the backend. For
 * each recorded action: the time, what was done, the IP address it came from,
 * and the member's user id.
 */
export const MemberLogModal: React.FC<{
  memberId: number;
  memberName: string;
  onClose: () => void;
}> = ({ memberId, memberName, onClose }) => {
  const today = istTodayISO();
  const { data, isLoading, isError } = useGetActivityLogsQuery(
    { start: today, end: today, memberId },
    { refetchOnMountOrArgChange: true },
  );
  const entries = data?.members.find((group) => group.user_id === memberId)?.entries ?? [];

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-slate-900/40 backdrop-blur-sm" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Activity log for ${memberName}`}
        className="relative flex max-h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl bg-white shadow-2xl"
      >
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-6 py-4">
          <div className="min-w-0">
            <h2 className="truncate text-lg font-bold text-slate-800">{memberName} — Activity log</h2>
            <p className="mt-0.5 text-xs font-medium text-slate-500">
              Today, {formatISTDate(`${today}T12:00:00Z`)} (IST)
              {!isLoading && !isError && ` · ${entries.length} action${entries.length === 1 ? "" : "s"}`}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-md p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
          >
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeWidth="2.5" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="overflow-y-auto">
          {isLoading ? (
            <p className="py-12 text-center text-sm font-semibold text-slate-500">Loading log…</p>
          ) : isError ? (
            <p className="py-12 text-center text-sm font-semibold text-rose-600">
              The log could not be loaded. Please try again.
            </p>
          ) : entries.length === 0 ? (
            <p className="py-12 text-center text-sm font-semibold text-slate-500">
              No activity recorded for {memberName} today.
            </p>
          ) : (
            <table className="w-full text-left text-sm">
              <thead className="sticky top-0 bg-slate-50 text-slate-500">
                <tr>
                  <th className="px-6 py-3 text-[11px] font-bold uppercase tracking-wider">Time</th>
                  <th className="px-6 py-3 text-[11px] font-bold uppercase tracking-wider">Track</th>
                  <th className="px-6 py-3 text-[11px] font-bold uppercase tracking-wider">IP</th>
                  <th className="px-6 py-3 text-[11px] font-bold uppercase tracking-wider">User ID</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {entries.map((entry) => {
                  const style = moduleStyle(entry.module);
                  const source = sourceLabel(entry);
                  return (
                    <tr key={entry.id}>
                      <td className="whitespace-nowrap px-6 py-3 font-bold tabular-nums text-slate-700">
                        {formatISTTime12(entry.created_at)}
                      </td>
                      <td className="px-6 py-3">
                        <div className="flex flex-wrap items-center gap-2">
                          <span
                            className="rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide"
                            style={{ color: style.text, background: style.bg }}
                          >
                            {style.label}
                          </span>
                          <span className="font-bold text-slate-800">{entryActionLabel(entry)}</span>
                          {source && <span className="text-[11px] font-semibold text-slate-400">{source}</span>}
                        </div>
                        {entry.description && <div className="mt-0.5 text-xs text-slate-500">{entry.description}</div>}
                      </td>
                      <td className="whitespace-nowrap px-6 py-3 font-mono text-xs text-slate-600">
                        {entry.ip_address ?? "—"}
                      </td>
                      <td className="whitespace-nowrap px-6 py-3 font-semibold text-slate-600">{memberId}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
};
