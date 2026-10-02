import React, { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { V2Shell } from "./V2Shell";
import { Sparkline, TrendAreaChart, RankedBars, Donut, Legend, FloatingCard, useHoverAnchor } from "./charts";
import { AppIcon } from "../../../components/AppIcon";
import { DateRangeFilter, DEFAULT_RANGE, ProjectMultiSelect } from "./filters";
import { useGetAllProjectsQuery } from "../../../store/api/projectsApi";
import type { DateRange } from "./filters";
import { brand, series } from "./theme";
import { useGetReactDashboardQuery } from "../../../store/api/dashboardApi";
import type { ReactDashboardProjectBilling } from "../../../store/api/dashboardApi";
import { formatHMS, formatHoursAsHMS, secondsOf } from "../../../utils/duration";
import { DashboardSkeleton } from "./skeletons";

/** `YYYY-MM-DD` -> local Date, without the UTC shift `new Date(iso)` applies. */
const parseIso = (iso: string) => {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
};

const isoOf = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

const DAY_MS = 24 * 60 * 60 * 1000;

/** How many rows the server ranks into each "top" list. */
const TOP_N = 10;

/**
 * The equally long span immediately before the selected one. The KPI deltas are
 * this period measured against that one — both are real queries, so a card
 * never shows a change nobody tracked.
 */
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

/** The Top Projects card's filter tabs. */
type ProjectFilterTab = "top" | "billable" | "internal";

/**
 * Budget-usage color for a Billable project's progress bar, against the
 * project's own fixed_hours -- not a generic 0-100 gauge. Under 80% is in
 * progress, 80-99% is closing in, exactly 100% landed on budget, and above
 * 100% is over budget. Bands, not a gradient: a project is either in one
 * state or another, never "a bit of both". Banded on the same rounded value
 * the row prints, so the label and its color always agree.
 */
const usageColor = (pct: number): string => {
  const shown = Math.round(pct);
  if (shown > 100) return "#EF4444"; // red-500 -- over budget
  if (shown === 100) return "#10B981"; // emerald-500 -- on budget
  if (shown >= 80) return "#EAB308"; // yellow-500 -- closing in
  return "#3B82F6"; // blue-500 -- in progress
};

const HoverStat: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="flex items-center justify-between gap-4 py-0.5">
    <span className="text-[11px] text-slate-500">{label}</span>
    <span className="text-[11px] font-semibold text-slate-800">{value}</span>
  </div>
);

/** One Billable row: name, usage %, a color-banded progress bar, and a hover
 * card with the hour/activity breakdown behind that percentage. */
const BillableProjectRow: React.FC<{ project: ReactDashboardProjectBilling }> = ({ project }) => {
  const fixedHours = project.fixed_hours ?? 0;
  const fixedSeconds = Math.round(fixedHours * 3600);
  // Server-computed with the one definition Project Management and client
  // Billing use: fixed hours minus Used, internal time not counted.
  const remainingSeconds = project.remaining_seconds ?? fixedSeconds - project.completed_seconds;
  const pct = project.usage_percentage ?? 0;
  const color = usageColor(pct);
  const barWidth = Math.min(Math.max(pct, 0), 100);

  const { rect, bind } = useHoverAnchor();

  return (
    <li className="relative rounded-lg px-3 py-2.5 transition hover:bg-slate-50/60" {...bind}>
      <div className="flex items-center justify-between gap-3">
        <span className="truncate text-[13px] font-medium text-slate-700">{project.project_name}</span>
        <span className="shrink-0 text-[12px] font-bold" style={{ color }}>
          {pct.toFixed(0)}%
        </span>
      </div>
      {/* The whole track carries the band color as a light tint, so a project's
          state reads at a glance even at 0%; the solid fill is still the true
          usage and never grows past what was actually used. */}
      <div
        className="mt-2 h-1.5 w-full overflow-hidden rounded-full"
        style={{ backgroundColor: `${color}33` }}
      >
        <div
          className="h-full rounded-full transition-all duration-500 ease-out"
          style={{ width: `${barWidth}%`, backgroundColor: color }}
        />
      </div>

      {/* Hover detail card -- the rich-card language the trend chart's own
          tooltip uses, not the compact one-liner RankedBars uses, since this
          one carries several figures rather than one. */}
      <FloatingCard rect={rect} className="w-60 rounded-xl border border-[#E2E8F0] bg-white px-3 py-2.5 shadow-lg">
        <div className="mb-1.5 truncate text-[12px] font-bold text-slate-800">{project.project_name}</div>
        <HoverStat label="Used Hours" value={formatHMS(project.completed_seconds)} />
        <HoverStat label="Internal Hours" value={formatHMS(project.internal_seconds ?? 0)} />
        <HoverStat
          label={remainingSeconds >= 0 ? "Remaining Hours" : "Over Budget By"}
          value={formatHMS(Math.abs(remainingSeconds))}
        />
        <HoverStat label="Fixed Hours" value={formatHoursAsHMS(fixedHours)} />
        <HoverStat label="Usage" value={`${pct.toFixed(1)}%`} />
        {project.avg_activity !== null && (
          <HoverStat label="Activity" value={`${project.avg_activity.toFixed(0)}%`} />
        )}
      </FloatingCard>
    </li>
  );
};

