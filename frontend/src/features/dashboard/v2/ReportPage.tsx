import React, { useEffect, useMemo, useState } from "react";
import { Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { V2Shell } from "./V2Shell";
import { Sparkline } from "./charts";
import {
  DateRangeFilter,
  DEFAULT_RANGE,
  MemberMultiSelect,
  ProjectMultiSelect,
  rangeForMonth,
  rangeForSpan,
} from "./filters";
import type { DateRange } from "./filters";
import { monthByKey } from "./mockData";
import { ExportDialog } from "./ExportDialog";
import { MemberBreakdownAccordion, buildMemberItemBreakdown } from "./MemberBreakdownAccordion";
import type { MemberActivity } from "./memberActivity";
import {
  useGetReactReportsSummaryQuery,
  useGetReactReportsListQuery,
  useGetReactReportsTrendQuery,
  useLazyGetDetailedLogsQuery,
} from "../../../store/api/reportsApi";
import type { DetailedLogItem } from "../../../store/api/reportsApi";
import { formatHMS, secondsOf } from "../../../utils/duration";
import { useGetAllMembersQuery } from "../../../store/api/membersApi";
import { useGetAllProjectsQuery } from "../../../store/api/projectsApi";

type ReportId = "projects" | "tasks" | "apps" | "urls";

const REPORTS: Record<
  ReportId,
  { title: string; subtitle: string; dimension: string; dimensionLabel: string; color: string }
> = {
  projects: {
    title: "Project-Wise Activity Report",
    subtitle: "Every project in the selected period",
    dimension: "project",
    dimensionLabel: "Project",
    color: "#2563EB",
  },
  tasks: {
    title: "Top Tasks Report",
    subtitle: "Every task in the selected period",
    dimension: "task",
    dimensionLabel: "Task",
    color: "#10B981",
  },
  apps: {
    title: "Apps Usage Report",
    subtitle: "Every application used in the selected period",
    dimension: "app",
    dimensionLabel: "App",
    color: "#F59E0B",
  },
  urls: {
    title: "URLs Usage Report",
    subtitle: "Every website visited in the selected period",
    dimension: "url",
    dimensionLabel: "URL",
    color: "#8B5CF6",
  }
};

export const ReportPage: React.FC = () => {
  const { reportId } = useParams<{ reportId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  const monthParam = searchParams.get("month");
  // The dashboard hands its own picked span over as ?start=&end=, so following
  // a "View All" link keeps the range the user was already looking at.
  const startParam = searchParams.get("start");
  const endParam = searchParams.get("end");

  const initialRange = (): DateRange => {
    if (startParam && endParam) return rangeForSpan(startParam, endParam);
    if (monthParam) return rangeForMonth(monthParam);
    return DEFAULT_RANGE;
  };

  const [range, setRange] = useState<DateRange>(initialRange);
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  const [selectedProjects, setSelectedProjects] = useState<string[]>([]);
  const [exportOpen, setExportOpen] = useState(false);

  const { data: membersData = [] } = useGetAllMembersQuery();

  const { data: allProjects = [] } = useGetAllProjectsQuery();

  const config = REPORTS[reportId as ReportId];


  // `start_date`/`end_date` are the names the /react/reports endpoints declare.
  // FastAPI drops undeclared query parameters, so sending `from`/`to` here made
  // the server fall back to its default window and the date filter did nothing.
  const queryParams = {
    start_date: range.from,
    end_date: range.to,
    member_id: selectedMembers.length ? selectedMembers.map(Number) : undefined,
    project_id: selectedProjects.length ? selectedProjects.map(Number) : undefined,
  };

  const { data: summaryData } = useGetReactReportsSummaryQuery(
    queryParams,
    { skip: !config }
  );

  const { data: listData } = useGetReactReportsListQuery(
    { dimension: reportId as string, page: 1, limit: 100, sort_by: 'total_hours', sort_order: 'desc', ...queryParams },
    { skip: !config }
  );

  // Each member's average activity, for the card shown when their name is hovered in the Member
  // Breakdown. The server's own per-member figure for exactly these filters -- the same
  // duration-weighted average as the Avg. Activity tile -- rather than anything rebuilt from rows.
  const {
    data: memberActivityData,
    isLoading: isMemberActivityLoading,
    isError: isMemberActivityError,
  } = useGetReactReportsListQuery(
    { dimension: "members", page: 1, limit: 200, sort_by: 'total_hours', sort_order: 'desc', ...queryParams },
    { skip: !config }
  );

  const { data: trendData } = useGetReactReportsTrendQuery(
    queryParams,
    { skip: !config }
  );

  // Member Breakdown, every tab: a member -> date -> ... accordion, built
  // client-side from the row-by-row detailed-log endpoint that already backs
  // the Timesheet CSV export. Fetched eagerly, walking a few pages of the
  // same endpoint that export uses -- but capped far more conservatively
  // (this loads on every page view, not on an explicit export click), with
  // an honest note if the range holds more than that.
  const MEMBER_BREAKDOWN_PAGE_LIMIT = 200;
  const MEMBER_BREAKDOWN_MAX_PAGES = 5;
  const [fetchDetailedLogs] = useLazyGetDetailedLogsQuery();
  const [memberLogs, setMemberLogs] = useState<DetailedLogItem[]>([]);
  const [isMemberLogsLoading, setIsMemberLogsLoading] = useState(false);
  const [isMemberLogsTruncated, setIsMemberLogsTruncated] = useState(false);
  const memberIdsKey = (queryParams.member_id ?? []).join(",");
  const projectIdsKey = (queryParams.project_id ?? []).join(",");

  useEffect(() => {
    if (!config) return;
    let cancelled = false;
    setIsMemberLogsLoading(true);
    (async () => {
      const rows: DetailedLogItem[] = [];
      let page = 1;
      let truncated = false;
      for (;;) {
        // `/reports/detailed-logs` takes the legacy `from`/`to` names, not
        // `start_date`/`end_date` -- FastAPI drops parameters it does not
        // declare, so passing the wrong pair would silently answer with the
        // server's default window instead of the range selected above.
        const response = await fetchDetailedLogs({
          from: range.from,
          to: range.to,
          member_id: queryParams.member_id,
          project_id: queryParams.project_id,
          // projects/members/tasks all return the same session-grain rows,
          // so "projects" serves the Tasks tab too. Apps and URLs are a
          // different row grain entirely (usage samples, not sessions),
          // selected through usage_type rather than a different dimension
          // value -- there is no "urls" dimension on this endpoint.
          dimension: reportId === "apps" || reportId === "urls" ? "apps" : "projects",
          usage_type: reportId === "urls" ? "url" : undefined,
          sort_by: "date",
          sort_desc: true,
          page,
          limit: MEMBER_BREAKDOWN_PAGE_LIMIT,
        }).unwrap();
        rows.push(...(response.items || []));
        const lastPage = Math.max(1, response.pagination?.total_pages || 1);
        if (page >= lastPage || !response.items?.length) break;
        if (page >= MEMBER_BREAKDOWN_MAX_PAGES) {
          truncated = true;
          break;
        }
        page += 1;
      }
      if (!cancelled) {
        setMemberLogs(rows);
        setIsMemberLogsTruncated(truncated);
        setIsMemberLogsLoading(false);
      }
    })().catch(() => {
      if (!cancelled) {
        setMemberLogs([]);
        setIsMemberLogsTruncated(false);
        setIsMemberLogsLoading(false);
      }
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reportId, config, range.from, range.to, memberIdsKey, projectIdsKey, fetchDetailedLogs]);

  // One item per tab: a project's total on Projects, a task's on Tasks, an
  // app's or site's on Apps/URLs -- not a date-by-day drilldown. The range
  // picker above already narrows the window; this answers "how much of each
  // {item}", not "on which day".
  const breakdownPicker = useMemo(() => {
    if (reportId === "tasks") return (row: DetailedLogItem) => row.task_name;
    if (reportId === "urls") return (row: DetailedLogItem) => row.url;
    if (reportId === "apps") return (row: DetailedLogItem) => row.app;
    return (row: DetailedLogItem) => row.project_name;
  }, [reportId]);
  const breakdownFallback =
    reportId === "tasks" ? "No task"
    : reportId === "urls" ? "Unknown site"
    : reportId === "apps" ? "Unknown application"
    : "Unassigned";

  const memberBreakdown = useMemo(
    () => buildMemberItemBreakdown(memberLogs, breakdownPicker, breakdownFallback),
    [memberLogs, breakdownPicker, breakdownFallback]
  );

  const memberActivity = useMemo<MemberActivity>(() => {
    const byMember: Record<number, number | null> = {};
    for (const item of memberActivityData?.items ?? []) {
      if (item.member_id !== undefined) byMember[item.member_id] = item.avg_activity ?? null;
    }
    return { isLoading: isMemberActivityLoading, isError: isMemberActivityError, byMember };
  }, [memberActivityData, isMemberActivityLoading, isMemberActivityError]);

  const finalGrouped = useMemo(() => {
    return (listData?.items || []).map((item: any, i: number) => {
      let name = "Unknown";
      let id = String(i);
      
      if (reportId === 'projects') { name = item.project_name || name; id = String(item.project_id || i); }
      if (reportId === 'tasks') { name = item.task_name || name; id = String(item.task_id || i); }
      if (reportId === 'apps') { name = item.app_name || name; id = String(item.app_id || i); }
      if (reportId === 'urls') { name = item.url_name || name; id = String(item.url_id || i); }
      
      // Exact seconds from the server, with `value` derived from them rather
      // than from the 2dp hours. Rounded hours made a ring slice larger than
      // the exact scope total it is divided by (one project rendered at
      // 100.4%), and rounded a four-second visit down to 00:00:00 in the
      // ranked list beside it while the ring showed its real share.
      const seconds = item.total_seconds ?? Math.round((item.total_hours || 0) * 3600);
      return {
        id,
        name,
        value: seconds / 3600,
        seconds,
        // Null activity means nothing was sampled, which is not 0%.
        secondary: item.avg_activity ?? null,
      };
    }).sort((a: any, b: any) => b.value - a.value);
  }, [listData, reportId]);

  const summary = summaryData;
  const totalTrackedSeconds = secondsOf(summary);
  // Null means nothing in scope was activity-sampled, which is not the same as
  // 0% activity -- say so rather than printing a number nobody measured.
  const avgActivity = summary?.avg_activity ?? null;
  const uniqueMembers = summary?.total_members || 0;
  const totalTasks = summary?.total_tasks || 0;
  const totalGrouped = finalGrouped.length;
  // Every tab renders through this one component, so the copy has to follow the
  // dimension rather than always saying "project".
  const groupNoun = config ? config.dimensionLabel.toLowerCase() : "";
  const groupNounPlural = `${groupNoun}s`;

  // Trend: one point per calendar day in the selected range, straight from
  // /react/reports/trend. A day nobody tracked on comes back as a real zero,
  // so the axis stays the range the user asked for.
  const trendPoints = useMemo(() => trendData?.points ?? [], [trendData]);

  // The tile decorations read the same daily series as the chart, so nothing
  // on this page shows a shape that is not in the data.
  const dailyHours = trendPoints.map((point) => point.total_hours);
  const dailyActivity = trendPoints.map((point) => point.avg_activity ?? 0);

  if (!config) return <Navigate to="/dashboard" replace />;

  const resetFilters = () => {
    setRange(initialRange());
    setSelectedMembers([]);
    setSelectedProjects([]);
  };


  return (
    <V2Shell
      title={config.title}
      subtitle={config.subtitle}
      breadcrumb={
        <button
          onClick={() => navigate("/dashboard")}
          className="mb-1 flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-[#64748B] transition hover:text-[#2563EB]"
        >
          <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" d="M15 19l-7-7 7-7" />
          </svg>
          Dashboard
          {monthParam && <span className="normal-case text-[#94A3B8]">· {monthByKey(monthParam).label}</span>}
        </button>
      }
      actions={
        <>
          <div className="hidden items-center rounded-lg border border-[#E2E8F0] bg-white p-0.5 lg:flex">
            {(Object.keys(REPORTS) as ReportId[]).map((id) => (
              <button
                key={id}
                onClick={() => navigate(`/dashboard/reports/${id}${monthParam ? `?month=${monthParam}` : ""}`)}
                className={
                  // Tailwind v4's preflight gives buttons the arrow cursor;
                  // a report tab is a link in spirit, so it gets the hand.
                  "cursor-pointer rounded-[6px] px-4 py-2 text-xs font-bold capitalize transition " +
                  (id === reportId ? "bg-[#2563EB] text-white shadow-sm" : "text-[#64748B] hover:text-[#0F172A]")
                }
              >
                {id}
              </button>
            ))}
          </div>
          <button
            onClick={() => setExportOpen(true)}
            className="flex items-center gap-1.5 rounded-lg bg-[#0F172A] px-4 py-2 text-xs font-bold text-white shadow-sm transition hover:bg-[#1E293B]"
          >
            <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" />
            </svg>
            Export CSV
          </button>
        </>
      }
    >
      <div className="w-full space-y-6">
        
        {/* Filters */}
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#E2E8F0] bg-white p-2 pl-4 shadow-sm">
          <DateRangeFilter value={range} onChange={(r: any) => { setRange(r) }} />
          <div className="flex flex-wrap items-center gap-2">
            <MemberMultiSelect members={membersData} selected={selectedMembers} onChange={(ids: any) => { setSelectedMembers(ids) }} />
            <ProjectMultiSelect projects={allProjects} selected={selectedProjects} onChange={(ids: any) => { setSelectedProjects(ids) }} />
            <button
              onClick={resetFilters}
              className="rounded-lg border border-[#E2E8F0] px-4 py-2 text-[13px] font-bold text-[#64748B] transition hover:bg-[#F8FAFC] hover:text-[#0F172A]"
            >
              Reset
            </button>
          </div>
        </div>

        {/* Summary Tiles */}
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-4">
          {/* Tile 1: Total Hours */}
          <div className="relative overflow-hidden rounded-2xl border border-[#E2E8F0] bg-white p-5 shadow-sm transition-all hover:-translate-y-1 hover:shadow-md">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[#2563EB] text-white">
                <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
              </div>
              <span className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">Total Time</span>
            </div>
            <div className="mt-4 flex items-end justify-between">
              <div className="relative z-10">
                <div className="text-[32px] font-extrabold leading-none tracking-tight text-[#0F172A]">{formatHMS(totalTrackedSeconds)}</div>
                <div className="mt-1.5 text-[11px] text-[#94A3B8]">{range.from} - {range.to}</div>
              </div>
              {dailyHours.length > 1 && (
                <div className="absolute bottom-0 right-0 h-16 w-32 opacity-70">
                  <Sparkline values={dailyHours} color="#2563EB" height={64} />
                </div>
              )}
            </div>
          </div>

          {/* Tile 2: Avg Activity */}
          <div className="relative overflow-hidden rounded-2xl border border-[#E2E8F0] bg-white p-5 shadow-sm transition-all hover:-translate-y-1 hover:shadow-md">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[#10B981] text-white">
                <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" /></svg>
              </div>
              <span className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">Avg. Activity</span>
            </div>
            <div className="mt-4 flex items-end justify-between">
              <div className="relative z-10">
                <div className="text-[32px] font-extrabold leading-none tracking-tight text-[#0F172A]">{avgActivity === null ? "--" : `${avgActivity}%`}</div>
                <div className="mt-1.5 text-[11px] text-[#94A3B8]">{avgActivity === null ? "Nothing sampled in range" : "Across filtered entries"}</div>
              </div>
              <div className="absolute bottom-4 right-4 flex items-end gap-1">
                {dailyActivity.slice(-6).map((percent, i) => (
                  <div
                    key={i}
                    className="w-1.5 rounded-full bg-[#10B981] opacity-40"
                    // 0-100% mapped onto the tile's 32px well; a floor of 2px
                    // keeps a zero-activity day visible as an empty bar.
                    style={{ height: Math.max(2, (percent / 100) * 32) }}
                  />
                ))}
              </div>
            </div>
          </div>

          {/* Tile 3: Members */}
          <div className="relative overflow-hidden rounded-2xl border border-[#E2E8F0] bg-white p-5 shadow-sm transition-all hover:-translate-y-1 hover:shadow-md">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[#8B5CF6] text-white">
                <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z" /></svg>
              </div>
              <span className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">Members</span>
            </div>
            <div className="mt-4 flex items-end justify-between">
              <div className="relative z-10">
                <div className="text-[32px] font-extrabold leading-none tracking-tight text-[#0F172A]">{uniqueMembers}</div>
                <div className="mt-1.5 text-[11px] text-[#94A3B8]">Included in this report</div>
              </div>
              <div className="absolute -right-2 top-10 flex h-20 w-20 items-center justify-center rounded-full bg-[#8B5CF6]/10">
                <svg className="h-10 w-10 text-[#8B5CF6]" fill="currentColor" viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
              </div>
            </div>
          </div>

          {/* Tile 4: Entries */}
          <div className="relative overflow-hidden rounded-2xl border border-[#E2E8F0] bg-white p-5 shadow-sm transition-all hover:-translate-y-1 hover:shadow-md">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[#F59E0B] text-white">
                <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" /></svg>
              </div>
              <span className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">Tasks</span>
            </div>
            <div className="mt-4 flex items-end justify-between">
              <div className="relative z-10">
                <div className="text-[32px] font-extrabold leading-none tracking-tight text-[#0F172A]">{totalTasks}</div>
                <div className="mt-1.5 text-[11px] text-[#94A3B8]">Across {totalGrouped} {groupNounPlural}</div>
              </div>
              <div className="absolute -right-2 top-10 flex h-20 w-20 items-center justify-center rounded-full bg-[#F59E0B]/10">
                <svg className="h-10 w-10 text-[#F59E0B]" fill="currentColor" viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-5 14H7v-2h7v2zm3-4H7v-2h10v2zm0-4H7V7h10v2z"/></svg>
              </div>
            </div>
          </div>
        </div>

        {/* The Hours-by-dimension ranked bars, Activity Trend and Hours
            Distribution sections used to sit here; removed at the owner's
            request (2026-09-29). The summary tiles above and the member
            breakdown below are the whole page now. */}

        {/* Member Breakdown: tracked hours by member, drilling down into
            exactly the one thing this tab is about -- a project's total on
            Projects, a task's on Tasks, an app's or site's on Apps/URLs. */}
        <MemberBreakdownAccordion
          members={memberBreakdown}
          isLoading={isMemberLogsLoading}
          isTruncated={isMemberLogsTruncated}
          itemLabel={
            reportId === "tasks" ? "Task" : reportId === "urls" ? "Site" : reportId === "apps" ? "App" : "Project"
          }
          accentColor={config.color}
          activity={memberActivity}
          emptyLabel={`No tracked time for any member between ${range.from} and ${range.to}.`}
        />

        {/* The dialog re-queries with `queryParams`, so the file always covers
            exactly the range/projects/members selected above. */}
        <ExportDialog
          open={exportOpen}
          onClose={() => setExportOpen(false)}
          reportId={reportId as ReportId}
          reportTitle={config.title}
          dimensionLabel={config.dimensionLabel}
          range={range}
          selectedMembers={selectedMembers}
          selectedProjects={selectedProjects}
          members={membersData}
          projects={allProjects}
          queryParams={queryParams}
        />
      </div>
    </V2Shell>
  );
};
