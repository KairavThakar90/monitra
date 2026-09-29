import React, { useEffect, useMemo, useState } from "react";
import type { Member } from "../../../store/api/membersApi";
import type { Project } from "../../../store/api/projectsApi";
import type {
  DetailedLogItem,
  ReactReportsItem,
  ReactReportQueryParams,
} from "../../../store/api/reportsApi";
import {
  useLazyGetDetailedLogsQuery,
  useLazyGetReactReportsListQuery,
} from "../../../store/api/reportsApi";
import { useGetMemberDetailsQuery } from "../../../store/api/membersApi";
import { useAuth } from "../../auth/authContext";
import { formatHoursAsHMS, IST_TIME_ZONE } from "../../../utils/duration";
import { exportToCsv } from "./filters";
import type { DateRange } from "./filters";
import {
  buildTimesheetRows,
  datesInRange,
  timesheetBody,
  timesheetHeaders,
} from "./timesheetExport";

/**
 * Export dialog for the Reports page.
 *
 * Two things it deliberately does NOT do:
 *
 *  - It does not export the rows already rendered on screen. The page only
 *    holds the first 100 rows of the ranked list, so exporting that array
 *    would silently truncate a wider report. This re-queries the same endpoint
 *    with the same filters and walks every page (the backend caps `limit` at
 *    200) so the file is the complete filtered result set.
 *  - It does not invent columns. Every column below maps to a field the
 *    /react/reports/{dimension} response actually carries. A metric the API
 *    returns as null (nothing was activity-sampled) is written as an empty
 *    cell, never as 0 — "not measured" and "measured zero" are different
 *    answers and the spreadsheet has to keep them apart.
 */

/** The backend's hard ceiling on `limit` (see reports_page/router.py). */
const PAGE_LIMIT = 200;
/** Stops the walk if the server ever reports an inconsistent `pages`. */
const MAX_PAGES = 500;

type ReportId = "projects" | "tasks" | "apps" | "urls";

/**
 * Which shape of file to write.
 *
 *  - `report`   the ranked table of the report tab currently open.
 *  - `timesheet` one row per member x project x to-do, one column per day.
 *
 * The timesheet is not a variant of the report table: it is built from
 * `/reports/detailed-logs`, the only endpoint carrying (date, member, project,
 * task) grain, and it covers every member the filters allow rather than the
 * dimension of the open tab. It is therefore offered on all four tabs.
 */
type ExportFormat = "report" | "timesheet";

/**
 * Everything a cell may need beyond its own row — currently just the
 * export-wide total that `% of Total` is measured against.
 */
interface ExportContext {
  totalHours: number;
}

interface ColumnDef {
  key: string;
  label: string;
  /** Cell value. `null` renders as an empty cell rather than a zero. */
  value: (row: ReactReportsItem, index: number, ctx: ExportContext) => string | number | null;
  /** Off unless the user ticks it. */
  optional?: boolean;
}

const nameOf = (dimension: ReportId, row: ReactReportsItem): string => {
  if (dimension === "projects") return row.project_name || "Unknown";
  if (dimension === "tasks") return row.task_name || "Unknown";
  if (dimension === "apps") return row.app_name || "Unknown";
  return row.url_name || "Unknown";
};

const idOf = (dimension: ReportId, row: ReactReportsItem): number | null => {
  if (dimension === "projects") return row.project_id ?? null;
  if (dimension === "tasks") return row.task_id ?? null;
  if (dimension === "apps") return row.app_id ?? null;
  return row.url_id ?? null;
};

/**
 * Columns for one dimension, in export order.
 *
 * `total_tasks` is omitted on the Task tab because the API documents it as
 * always 1 there — a column of ones tells the reader nothing.
 */
