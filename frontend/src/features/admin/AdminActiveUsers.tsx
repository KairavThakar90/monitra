import React from "react";
import { V2Shell } from "../dashboard/v2/V2Shell";
import { Avatar, Card, EmptyState, ErrorNote, Spinner } from "../member/MemberUi";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { useGetActiveTimeTrackingQuery, type ActiveTimeTrackingItem } from "../../store/api/timeTrackingApi";
import { useOnResume } from "../../hooks/useResume";
import { formatISTDate, formatISTTime12 } from "../../utils/duration";

/**
 * Active Users: who has a timer running right now, and on what.
 *
 * Each row is one running `time_entries` row, so a member appears here from the
 * moment their timer starts and disappears the moment it stops. Nothing on this
 * page is a client counter: `Running for` is the server's own figure (net of the
 * entry's adjustments, per docs/TIMING_MODEL.md section 3) as of the moment it
 * answered, and the page re-asks every `ACTIVE_USERS_POLL_MS` instead of
 * ticking a second clock of its own. `Activity` is likewise the server's figure:
 * the member's duration-weighted activity today, or a dash when none has been
 * measured yet (never a made-up 0%).
 *
 * The backend scopes the list to what the caller may see: the organization, a
 * leader's team, or just the caller.
 */

/** How often the list is re-read while the tab is in front. */
const ACTIVE_USERS_POLL_MS = 30_000;

/**
 * A snapshot older than this is no longer "right now". While one is being
 * replaced -- the first thing that happens when a machine wakes -- its figures
 * are dimmed rather than presented as current.
 */
const STALE_AFTER_MS = 45_000;

/** "2:35 pm", or "30 Sep 2026, 11:10 pm" when the entry started on an earlier IST day. */
const startedLabel = (startIso: string, serverIso: string | undefined) => {
  const time = formatISTTime12(startIso);
  if (!serverIso) return time;
  const startDay = formatISTDate(startIso);
  return startDay && startDay !== formatISTDate(serverIso) ? `${startDay}, ${time}` : time;
};

type SortOrder = "asc" | "desc";

/**
 * Orders rows by the instant their timer started. `start_time` is compared as a
 * parsed instant, not as text, so differing ISO spellings of the same moment
 * ("Z" vs "+00:00") cannot reorder rows. Ties fall back to the entry id, in the
 * same direction, which is also the order the backend itself uses.
 */
const sortByStarted = (items: ActiveTimeTrackingItem[], order: SortOrder) => {
  const direction = order === "asc" ? 1 : -1;
  return [...items].sort(
    (a, b) =>
      direction * (Date.parse(a.start_time) - Date.parse(b.start_time) || a.time_entry_id - b.time_entry_id),
  );
};

const SortArrow: React.FC<{ order: SortOrder }> = ({ order }) => (
  <svg
    aria-hidden="true"
    viewBox="0 0 12 12"
    className="h-3 w-3 shrink-0 text-[#0F172A]"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    {order === "asc" ? <path d="M6 10V2M2.5 5.5 6 2l3.5 3.5" /> : <path d="M6 2v8M2.5 6.5 6 10l3.5-3.5" />}
  </svg>
);

