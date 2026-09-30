import React, { useEffect, useState } from "react";
import type { Member } from "../../../store/api/membersApi";
import type { Project } from "../../../store/api/projectsApi";
import type { DetailedLogItem, ReactReportQueryParams } from "../../../store/api/reportsApi";
import { useLazyGetDetailedLogsQuery } from "../../../store/api/reportsApi";
import { useGetMemberDetailsQuery } from "../../../store/api/membersApi";
import { useAuth } from "../../auth/authContext";
import { IST_TIME_ZONE } from "../../../utils/duration";
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
 * There is one file: the **timesheet** -- one row per member x project x
 * to-do, one column per day in the range, and a row total. It replaced a
 * second "ranked table" format, so an export always has the same shape
 * whichever report tab it is opened from; the client portal writes the same
 * file (`features/client/ClientExportDialog`).
 *
 * It does not export what is on screen. The page holds only its first rows,
 * so the file is built by re-querying `/reports/detailed-logs` -- the only
 * endpoint carrying (date, member, project, to-do) grain -- with the page's
 * own filters and walking every page (the backend caps `limit` at 200). The
 * timesheet therefore covers every member the filters allow, not just the
 * dimension of the open tab.
 */

/** The backend's hard ceiling on `limit` (see reports_page/router.py). */
const PAGE_LIMIT = 200;
/** Stops the walk if the server ever reports an inconsistent `pages`. */
const MAX_PAGES = 500;

export const ExportDialog: React.FC<{
  open: boolean;
  onClose: () => void;
  range: DateRange;
  /** The date/member/project filters exactly as the page sends them. */
  queryParams: ReactReportQueryParams;
  // Still passed by the page; the file no longer depends on them.
  reportId?: string;
  reportTitle?: string;
  dimensionLabel?: string;
  selectedMembers?: string[];
  selectedProjects?: string[];
  members?: Member[];
  projects?: Project[];
}> = ({ open, onClose, range, queryParams }) => {
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [fetchLogs] = useLazyGetDetailedLogsQuery();

  // Everyone in the export shares the caller's organization, so its name is
  // read once from the caller's own record rather than per exported member.
  // If the record does not carry one, the column is left empty -- an invented
  // organization name would be worse than an honest blank.
  const { currentUser } = useAuth();
  const { data: me } = useGetMemberDetailsQuery(
    { id: currentUser?.id as number },
    { skip: !open || !currentUser },
  );
  const organizationName = me?.member?.organization?.name ?? "";

  useEffect(() => {
    if (!open) {
      setBusy(false);
      setProgress(null);
      setError(null);
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
      setProgress(`Fetching page ${page}…`);
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

  const handleExport = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const logs = await fetchAllLogs();
      if (!logs.length) {
        setError("No tracked time matches these filters, so there is nothing to export.");
        return;
      }
      setProgress("Building file…");

      const dates = datesInRange(range.from, range.to);
      const rows = buildTimesheetRows(logs);

      exportToCsv(
        `timesheet_report_${range.from}_to_${range.to}.csv`,
        timesheetHeaders(dates),
        timesheetBody(rows, dates, organizationName, IST_TIME_ZONE),
        [],
        // Quoted throughout, matching the timesheet format this mirrors.
        true,
      );
      onClose();
    } catch (caught) {
      setError(
        (caught as { data?: { detail?: string } })?.data?.detail ||
          "Export failed. Please check your connection and try again.",
      );
    } finally {
      setBusy(false);
      setProgress(null);
    }
  };

  const dayCount = datesInRange(range.from, range.to).length;

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
              Downloads every row matching the filters — not just what is on screen.
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
          <section>
            <h3 className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">Format</h3>
            <div className="mt-3 rounded-lg border border-[#2563EB]/40 bg-[#EFF6FF] px-3 py-2.5">
              <span className="block text-[13px] font-bold text-[#0F172A]">Timesheet</span>
              <span className="mt-0.5 block text-[11px] font-medium text-[#64748B]">
                Every member x project x to-do, one column per day.
              </span>
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
            {busy ? progress : `CSV · ${dayCount} day column${dayCount === 1 ? "" : "s"}`}
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
              disabled={busy}
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