const columnsFor = (dimension: ReportId, dimensionLabel: string): ColumnDef[] => {
  const columns: ColumnDef[] = [
    { key: "rank", label: "Sr. No.", value: (_row, index) => index + 1 },
    { key: "name", label: dimensionLabel, value: (row) => nameOf(dimension, row) },
    { key: "id", label: `${dimensionLabel} ID`, value: (row) => idOf(dimension, row), optional: true },
    { key: "time", label: "Total Time (HH:MM:SS)", value: (row) => formatHoursAsHMS(row.total_hours) },
    { key: "hours", label: "Total Hours", value: (row) => Number(row.total_hours ?? 0).toFixed(2) },
    {
      key: "share",
      label: "% of Total",
      value: (row, _index, ctx) =>
        ctx.totalHours > 0 ? (((row.total_hours ?? 0) / ctx.totalHours) * 100).toFixed(2) : null,
    },
    {
      key: "activity",
      label: "Avg Activity (%)",
      // Null means nothing in this row's scope was sampled. An empty cell says
      // that; a 0 would claim the row was measured and found idle.
      value: (row) => (row.avg_activity == null ? null : row.avg_activity),
    },
    { key: "members", label: "Members", value: (row) => row.total_members ?? 0 },
  ];
  if (dimension !== "tasks") {
    columns.push({ key: "tasks", label: "Tasks", value: (row) => row.total_tasks ?? 0 });
  }
  return columns;
};

