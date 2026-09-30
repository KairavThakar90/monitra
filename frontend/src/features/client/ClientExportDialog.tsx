import React, { useEffect, useState } from 'react';
import type { DateRange } from '../dashboard/v2/filters';
import { exportToCsv } from '../dashboard/v2/filters';
import { useGetMyTimesheetQuery } from '../../store/api/clientPortalApi';
import type { DetailedLogItem } from '../../store/api/reportsApi';
import { IST_TIME_ZONE } from '../../utils/duration';
import {
  buildTimesheetRows,
  datesInRange,
  timesheetBody,
  timesheetHeaders,
} from '../dashboard/v2/timesheetExport';

/**
 * The client portal's export dialog.
 *
 * The same file the administrator's Reports page writes: a **timesheet** --
 * one row per member x project x to-do, one column per day in the range, and a
 * row total -- so a client and an administrator read one format. It is built
 * from the portal's own endpoint, `/clients/me/timesheet`, which is limited to
 * the projects shared with this client and to the range and filters of the
 * page the Export button was pressed on.
 *
 * Whatever the administrator did not share is withheld, never invented: with
 * Timing off there is no time to report and the dialog says so; a member or a
 * to-do that was not shared is written as "Not shared" (rows keep their
 * opaque member id, so two people are never merged into one).
 */

const NOT_SHARED = 'Not shared';

export const ClientExportDialog: React.FC<{
  open: boolean;
  onClose: () => void;
  range: DateRange;
  selectedProjectIds: string[];
  selectedMemberIds: string[];
  // Still passed by the pages; the file no longer depends on them.
  defaultReport?: string;
  allProjects?: { id: number; project_name: string }[];
  allMembers?: { id: number; name: string }[];
}> = ({ open, onClose, range, selectedProjectIds, selectedMemberIds }) => {
  const [error, setError] = useState<string | null>(null);

  // A shared cache key with nothing else on the page, and skipped while the
  // dialog is closed, so an unopened dialog costs no request.
  const { data, isFetching, isError } = useGetMyTimesheetQuery(
    {
      start_date: range.from,
      end_date: range.to,
      project_ids: selectedProjectIds.map(Number),
      member_ids: selectedMemberIds.map(Number),
    },
    { skip: !open },
  );

  useEffect(() => {
    if (!open) setError(null);
  }, [open]);

  if (!open) return null;

  const dayCount = datesInRange(range.from, range.to).length;
  const timingShared = data?.permissions.share_timing ?? true;

  const handleExport = () => {
    setError(null);
    if (!data) return;
    if (!data.permissions.share_timing) {
      setError('Time tracking is not shared with your account, so there is no time to export.');
      return;
    }
    if (!data.items.length) {
      setError('No tracked time matches these filters, so there is nothing to export.');
      return;
    }

    // The staff timesheet builder reads the detailed-log shape; the portal's
    // rows carry exactly the fields it uses. Withheld names become the same
    // "Not shared" the rest of the portal's exports print.
    const logs = data.items.map(
      (item) =>
        ({
          id: '',
          date: item.date,
          member_id: item.member_id,
          member_name: item.member_name ?? NOT_SHARED,
          project_id: item.project_id,
          project_name: item.project_name,
          task_id: item.task_id,
          task_name: item.task_name ?? NOT_SHARED,
          tracked_seconds: item.tracked_seconds,
          tracked_hours: item.tracked_seconds / 3600,
          tracked_time: '',
        }) as DetailedLogItem,
    );

    const dates = datesInRange(range.from, range.to);
    exportToCsv(
      `timesheet_report_${range.from}_to_${range.to}.csv`,
      timesheetHeaders(dates),
      timesheetBody(buildTimesheetRows(logs), dates, data.organization ?? '', IST_TIME_ZONE),
      [],
      // Quoted throughout, exactly as the administrator's timesheet is.
      true,
    );
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-[#0F172A]/40 backdrop-blur-[2px]" onClick={onClose} />
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
              Downloads every row matching the page&rsquo;s current filters.
            </p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="rounded-lg p-1.5 text-[#94A3B8] transition hover:bg-[#F1F5F9] hover:text-[#0F172A]"
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

          {!timingShared && (
            <p className="mt-4 rounded-lg bg-[#FFFBEB] px-3 py-2 text-[12px] font-semibold text-[#B45309]">
              Time tracking is not shared with your account. Ask your admin to enable it to export a timesheet.
            </p>
          )}
          {isError && (
            <p className="mt-4 rounded-lg bg-[#FEF2F2] px-3 py-2 text-[12px] font-semibold text-[#DC2626]">
              The timesheet could not be loaded. Please try again.
            </p>
          )}
          {error && (
            <p className="mt-4 rounded-lg bg-[#FEF2F2] px-3 py-2 text-[12px] font-semibold text-[#DC2626]">{error}</p>
          )}
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-[#E2E8F0] bg-[#F8FAFC] px-6 py-4">
          <span className="text-[12px] font-semibold text-[#94A3B8]">
            {isFetching ? 'Loading…' : `CSV · ${dayCount} day column${dayCount === 1 ? '' : 's'}`}
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              className="rounded-lg border border-[#E2E8F0] bg-white px-4 py-2 text-[13px] font-bold text-[#64748B] transition hover:text-[#0F172A]"
            >
              Cancel
            </button>
            <button
              onClick={handleExport}
              disabled={isFetching || !data || !timingShared}
              className="flex items-center gap-1.5 rounded-lg bg-[#0F172A] px-4 py-2 text-[13px] font-bold text-white shadow-sm transition hover:bg-[#1E293B] disabled:cursor-not-allowed disabled:opacity-40"
            >
              <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="2.5"
                  d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3"
                />
              </svg>
              Download CSV
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
};