/** One Free Time / Internal row: name and this range's tracked hours -- a
 * free project has no fixed_hours budget, so there is no percentage to color. */
const InternalProjectRow: React.FC<{ project: ReactDashboardProjectBilling }> = ({ project }) => {
  const { rect, bind } = useHoverAnchor();
  return (
  <li className="relative flex items-center justify-between gap-3 rounded-lg px-3 py-2.5 transition hover:bg-slate-50/60" {...bind}>
    <div className="flex flex-col truncate pr-4">
      <span className="truncate text-[13px] font-medium text-slate-700">{project.project_name}</span>
      <span className="truncate text-[11px] text-slate-500">
        {project.avg_activity === null ? "No activity samples" : `${project.avg_activity.toFixed(0)}% active`}
      </span>
    </div>
    <span className="shrink-0 text-[13px] font-semibold text-slate-800">{formatHMS(project.tracked_seconds)}</span>

    <FloatingCard rect={rect} className="w-56 rounded-xl border border-[#E2E8F0] bg-white px-3 py-2.5 shadow-lg">
      <div className="mb-1.5 truncate text-[12px] font-bold text-slate-800">{project.project_name}</div>
      <HoverStat label="Completed Hours" value={formatHMS(project.completed_seconds)} />
      <HoverStat label="Time Tracked" value={formatHMS(project.tracked_seconds)} />
      {project.avg_activity !== null && (
        <HoverStat label="Activity" value={`${project.avg_activity.toFixed(0)}%`} />
      )}
    </FloatingCard>
  </li>
  );
};