export const ExportDialog: React.FC<{
  open: boolean;
  onClose: () => void;
  reportId: ReportId;
  reportTitle: string;
  dimensionLabel: string;
  range: DateRange;
  selectedMembers: string[];
  selectedProjects: string[];
  members: Member[];
  projects: Project[];
  /** The date/member/project filters exactly as the page sends them. */
  queryParams: ReactReportQueryParams;
}> = ({
  open,
  onClose,
  reportId,
  reportTitle,
  dimensionLabel,
  range,
  // selectedMembers/selectedProjects/members/projects stay in the prop type
  // for the callers' sake but are no longer read: they only fed the applied
  // filters chips and the file's filter-summary block, both removed.
  queryParams,
}) => {
  const columns = useMemo(() => columnsFor(reportId, dimensionLabel), [reportId, dimensionLabel]);

  const [selectedColumns, setSelectedColumns] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [format, setFormat] = useState<ExportFormat>("report");

  const [fetchList] = useLazyGetReactReportsListQuery();
  const [fetchLogs] = useLazyGetDetailedLogsQuery();

  // Everyone in the export shares the caller's organization, so its name is
  // read once from the caller's own record rather than per exported member.
  // If the record does not carry one, the column is left empty -- an invented
  // organization name would be worse than an honest blank.
  const { currentUser } = useAuth();
  const { data: me } = useGetMemberDetailsQuery(
    { id: currentUser?.id as number },
    { skip: !open || !currentUser }
  );
  const organizationName = me?.member?.organization?.name ?? "";

  // Every non-optional column starts ticked. Re-runs when the tab changes,
  // because the column set itself differs per dimension.
  useEffect(() => {
    setSelectedColumns(columns.filter((c) => !c.optional).map((c) => c.key));
  }, [columns]);

  useEffect(() => {
    if (!open) {
      setBusy(false);
      setProgress(null);
      setError(null);
      setFormat("report");
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, busy, onClose]);

  if (!open) return null;

  const toggleColumn = (key: string) =>
    setSelectedColumns((current) =>
      current.includes(key) ? current.filter((k) => k !== key) : [...current, key]
    );

  const activeColumns = columns.filter((c) => selectedColumns.includes(c.key));

  /** Walks every page of the filtered result set. */
  const fetchAllRows = async (): Promise<ReactReportsItem[]> => {
    const rows: ReactReportsItem[] = [];
    let page = 1;
    for (;;) {
      setProgress(`Fetching page ${page}…`);
      const response = await fetchList({
        dimension: reportId,
        page,
        limit: PAGE_LIMIT,
        sort_by: "total_hours",
        sort_order: "desc",
        ...queryParams,
      }).unwrap();
      rows.push(...(response.items || []));
      const lastPage = Math.max(1, response.pages || 1);
      if (page >= lastPage || !response.items?.length || page >= MAX_PAGES) break;
      page += 1;
    }
    return rows;
  };

  /**
   * Walks every page of the row-by-row log for the current filters.
   *
   * `/reports/detailed-logs` takes the legacy `from`/`to` names, not
   * `start_date`/`end_date`; FastAPI drops parameters it does not declare, so
   * passing the wrong pair would silently export the server's default window.
   */
  const fetchAllLogs = async (): Promise<DetailedLogItem[]> => {
    const logs: DetailedLogItem[] = [];
    let page = 1;
    for (;;) {
      setProgress(`Fetching page ${page}\u2026`);
      const response = await fetchLogs({
        from: queryParams.start_date,
        to: queryParams.end_date,
        member_id: queryParams.member_id,
        project_id: queryParams.project_id,
        // projects/members/tasks all return the same session-grain rows; the
        // apps dimension would return per-app rows and double-count the day.
        dimension: "projects",
        sort_by: "date",
        sort_desc: false,
        page,
        limit: PAGE_LIMIT,
      }).unwrap();
      logs.push(...(response.items || []));
      const lastPage = Math.max(1, response.pagination?.total_pages || 1);
      if (page >= lastPage || !response.items?.length || page >= MAX_PAGES) break;
      page += 1;
    }
    return logs;
  };

  const exportTimesheet = async () => {
    const logs = await fetchAllLogs();
    if (!logs.length) {
      setError("No tracked time matches these filters, so there is nothing to export.");
      return;
    }
    setProgress("Building file\u2026");

    const dates = datesInRange(range.from, range.to);
    const rows = buildTimesheetRows(logs);

    exportToCsv(
      `timesheet_report_${range.from}_to_${range.to}.csv`,
      timesheetHeaders(dates),
      timesheetBody(rows, dates, organizationName, IST_TIME_ZONE),
      [],
      // Quoted throughout, matching the timesheet format this mirrors.
      true
    );
    onClose();
  };

  const handleExport = async () => {
    if (format === "timesheet") {
      if (busy) return;
      setBusy(true);
      setError(null);
      try {
        await exportTimesheet();
      } catch (caught: any) {
        setError(
          caught?.data?.detail ||
            "Export failed. Please check your connection and try again."
        );
      } finally {
        setBusy(false);
        setProgress(null);
      }
      return;
    }

    if (!activeColumns.length || busy) return;
    setBusy(true);
    setError(null);
    try {
      const rows = await fetchAllRows();
      if (!rows.length) {
        setError("No rows match these filters, so there is nothing to export.");
        return;
      }
      setProgress("Building file…");

      // Share-of-total is measured against the rows in this file, so the
      // percentage column always adds up to 100 within the export itself.
      const totalHours = rows.reduce((sum, row) => sum + (row.total_hours ?? 0), 0);

      const context: ExportContext = { totalHours };

      const headers = activeColumns.map((c) => c.label);
      const body = rows.map((row, index) =>
        activeColumns.map((column) => {
          const value = column.value(row, index, context);
          return value == null ? "" : value;
        })
      );

      exportToCsv(
        `${reportId}-report_${range.from}_to_${range.to}.csv`,
        headers,
        body,
        []
      );
      onClose();
    } catch (caught: any) {
      setError(
        caught?.data?.detail ||
          "Export failed. Please check your connection and try again."
      );
    } finally {
      setBusy(false);
      setProgress(null);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-[#0F172A]/40 backdrop-blur-[2px]"
        onClick={() => !busy && onClose()}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Export report"
        className="relative flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-[#E2E8F0] bg-white shadow-2xl"
      >
        <header className="flex items-start justify-between gap-4 border-b border-[#E2E8F0] px-6 py-5">
          <div>
            <h2 className="text-[16px] font-bold tracking-tight text-[#0F172A]">Export report</h2>
            <p className="mt-0.5 text-[12px] text-[#94A3B8]">
              Downloads every row matching the filters below — not just what is on screen.
            </p>
          </div>
          <button
            onClick={() => !busy && onClose()}
            disabled={busy}
            aria-label="Close"
            className="rounded-lg p-1.5 text-[#94A3B8] transition hover:bg-[#F1F5F9] hover:text-[#0F172A] disabled:opacity-40"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-5">
          {/* Format. The two files answer different questions, so this is a
              choice of report rather than a styling option. */}
          <section className="mb-6">
            <h3 className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">Format</h3>
            <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
              {([
                {
                  id: "report" as ExportFormat,
                  title: reportTitle,
                  note: "The ranked table on screen, with the columns you pick below.",
                },
                {
                  id: "timesheet" as ExportFormat,
                  title: "Timesheet",
                  note: "Every member x project x to-do, one column per day.",
                },
              ]).map((option) => {
                const active = format === option.id;
                return (
                  <button
                    key={option.id}
                    type="button"
                    onClick={() => setFormat(option.id)}
                    className={
                      "rounded-lg border px-3 py-2.5 text-left transition " +
                      (active
                        ? "border-[#2563EB]/40 bg-[#EFF6FF]"
                        : "border-[#E2E8F0] hover:bg-[#F8FAFC]")
                    }
                  >
                    <span className="block truncate text-[13px] font-bold text-[#0F172A]">
                      {option.title}
                    </span>
                    <span className="mt-0.5 block text-[11px] font-medium text-[#94A3B8]">
                      {option.note}
                    </span>
                  </button>
                );
              })}
            </div>
          </section>

          {/* Columns. The timesheet's columns are its days, which are fixed
              by the selected range, so there is nothing to choose. */}
          <section className={format === "timesheet" ? "hidden" : ""}>
            <div className="flex items-center justify-between">
              <h3 className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">
                Columns ({activeColumns.length}/{columns.length})
              </h3>
              <div className="flex items-center gap-3">
                <button
                  onClick={() => setSelectedColumns(columns.map((c) => c.key))}
                  className="text-[12px] font-bold text-[#2563EB] transition hover:underline"
                >
                  Select all
                </button>
                <button
                  onClick={() => setSelectedColumns([])}
                  className="text-[12px] font-bold text-[#64748B] transition hover:underline"
                >
                  Clear
                </button>
              </div>
            </div>
            <div className="mt-3 grid grid-cols-1 gap-1.5 sm:grid-cols-2">
              {columns.map((column) => {
                const checked = selectedColumns.includes(column.key);
                return (
                  <label
                    key={column.key}
                    className={
                      "flex cursor-pointer items-center gap-2.5 rounded-lg border px-3 py-2 transition " +
                      (checked ? "border-[#2563EB]/40 bg-[#EFF6FF]" : "border-[#E2E8F0] hover:bg-[#F8FAFC]")
                    }
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleColumn(column.key)}
                      className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                    />
                    <span className="truncate text-[13px] font-semibold text-[#0F172A]">{column.label}</span>
                  </label>
                );
              })}
            </div>
          </section>

          {error && (
            <p className="mt-4 rounded-lg bg-[#FEF2F2] px-3 py-2 text-[12px] font-semibold text-[#DC2626]">
              {error}
            </p>
          )}
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-[#E2E8F0] bg-[#F8FAFC] px-6 py-4">
          <span className="text-[12px] font-semibold text-[#94A3B8]">
            {busy
              ? progress
              : format === "timesheet"
                ? `CSV \u00b7 ${datesInRange(range.from, range.to).length} day columns`
                : activeColumns.length
                  ? "Format: CSV"
                  : "Pick at least one column"}
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              disabled={busy}
              className="rounded-lg border border-[#E2E8F0] bg-white px-4 py-2 text-[13px] font-bold text-[#64748B] transition hover:text-[#0F172A] disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              onClick={handleExport}
              disabled={busy || (format === "report" && !activeColumns.length)}
              className="flex items-center gap-1.5 rounded-lg bg-[#0F172A] px-4 py-2 text-[13px] font-bold text-white shadow-sm transition hover:bg-[#1E293B] disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busy ? (
                <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
              ) : (
                <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" />
                </svg>
              )}
              {busy ? "Exporting…" : "Download CSV"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
};
