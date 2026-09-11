import React, { useMemo, useState } from "react";
import { Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { MemberShell, MEMBER_REPORT_LINKS } from "./MemberShell";
import { Card, EmptyState, ErrorNote, Spinner } from "./MemberUi";
import { TrendAreaChart, RankedBars, Donut, Legend } from "../dashboard/v2/charts";
import { AppIcon } from "../../components/AppIcon";
import {
  DateRangeFilter,
  ProjectMultiSelect,
  DEFAULT_RANGE,
  rangeForSpan,
  exportToCsv,
} from "../dashboard/v2/filters";
import type { DateRange } from "../dashboard/v2/filters";
import { series } from "../dashboard/v2/theme";
import {
  useGetReactReportsSummaryQuery,
  useGetReactReportsListQuery,
  useGetReactReportsTrendQuery,
} from "../../store/api/reportsApi";
import { useGetAllProjectsQuery } from "../../store/api/projectsApi";
import { formatHMS, formatHoursAsHMS } from "../../utils/duration";

/**
 * The member's own reports: the same four dimensions the admin Reports page
 * offers, answered for one person.
 *
 * The endpoints are shared with the admin page and pin `member_id` to the
 * caller server-side, so this page carries no member picker — there is only
 * ever one member in scope, and offering a filter that cannot change the
 * answer would be a lie about what the page can do.
 */

type ReportId = "projects" | "tasks" | "apps" | "urls";

const REPORTS: Record<
  ReportId,
  { title: string; subtitle: string; dimensionLabel: string; column: string }
> = {
  projects: {
    title: "My Project Report",
    subtitle: "How your tracked time splits across the projects you work on.",
    dimensionLabel: "Project",
    column: "Project",
  },
  tasks: {
    title: "My Task Report",
    subtitle: "How your tracked time splits across individual tasks.",
    dimensionLabel: "Task",
    column: "Task",
  },
  apps: {
    title: "My App Report",
    subtitle: "The applications recorded while you were tracking time.",
    dimensionLabel: "Application",
    column: "Application",
  },
  urls: {
    title: "My URL Report",
    subtitle: "The addresses recorded while you were tracking time.",
    dimensionLabel: "URL",
    column: "URL",
  },
};

const DONUT_COLORS = ["#2563EB", "#0D9488", "#7C3AED", "#D97706", "#DB2777"];

const longDate = (iso: string) => {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, (m || 1) - 1, d || 1).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
};

