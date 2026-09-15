import React, { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { MemberShell } from "./MemberShell";
import { Card, EmptyState, ErrorNote } from "./MemberUi";
import { Sparkline, TrendAreaChart, RankedBars, Donut, Legend } from "../dashboard/v2/charts";
import { AppIcon } from "../../components/AppIcon";
import { DateRangeFilter, DEFAULT_RANGE } from "../dashboard/v2/filters";
import type { DateRange } from "../dashboard/v2/filters";
import { brand, series } from "../dashboard/v2/theme";
import { DashboardSkeleton } from "../dashboard/v2/skeletons";
import { useGetReactDashboardQuery } from "../../store/api/dashboardApi";
import { useGetTimeTrackingQuery } from "../../store/api/timeTrackingApi";
import { formatHMS, formatHoursAsHMS, secondsOf } from "../../utils/duration";

/**
 * The member's own dashboard.
 *
 * It runs against exactly the same `/react/dashboard` endpoint the admin
 * dashboard uses. Nothing here narrows the query to the signed-in member —
 * the backend does that, pinning `member_id` to the caller whenever they lack
 * `time_entries:view_all`. Scoping in the UI would be decoration; scoping in
 * the service is the guarantee.
 *
 * Consequently there is no Top Members card: for a member that list is a
 * single row containing themselves, which tells them nothing.
 */

const parseIso = (iso: string) => {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
};

const isoOf = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

const DAY_MS = 24 * 60 * 60 * 1000;
const TOP_N = 10;

/** The equally long span immediately before the selected one, for the deltas. */
const previousRange = (range: DateRange): { start_date: string; end_date: string } => {
  const from = parseIso(range.from);
  const to = parseIso(range.to);
  const days = Math.round((to.getTime() - from.getTime()) / DAY_MS) + 1;
  const prevTo = new Date(from.getTime() - DAY_MS);
  const prevFrom = new Date(prevTo.getTime() - (days - 1) * DAY_MS);
  return { start_date: isoOf(prevFrom), end_date: isoOf(prevTo) };
};

const longDate = (iso: string) =>
  parseIso(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });

export const MemberDashboard: React.FC = () => {
  const navigate = useNavigate();
  const [range, setRange] = useState<DateRange>(DEFAULT_RANGE);

  const { data, isFetching, isError } = useGetReactDashboardQuery({
    start_date: range.from,
    end_date: range.to,
    top_n: TOP_N,
  });
  const { data: previous } = useGetReactDashboardQuery({ ...previousRange(range), top_n: TOP_N });

  // The day-by-day timesheet strip. `/time-tracking` is self-scoped for a
  // member the same way the dashboard is, so no employee id is sent.
  const { data: timesheet } = useGetTimeTrackingQuery({
    start_date: range.from,
    end_date: range.to,
    limit: 100,
  });

  /**
   * The persisted cache can hand us the previous visit's rows before this
   * page load's request returns, so the skeleton is shown until the first
   * request settles; later range changes dim the current view instead.
   */
  const [firstLoadSettled, setFirstLoadSettled] = useState(false);
  const settledRef = useRef(false);
  useEffect(() => {
    if (!isFetching && !settledRef.current) {
      settledRef.current = true;
      setFirstLoadSettled(true);
    }
  }, [isFetching]);
  const showSkeleton = !firstLoadSettled && !isError;

  const summary = data?.summary;
  const points = useMemo(() => data?.time_tracked.data ?? [], [data]);

  const deltaOf = (current: number | null | undefined, before: number | null | undefined) => {
    if (current === null || current === undefined) return null;
    if (before === null || before === undefined || before === 0) return null;
    return ((current - before) / before) * 100;
  };

  const trendColor = (pct: number) => {
    if (pct > 0) return "text-emerald-500";
    if (pct < 0) return "text-rose-500";
    return "text-slate-500";
  };

  const kpiCard = (
    title: string,
    value: string | number,
    delta: number | null,
    color: string,
    trend?: number[]
  ) => (
    <div className="flex flex-col justify-between rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">{title}</div>
          <div className="mt-1 text-3xl font-extrabold text-[#0F172A]">{value}</div>
        </div>
        {delta === null ? (
          // No comparable previous window is not a 0% change — say nothing.
          <div className="mt-1 text-[13px] font-bold text-slate-400" title="No comparable previous period">
            --
          </div>
        ) : (
          <div className={`mt-1 flex items-center gap-1 text-[13px] font-bold ${trendColor(delta)}`}>
            {delta > 0 ? (
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 10l7-7m0 0l7 7m-7-7v18" />
              </svg>
            ) : delta < 0 ? (
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 14l-7 7m0 0l-7-7m7 7V3" />
              </svg>
            ) : null}
            <span>{`${delta > 0 ? "+" : ""}${delta.toFixed(1)}%`}</span>
          </div>
        )}
      </div>
      {trend && trend.length > 1 ? (
        <div className="mt-6 h-10 w-full opacity-70">
          <Sparkline values={trend} color={color} height={40} />
        </div>
      ) : null}
    </div>
  );

  const trackedSeries = points.map((p) => p.tracked_hours);
  const manualSeries = points.map((p) => p.manual_hours);
  const trendLabels = points.map((p) =>
    parseIso(p.date).toLocaleDateString("en-GB", { day: "2-digit", month: "short" })
  );

  const topProjects = data?.top_projects.items ?? [];
  const topApps = useMemo(() => data?.top_apps.items ?? [], [data]);
  const totalAppHours = data?.top_apps.total_app_hours ?? 0;

  /** Five named arcs plus the rest, so the donut is honest part-to-whole. */
  const appSlices = useMemo(() => {
    const named: { label: string; value: number; color: string }[] = topApps
      .slice(0, 5)
      .map((app, i) => ({
        label: app.app_name,
        value: app.total_hours,
        color: series[i % series.length] as string,
      }));
    const rest = totalAppHours - named.reduce((sum, slice) => sum + slice.value, 0);
    if (rest > 0.01) {
      named.push({ label: "Other apps", value: Math.round(rest * 100) / 100, color: brand.subtle });
    }
    return named;
  }, [topApps, totalAppHours]);

  const activity = summary?.activity ?? null;
  const timesheetRows = timesheet?.items ?? [];

  const reportLink = (reportId: string) =>
    `/member/reports/${reportId}?start=${range.from}&end=${range.to}`;

  const emptyNote = (label: string) => (
    <p className="py-10 text-center text-[13px] text-[#94A3B8]">
      {isFetching
        ? "Loading…"
        : `You tracked no ${label} between ${longDate(range.from)} and ${longDate(range.to)}.`}
    </p>
  );

  return (
    <MemberShell
      title="My Dashboard"
      subtitle={`Your tracked work between ${longDate(range.from)} and ${longDate(range.to)}.`}
    >
      <div className="w-full space-y-6 pb-20">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#E2E8F0] bg-white p-2 pl-4 shadow-sm">
          <DateRangeFilter value={range} onChange={setRange} />
          <button
            onClick={() => setRange(DEFAULT_RANGE)}
            className="rounded-lg border border-[#E2E8F0] px-4 py-2 text-[13px] font-bold text-[#64748B] transition hover:bg-[#F8FAFC] hover:text-[#0F172A]"
          >
            Reset
          </button>
        </div>

        {isError && <ErrorNote message="Your dashboard could not be loaded for this range. Please try again." />}

        {showSkeleton ? (
          <DashboardSkeleton />
        ) : (
          <div
            className={`space-y-6 transition-all duration-300 ${
              isFetching ? "pointer-events-none opacity-60 blur-[2px]" : ""
            }`}
          >
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {kpiCard(
                "My Activity",
                activity === null ? "--" : `${activity.toFixed(1)}%`,
                deltaOf(activity, previous?.summary.activity),
                series[0]
              )}
              {kpiCard(
                "Time Worked",
                formatHMS(secondsOf(summary)),
                deltaOf(summary?.total_hours, previous?.summary.total_hours),
                series[1],
                trackedSeries
              )}
              {kpiCard(
                "Projects Worked",
                summary?.active_projects ?? 0,
                deltaOf(summary?.active_projects, previous?.summary.active_projects),
                series[2]
              )}
              {kpiCard(
                "Tasks Touched",
                summary?.total_tasks ?? 0,
                deltaOf(summary?.total_tasks, previous?.summary.total_tasks),
                series[3]
              )}
            </div>

            <Card
              title={`Time Tracked (${longDate(range.from)} - ${longDate(range.to)})`}
              action={
                <Legend
                  items={[
                    { label: "Tracked Time", color: series[0] },
                    { label: "Manual Time", color: brand.muted },
                  ]}
                />
              }
            >
              <div className="h-64 w-full">
                {points.length > 0 ? (
                  <TrendAreaChart
                    labels={trendLabels}
                    seriesList={[
                      { label: "Tracked Time", values: trackedSeries, color: series[0] },
                      { label: "Manual Time", values: manualSeries, color: brand.muted },
                    ]}
                    unit="h"
                  />
                ) : (
                  <div className="flex h-full items-center justify-center text-[13px] text-[#94A3B8]">
                    {isFetching ? "Loading…" : "No days in the selected range."}
                  </div>
                )}
              </div>
            </Card>

            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <Card
                title="My Timesheet"
                action={
                  <button
                    onClick={() => navigate("/member/time-tracking")}
                    className="text-[11px] font-bold text-[#2563EB] hover:underline"
                  >
                    View All
                  </button>
                }
              >
                {timesheetRows.length === 0 ? (
                  <EmptyState
                    message="No tracked days in this range."
                    hint="Days you track with the desktop app appear here."
                  />
                ) : (
                  <div className="-mx-2 overflow-x-auto">
                    <table className="w-full min-w-[420px] text-left text-[13px]">
                      <thead>
                        <tr className="border-b border-[#E2E8F0] text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">
                          <th className="px-2 py-2.5">Date</th>
                          <th className="px-2 py-2.5">Start</th>
                          <th className="px-2 py-2.5">Stop</th>
                          <th className="px-2 py-2.5 text-right">Duration</th>
                        </tr>
                      </thead>
                      <tbody>
                        {timesheetRows.slice(0, 8).map((row) => (
                          <tr key={row.date} className="border-b border-[#F1F5F9] last:border-0">
                            <td className="px-2 py-2.5 font-semibold text-[#0F172A]">{longDate(row.date)}</td>
                            <td className="px-2 py-2.5 text-[#64748B]">
                              {row.start_time
                                ? new Date(row.start_time).toLocaleTimeString("en-GB", {
                                    timeZone: "Asia/Kolkata",
                                    hour: "2-digit",
                                    minute: "2-digit",
                                  })
                                : "—"}
                            </td>
                            <td className="px-2 py-2.5 text-[#64748B]">
                              {row.end_time
                                ? new Date(row.end_time).toLocaleTimeString("en-GB", {
                                    timeZone: "Asia/Kolkata",
                                    hour: "2-digit",
                                    minute: "2-digit",
                                  })
                                : "—"}
                            </td>
                            <td className="px-2 py-2.5 text-right font-mono font-bold text-[#0F172A]">
                              {row.total_time || formatHMS(row.total_seconds)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>

              <Card
                title="My Project Activity"
                action={
                  <button
                    onClick={() => navigate(reportLink("projects"))}
                    className="text-[11px] font-bold text-[#2563EB] hover:underline"
                  >
                    View Report
                  </button>
                }
              >
                {topProjects.length === 0 ? (
                  emptyNote("project time")
                ) : (
                  <RankedBars
                    items={topProjects.map((p) => ({
                      id: String(p.project_id),
                      name: p.project_name,
                      value: p.total_hours,
                      meta: p.avg_activity === null ? "No activity samples" : `${p.avg_activity.toFixed(0)}% avg`,
                      secondary: p.avg_activity ?? undefined,
                    }))}
                    color={series[2]}
                    formatValue={(n) => formatHoursAsHMS(n)}
                  />
                )}
              </Card>
            </div>

            <Card
              title="My Apps & URLs"
              action={
                <button
                  onClick={() => navigate(reportLink("apps"))}
                  className="text-[11px] font-bold text-[#2563EB] hover:underline"
                >
                  View Report
                </button>
              }
            >
              {topApps.length === 0 ? (
                emptyNote("application usage")
              ) : (
                <div className="flex flex-col items-center gap-8 py-2 lg:flex-row lg:items-start lg:justify-center">
                  <Donut
                    size={200}
                    slices={appSlices}
                    centerLabel="Total App Time"
                    centerValue={formatHoursAsHMS(totalAppHours)}
                  />
                  <div className="w-full max-w-md space-y-2">
                    {topApps.slice(0, 8).map((app, i) => (
                      <div key={app.app_id} className="flex items-center gap-3 text-[13px]">
                        <span
                          className="h-2.5 w-2.5 shrink-0 rounded-full"
                          style={{ backgroundColor: series[i % series.length] }}
                        />
                        <AppIcon name={app.app_name} size={20} />
                        <span className="min-w-0 flex-1 truncate font-semibold text-[#0F172A]">{app.app_name}</span>
                        <span className="shrink-0 font-mono text-[#64748B]">
                          {formatHoursAsHMS(app.total_hours)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </Card>
          </div>
        )}
      </div>
    </MemberShell>
  );
};