export const AdminActiveUsers: React.FC = () => {
  const { data, isLoading, isFetching, isError, refetch, fulfilledTimeStamp } = useGetActiveTimeTrackingQuery(undefined, {
    pollingInterval: ACTIVE_USERS_POLL_MS,
    // A hidden tab has nobody looking at it; focusing it refetches straight away.
    skipPollingIfUnfocused: true,
  });

  // A machine that slept and woke with this tab still in front fires neither
  // `focus` nor `visibilitychange`, so nothing else would re-read the list
  // before the next poll -- and the figures shown are a server snapshot that
  // keeps its "running for" however long the machine was asleep.
  useOnResume(() => {
    void refetch();
  });

  // The snapshot's age is on this browser's clock (when the answer arrived),
  // never compared with the server's: the two clocks are only ever subtracted
  // from themselves (docs/TIMING_MODEL.md section 2).
  const snapshotAgeMs = fulfilledTimeStamp ? Date.now() - fulfilledTimeStamp : 0;
  const refreshingStale = isFetching && !isLoading && snapshotAgeMs > STALE_AFTER_MS;
  // A failed refresh keeps the previous rows on screen; it must say they are old.
  const refreshFailed = isError && !!data;

  // Oldest timer first is how the backend already returns the list, so that is
  // the order the page opens in; the header flips it.
  const [startedOrder, setStartedOrder] = React.useState<SortOrder>("asc");
  const items = React.useMemo(() => sortByStarted(data?.items ?? [], startedOrder), [data?.items, startedOrder]);

  return (
    <V2Shell title="Active Users" subtitle="Members with a timer running right now">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
        <Card
          title="Working now"
          action={
            <div className="flex items-center gap-3">
              <InlineRefreshIndicator active={isFetching && !isLoading} label="Updating" />
              {data && (
                <span className="text-[11px] text-[#94A3B8]" data-testid="active-users-updated">
                  Updated {formatISTTime12(data.server_time)}
                </span>
              )}
            </div>
          }
        >
          {/* Above the conditional, so an empty list that failed to refresh says so too. */}
          {refreshFailed && (
            <div
              role="status"
              data-testid="active-users-refresh-failed"
              className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] font-semibold text-amber-800"
            >
              Could not refresh. This is the list as of {formatISTTime12(data!.server_time)} and may be out of date.
            </div>
          )}
          {isLoading ? (
            <Spinner label="Loading active users…" />
          ) : isError && !data ? (
            <ErrorNote message="Could not load active users. Try again in a moment." />
          ) : items.length === 0 ? (
            <EmptyState
              message="No one is tracking time right now."
              hint="Members appear here as soon as they start a timer, and leave when they stop it."
            />
          ) : (
            <div className="flex flex-col gap-4" aria-busy={refreshingStale || undefined}>
              <div className="flex items-center gap-2.5">
                <span aria-hidden="true" className="relative flex h-2.5 w-2.5">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
                  <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-emerald-500" />
                </span>
                <span className="text-[13px] font-bold text-[#0F172A]" data-testid="active-users-count">
                  {data?.total ?? items.length} {(data?.total ?? items.length) === 1 ? "member" : "members"} active
                </span>
              </div>

              <div
                data-testid="active-users-table"
                className={`overflow-x-auto transition-opacity duration-200 ${refreshingStale ? "opacity-50" : ""}`}
                data-stale={refreshingStale ? "true" : "false"}
              >
                <table className="w-full min-w-[720px] text-left text-[13px]">
                  <thead>
                    <tr className="border-b border-[#E2E8F0] text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">
                      <th className="px-3 py-2.5">Member</th>
                      <th className="px-3 py-2.5">Task</th>
                      <th className="px-3 py-2.5">Project</th>
                      <th
                        className="px-3 py-2.5"
                        aria-sort={startedOrder === "asc" ? "ascending" : "descending"}
                      >
                        <button
                          type="button"
                          data-testid="sort-started"
                          onClick={() => setStartedOrder((order) => (order === "asc" ? "desc" : "asc"))}
                          title={startedOrder === "asc" ? "Oldest first. Click for newest first" : "Newest first. Click for oldest first"}
                          className="inline-flex items-center gap-1 rounded uppercase tracking-wider hover:text-[#0F172A] focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
                        >
                          Started
                          <SortArrow order={startedOrder} />
                        </button>
                      </th>
                      <th className="px-3 py-2.5 text-right" title="Today's activity, weighted by time measured">
                        Activity
                      </th>
                      <th className="px-3 py-2.5 text-right">Running for</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((item) => (
                      <tr
                        key={item.time_entry_id}
                        data-testid="active-user-row"
                        className="border-b border-[#F1F5F9] last:border-0 hover:bg-slate-50/60"
                      >
                        <td className="px-3 py-3">
                          <div className="flex min-w-0 items-center gap-3">
                            <Avatar name={item.name} size={34} />
                            <div className="min-w-0">
                              <div className="truncate font-semibold text-[#0F172A]">{item.name}</div>
                              {item.designation && (
                                <div className="truncate text-[11px] text-[#64748B]">{item.designation}</div>
                              )}
                            </div>
                          </div>
                        </td>
                        <td className="max-w-[260px] px-3 py-3">
                          <div className="truncate font-semibold text-[#0F172A]" title={item.task_name}>
                            {item.task_name}
                          </div>
                        </td>
                        <td className="max-w-[220px] px-3 py-3">
                          <div className="truncate text-[#64748B]" title={item.project_name}>
                            {item.project_name}
                          </div>
                        </td>
                        <td className="whitespace-nowrap px-3 py-3 text-[#64748B]">
                          {startedLabel(item.start_time, data?.server_time)}
                        </td>
                        <td
                          className="whitespace-nowrap px-3 py-3 text-right font-semibold text-[#0F172A]"
                          data-testid="active-user-activity"
                        >
                          {item.activity_percentage == null ? (
                            <span className="text-[#94A3B8]" title="No activity measured yet today">
                              —
                            </span>
                          ) : (
                            `${item.activity_percentage}%`
                          )}
                        </td>
                        <td className="whitespace-nowrap px-3 py-3 text-right font-mono font-semibold text-[#0F172A]">
                          {item.elapsed_time}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </Card>
      </div>
    </V2Shell>
  );
};