export const MemberReports: React.FC = () => {
  const { reportId } = useParams<{ reportId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  // The dashboard hands its picked span over as ?start=&end=, so following a
  // "View Report" link keeps the range the member was already looking at.
  const startParam = searchParams.get("start");
  const endParam = searchParams.get("end");
  const initialRange = (): DateRange =>
    startParam && endParam ? rangeForSpan(startParam, endParam) : DEFAULT_RANGE;

  const [range, setRange] = useState<DateRange>(initialRange);
  const [selectedProjects, setSelectedProjects] = useState<string[]>([]);

  const config = REPORTS[reportId as ReportId];

  // Only the member's own projects — `/projects` is already scoped to the
  // projects they are a member of.
  const { data: myProjects = [] } = useGetAllProjectsQuery();

  const queryParams = {
    start_date: range.from,
    end_date: range.to,
    project_id: selectedProjects.length ? selectedProjects.map(Number) : undefined,
  };

  const { data: summaryData, isFetching: isSummaryFetching, isError: isSummaryError } =
    useGetReactReportsSummaryQuery(queryParams, { skip: !config });
  const { data: listData, isFetching: isListFetching, isError: isListError } =
    useGetReactReportsListQuery(
      { dimension: reportId as string, page: 1, limit: 100, sort_by: "total_hours", sort_order: "desc", ...queryParams },
      { skip: !config }
    );
  const { data: trendData, isFetching: isTrendFetching } = useGetReactReportsTrendQuery(queryParams, {
    skip: !config,
  });

  const isFetching = isSummaryFetching || isListFetching || isTrendFetching;
  const isError = isSummaryError || isListError;

  const rows = useMemo(() => {
    return (listData?.items || [])
      .map((item, index) => {
        let name = "Unknown";
        let id = String(index);
        if (reportId === "projects") {
          name = item.project_name || name;
          id = String(item.project_id ?? index);
        } else if (reportId === "tasks") {
          name = item.task_name || name;
          id = String(item.task_id ?? index);
        } else if (reportId === "apps") {
          name = item.app_name || name;
          id = String(item.app_id ?? index);
        } else if (reportId === "urls") {
          name = item.url_name || name;
          id = String(item.url_id ?? index);
        }
        return {
          id,
          name,
          hours: item.total_hours || 0,
          seconds: Math.round((item.total_hours || 0) * 3600),
          // Null means nothing was sampled, which is not 0% activity.
          activity: item.avg_activity ?? null,
        };
      })
      .sort((a, b) => b.hours - a.hours);
  }, [listData, reportId]);

  const trendPoints = useMemo(() => trendData?.points ?? [], [trendData]);
  const trendLabels = trendPoints.map((point) =>
    new Date(`${point.date}T00:00:00`).toLocaleDateString("en-GB", { day: "2-digit", month: "short" })
  );

  const totalSeconds = Math.round((summaryData?.total_hours || 0) * 3600);
  const avgActivity = summaryData?.avg_activity ?? null;

  /**
   * The population this chart's slices are drawn from: every row the filters
   * match, which is what `total_seconds` on the list response measures.
   *
   * It used to be the summary strip's total. On the Apps and URLs tabs that is
   * a different measure — application and browser time is captured separately
   * from the session and covers only what the desktop could observe — so the
   * remainder silently absorbed every unattributed second of the day and drew
   * it as one huge unnamed arc. See the same comment in the admin ReportPage.
   */
  const distributionSeconds = listData?.total_seconds ?? 0;
  const isUsageDimension = reportId === "apps" || reportId === "urls";
  const unmeasuredSeconds = Math.max(0, totalSeconds - distributionSeconds);

  const donutSlices = useMemo(() => {
    const named = rows.slice(0, 5).map((row, index) => ({
      label: row.name,
      value: row.hours,
      color: DONUT_COLORS[index % DONUT_COLORS.length],
    }));
    // The rest of this tab's own rows, named for what it is and how many
    // rows it covers -- not a category, and never a home for time no row
    // accounts for.
    const rankedTotal = listData?.total ?? rows.length;
    const restSeconds =
      (listData?.total_seconds ?? 0) - named.reduce((sum, slice) => sum + slice.value * 3600, 0);
    if (rankedTotal > 5 && restSeconds > 1) {
      named.push({
        label: `Other ${config?.dimensionLabel.toLowerCase()}s (${rankedTotal - 5})`,
        value: Math.round((restSeconds / 3600) * 100) / 100,
        color: "#94A3B8",
      });
    }
    return named;
  }, [rows, listData, config]);

  if (!config) return <Navigate to="/member/reports/projects" replace />;

  const handleExport = () => {
    exportToCsv(
      `my-${reportId}-report_${range.from}_to_${range.to}.csv`,
      [config.column, "Tracked Time", "Tracked Hours", "Avg Activity %"],
      rows.map((row) => [
        row.name,
        formatHMS(row.seconds),
        row.hours,
        row.activity === null ? "No samples" : row.activity.toFixed(1),
      ]),
      [
        ["Report", config.title],
        ["Date range", `${range.from} to ${range.to}`],
        [
          "Projects",
          selectedProjects.length
            ? myProjects
                .filter((project) => selectedProjects.includes(String(project.id)))
                .map((project) => project.project_name)
                .join(" | ")
            : "All my projects",
        ],
        [],
      ]
    );
  };

  const tile = (label: string, value: string, note: string, color: string) => (
    <div className="rounded-2xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
      <div className="flex items-center gap-3">
        <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: color }} />
        <span className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">{label}</span>
      </div>
      <div className="mt-4 text-[30px] font-extrabold leading-none tracking-tight text-[#0F172A]">{value}</div>
      <div className="mt-1.5 text-[11px] text-[#94A3B8]">{note}</div>
    </div>
  );

  return (
    <MemberShell
      title={config.title}
      subtitle={config.subtitle}
      breadcrumb={
        <button
          onClick={() => navigate("/member/dashboard")}
          className="mb-1 flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-[#64748B] transition hover:text-[#2563EB]"
        >
          <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" d="M15 19l-7-7 7-7" />
          </svg>
          My Dashboard
        </button>
      }
      actions={
        <>
          <div className="hidden items-center rounded-lg border border-[#E2E8F0] bg-white p-0.5 lg:flex">
            {MEMBER_REPORT_LINKS.map((report) => (
              <button
                key={report.id}
                onClick={() => navigate(`/member/reports/${report.id}`)}
                className={
                  "rounded-[6px] px-4 py-2 text-xs font-bold transition " +
                  (report.id === reportId
                    ? "bg-[#2563EB] text-white shadow-sm"
                    : "text-[#64748B] hover:text-[#0F172A]")
                }
              >
                {report.label}
              </button>
            ))}
          </div>
          <button
            onClick={handleExport}
            disabled={rows.length === 0}
            className="flex items-center gap-1.5 rounded-lg bg-[#0F172A] px-4 py-2 text-xs font-bold text-white shadow-sm transition hover:bg-[#1E293B] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="2.5"
                d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3"
              />
            </svg>
            Export CSV
          </button>
        </>
      }
    >
      <div className="w-full space-y-6 pb-20">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#E2E8F0] bg-white p-2 pl-4 shadow-sm">
          <DateRangeFilter value={range} onChange={setRange} />
          <div className="flex flex-wrap items-center gap-2">
            <ProjectMultiSelect
              projects={myProjects}
              selected={selectedProjects}
              onChange={setSelectedProjects}
            />
            <button
              onClick={() => {
                setRange(initialRange());
                setSelectedProjects([]);
              }}
              className="rounded-lg border border-[#E2E8F0] px-4 py-2 text-[13px] font-bold text-[#64748B] transition hover:bg-[#F8FAFC] hover:text-[#0F172A]"
            >
              Reset
            </button>
          </div>
        </div>

        {isError && <ErrorNote message="This report could not be loaded for the selected range." />}

        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-4">
          {tile("Total Time", formatHMS(totalSeconds), `${range.from} - ${range.to}`, series[0])}
          {tile(
            "Avg. Activity",
            avgActivity === null ? "--" : `${avgActivity.toFixed(1)}%`,
            avgActivity === null ? "Nothing sampled in this range" : "Across your sampled time",
            series[1]
          )}
          {tile(config.dimensionLabel + "s", String(rows.length), `With tracked time in range`, series[2])}
          {tile("Tasks", String(summaryData?.total_tasks ?? 0), "Distinct tasks you tracked on", series[3])}
        </div>

        <Card
          title="Daily Trend"
          action={
            <Legend
              items={[
                { label: "Hours", color: series[0] },
                { label: "Activity %", color: "#10B981" },
              ]}
            />
          }
        >
          <div className="h-64 w-full">
            {trendPoints.length > 0 ? (
              <TrendAreaChart
                labels={trendLabels}
                seriesList={[
                  { label: "Hours", values: trendPoints.map((p) => p.total_hours), color: series[0] },
                  {
                    label: "Activity %",
                    // A day with no samples has no reading; drawn as zero so
                    // the axis stays continuous, and the tiles say so in words.
                    values: trendPoints.map((p) => p.avg_activity ?? 0),
                    color: "#10B981",
                  },
                ]}
                unit=""
              />
            ) : (
              <div className="flex h-full items-center justify-center text-[13px] text-[#94A3B8]">
                {isFetching ? "Loading…" : "No days in the selected range."}
              </div>
            )}
          </div>
        </Card>

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <Card title={`Top ${config.dimensionLabel}s`}>
            {isFetching && rows.length === 0 ? (
              <Spinner />
            ) : rows.length === 0 ? (
              <EmptyState
                message={`You tracked no ${config.dimensionLabel.toLowerCase()} time in this range.`}
                hint={`Nothing was recorded between ${longDate(range.from)} and ${longDate(range.to)}.`}
              />
            ) : (
              <RankedBars
                items={rows.slice(0, 10).map((row) => ({
                  id: row.id,
                  name: row.name,
                  value: row.hours,
                  meta: row.activity === null ? "No activity samples" : `${row.activity.toFixed(0)}% avg`,
                  secondary: row.activity ?? undefined,
                }))}
                color={series[0]}
                formatValue={(n) => formatHoursAsHMS(n)}
                appIcons={reportId === "apps"}
              />
            )}
          </Card>

          <Card title="Distribution">
            {rows.length === 0 ? (
              <EmptyState message="Nothing to distribute yet." />
            ) : (
              <div className="flex flex-col items-center gap-6 py-2">
                <Donut
                  size={190}
                  slices={donutSlices}
                  centerLabel="Total"
                  // The slices' own total, not the session total: a ring whose
                  // centre disagrees with its arcs is the bug this page had.
                  centerValue={formatHMS(distributionSeconds)}
                />
                <Legend
                  items={donutSlices.map((slice) => ({
                    label: slice.label,
                    color: slice.color,
                    value: formatHoursAsHMS(slice.value),
                    // The remainder arc aggregates several apps; no one mark
                    // stands for it.
                    icon:
                      reportId === "apps" && !slice.label.startsWith("Other ") ? (
                        <AppIcon name={slice.label} size={16} />
                      ) : undefined,
                  }))}
                />
                {/* Where application/browser capture covers less of the day
                    than the timer did, say so with the real numbers instead
                    of drawing the difference as an application. */}
                {isUsageDimension && distributionSeconds > 0 && unmeasuredSeconds > 0 && (
                  <p className="max-w-md text-center text-[11px] leading-4 text-[#64748B]">
                    {formatHMS(distributionSeconds)} of{" "}
                    {reportId === "apps" ? "application" : "browser"} activity was recorded against{" "}
                    {formatHMS(totalSeconds)} of your tracked time. The remaining{" "}
                    {formatHMS(unmeasuredSeconds)} was tracked but not attributed to
                    {reportId === "apps" ? " an application" : " a web address"}.
                  </p>
                )}
              </div>
            )}
          </Card>
        </div>

        <Card title="Detail">
          {rows.length === 0 ? (
            <EmptyState message="No rows for this range." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] text-left text-[13px]">
                <thead>
                  <tr className="border-b border-[#E2E8F0] text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">
                    <th className="px-3 py-3">#</th>
                    <th className="px-3 py-3">{config.column}</th>
                    <th className="px-3 py-3 text-right">Tracked Time</th>
                    <th className="px-3 py-3 text-right">Avg Activity</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, index) => (
                    <tr key={`${row.id}-${index}`} className="border-b border-[#F1F5F9] last:border-0">
                      <td className="px-3 py-3 text-[#94A3B8]">{index + 1}</td>
                      <td className="px-3 py-3 font-semibold text-[#0F172A]">{row.name}</td>
                      <td className="px-3 py-3 text-right font-mono font-bold text-[#0F172A]">
                        {formatHMS(row.seconds)}
                      </td>
                      <td className="px-3 py-3 text-right text-[#64748B]">
                        {row.activity === null ? (
                          <span className="text-[#CBD5E1]" title="Nothing sampled">
                            —
                          </span>
                        ) : (
                          `${row.activity.toFixed(1)}%`
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </MemberShell>
  );
};