export const DashboardV2: React.FC = () => {
  const navigate = useNavigate();
  const [range, setRange] = useState<DateRange>(DEFAULT_RANGE);
  const [projectTab, setProjectTab] = useState<ProjectFilterTab>("top");
  // The Top Apps arc currently highlighted, from the arc or from its legend row.
  const [activeApp, setActiveApp] = useState<string | null>(null);
  /** Empty means every project -- the convention every other filter uses. */
  const [selectedProjects, setSelectedProjects] = useState<string[]>([]);
  const { data: allProjects } = useGetAllProjectsQuery();
  const projectIds = selectedProjects.length ? selectedProjects.map(Number) : undefined;

  const { data, isFetching, isError } = useGetReactDashboardQuery({
    start_date: range.from,
    end_date: range.to,
    project_id: projectIds,
    top_n: TOP_N,
  });
  // Same endpoint, previous window and same projects -- only used for the delta badges.
  const { data: previous } = useGetReactDashboardQuery({
    ...previousRange(range),
    project_id: projectIds,
    top_n: TOP_N,
  });

  /**
   * The cache is restored from the previous visit, so after a refresh RTK Query
   * can hand us last visit's rows before the new request comes back. Those rows
   * are not this page load's answer, so the first request of a page load always
   * shows the skeleton; every later range change keeps the current view on
   * screen and only dims it.
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

  /** Percent change against the previous window; null when there is no basis. */
  const deltaOf = (current: number | null | undefined, before: number | null | undefined) => {
    if (current === null || current === undefined) return null;
    if (before === null || before === undefined || before === 0) return null;
    return ((current - before) / before) * 100;
  };

  const formatDelta = (pct: number) => `${pct > 0 ? "+" : ""}${pct.toFixed(1)}%`;

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
          // No comparable previous window (or nothing tracked in it) is not a
          // 0% change — say nothing rather than imply one.
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
            <span>{formatDelta(delta)}</span>
          </div>
        )}
      </div>
      {/* Only the cards with a real daily series get the trend strip. The band
          used to be reserved on every card, leaving three of the four with an
          unexplained empty block under the number. */}
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
  // Most-at-risk first: a project already over budget, or closing in on it,
  // is what the Billable tab exists to surface -- not whichever project
  // happens to be newest, which is the list's underlying server order.
  const billableProjects = useMemo(
    () => [...(data?.billable_projects ?? [])].sort(
      (a, b) => (b.usage_percentage ?? 0) - (a.usage_percentage ?? 0)
    ),
    [data]
  );
  const internalProjects = useMemo(
    () => [...(data?.internal_projects ?? [])].sort((a, b) => b.tracked_hours - a.tracked_hours),
    [data]
  );
  const totalAppHours = data?.top_apps.total_app_hours ?? 0;

  /**
   * Five named arcs plus whatever the remaining apps add up to. `total_app_hours`
   * is the whole population, not just the ranked page, so the donut is only
   * honest about being a part-to-whole once the rest is drawn as "Other".
   */
  const appSlices = useMemo(() => {
    const named: { label: string; value: number; color: string }[] = topApps
      .slice(0, 5)
      .map((app, i) => ({
        label: app.app_name,
        value: app.total_hours,
        color: series[i % series.length],
      }));
    const rest = totalAppHours - named.reduce((sum, slice) => sum + slice.value, 0);
    // Sub-minute leftovers are rounding noise in the server's 2dp hours.
    if (rest > 0.01) {
      named.push({ label: "Other apps", value: Math.round(rest * 100) / 100, color: brand.subtle });
    }
    return named;
  }, [topApps, totalAppHours]);

  const activity = summary?.activity ?? null;

  // The report pages read the same range off the query string, so following a
  // "View All" link keeps whatever the user picked here.
  const reportLink = (reportId: string) =>
    `/dashboard/reports/${reportId}?start=${range.from}&end=${range.to}`;

  const emptyNote = (label: string) => (
    <p className="py-10 text-center text-[13px] text-[#94A3B8]">
      {isFetching
        ? "Loading…"
        : `No ${label} tracked between ${longDate(range.from)} and ${longDate(range.to)}.`}
    </p>
  );

  // Billable / Internal list every non-archived project of that billing type
  // unconditionally (zero tracked hours included), so an empty list means
  // "none configured", never "none tracked in this range".
  const emptyBillingNote = (label: string) => (
    <p className="py-10 text-center text-[13px] text-[#94A3B8]">
      {isFetching ? "Loading…" : `No ${label} yet.`}
    </p>
  );

  return (
    <V2Shell
      title="Dashboard Overview"
      subtitle={
        range.from === range.to
          ? `Everything tracked for ${longDate(range.from)}.`
          : `Everything tracked between ${longDate(range.from)} to ${longDate(range.to)}.`
      }
    >
      <div className="w-full space-y-6 pb-20">
        {/* Filters */}
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#E2E8F0] bg-white p-2 pl-4 shadow-sm">
          <div className="flex flex-wrap items-center gap-3">
            <DateRangeFilter value={range} onChange={setRange} />
            <ProjectMultiSelect
              projects={allProjects ?? []}
              selected={selectedProjects}
              onChange={setSelectedProjects}
            />
          </div>
          <button
            onClick={() => { setRange(DEFAULT_RANGE); setSelectedProjects([]); }}
            className="rounded-lg border border-[#E2E8F0] px-4 py-2 text-[13px] font-bold text-[#64748B] transition hover:bg-[#F8FAFC] hover:text-[#0F172A]"
          >
            Reset
          </button>
        </div>

        {isError && (
          <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-[13px] font-semibold text-rose-700">
            The dashboard could not be loaded for this range. Please try again.
          </div>
        )}

        {showSkeleton ? (
          <DashboardSkeleton />
        ) : (
        <div
          className={`space-y-6 transition-all duration-300 ${
            isFetching ? "blur-[2px] opacity-60 pointer-events-none" : ""
          }`}
        >
          {/* KPIs */}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {kpiCard(
              "Avg. Activity",
              activity === null ? "--" : `${activity.toFixed(1)}%`,
              deltaOf(activity, previous?.summary.activity),
              series[0]
            )}
            {kpiCard(
              "Total Hours",
              formatHMS(secondsOf(summary)),
              deltaOf(summary?.total_hours, previous?.summary.total_hours),
              series[1],
              trackedSeries
            )}
            {kpiCard(
              "Active Projects",
              summary?.active_projects ?? 0,
              deltaOf(summary?.active_projects, previous?.summary.active_projects),
              series[2]
            )}
            {kpiCard(
              "Team Members",
              summary?.team_members ?? 0,
              deltaOf(summary?.team_members, previous?.summary.team_members),
              series[3]
            )}
          </div>

          {/* Trend Area Chart */}
          <div className="rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
            <div className="mb-4 flex items-center justify-between">
              <h3 className="text-[13px] font-bold uppercase tracking-wider text-[#64748B]">
                Time Tracked ({longDate(range.from)} - {longDate(range.to)})
              </h3>
              <Legend
                items={[
                  { label: "Tracked Time", color: series[0] },
                  { label: "Manual Time", color: brand.muted },
                ]}
              />
            </div>
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
          </div>

          {/* Top Lists */}
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            {/* Top Projects, with Billable / Free Time-Internal filter tabs */}
            <div className="flex flex-col rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-[13px] font-bold uppercase tracking-wider text-[#64748B]">Top Projects</h3>
                {projectTab === "top" && (
                  <button
                    onClick={() => navigate(reportLink("projects"))}
                    className="text-[11px] font-bold text-[#2563EB] hover:underline"
                  >
                    View All
                  </button>
                )}
              </div>

              {/* Filter tabs */}
              <div className="mb-3 flex gap-1 rounded-lg bg-slate-100 p-1">
                {(
                  [
                    { key: "top", label: "Top Projects" },
                    { key: "billable", label: "Billable" },
                    { key: "internal", label: "Flexible Time" },
                  ] as { key: ProjectFilterTab; label: string }[]
                ).map((tab) => (
                  <button
                    key={tab.key}
                    onClick={() => setProjectTab(tab.key)}
                    className={`flex-1 rounded-md px-2 py-1.5 text-[11px] font-bold transition ${
                      projectTab === tab.key
                        ? "bg-white text-[#0F172A] shadow-sm"
                        : "text-slate-500 hover:text-slate-700"
                    }`}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>

              {/* The scroller. Hover cards are `FloatingCard`s (fixed), so this
                  container cannot clip them. */}
              <div className="max-h-[340px] overflow-y-auto pr-1">
              {projectTab === "top" && (
                topProjects.length === 0 ? (
                  emptyNote("project time")
                ) : (
                  <RankedBars
                    items={topProjects.map((p) => ({
                      id: String(p.project_id),
                      name: p.project_name,
                      value: p.total_hours,
                      meta:
                        p.avg_activity === null ? "No activity samples" : `${p.avg_activity.toFixed(0)}% avg`,
                      secondary: p.avg_activity ?? undefined,
                    }))}
                    color={series[2]}
                    formatValue={(n) => formatHoursAsHMS(n)}
                  />
                )
              )}

              {projectTab === "billable" && (
                billableProjects.length === 0 ? (
                  emptyBillingNote("billable projects")
                ) : (
                  <ul className="flex flex-col gap-1">
                    {billableProjects.map((project) => (
                      <BillableProjectRow key={project.project_id} project={project} />
                    ))}
                  </ul>
                )
              )}

              {projectTab === "internal" && (
                internalProjects.length === 0 ? (
                  emptyBillingNote("internal projects")
                ) : (
                  <ul className="flex flex-col gap-1">
                    {internalProjects.map((project) => (
                      <InternalProjectRow key={project.project_id} project={project} />
                    ))}
                  </ul>
                )
              )}
              </div>
            </div>

            {/* Apps Breakdown Donut */}
            <div className="flex flex-col rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
              <div className="mb-4 flex items-center justify-between">
                <h3 className="text-[13px] font-bold uppercase tracking-wider text-[#64748B]">Top Apps</h3>
                <button
                  onClick={() => navigate(reportLink("apps"))}
                  className="text-[11px] font-bold text-[#2563EB] hover:underline"
                >
                  View All
                </button>
              </div>
              <div className="flex flex-1 flex-col items-center justify-center gap-5 py-4">
                {topApps.length === 0 ? (
                  emptyNote("app usage")
                ) : (
                  <>
                    <Donut
                      size={180}
                      slices={appSlices}
                      centerLabel="Total App Time"
                      centerValue={formatHoursAsHMS(totalAppHours)}
                      activeLabel={activeApp}
                      onActiveChange={setActiveApp}
                    />
                    {/* Without this the donut was four unlabelled arcs — the
                        app names were nowhere on the card. */}
                    <Legend
                      activeLabel={activeApp}
                      onActiveChange={setActiveApp}
                      items={appSlices.map((slice) => ({
                        label: slice.label,
                        color: slice.color,
                        value: formatHoursAsHMS(slice.value),
                        rawValue: slice.value,
                        icon: <AppIcon name={slice.label} size={16} />,
                      }))}
                    />
                  </>
                )}
              </div>
            </div>
          </div>
        </div>
        )}
      </div>
    </V2Shell>
  );
};
