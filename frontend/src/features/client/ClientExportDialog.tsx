import React, { useEffect, useState } from 'react';
import type { DateRange } from '../dashboard/v2/filters';
import { exportToCsv } from '../dashboard/v2/filters';
import {
  useGetMyMemberHoursQuery,
  useGetMyProjectsQuery,
  useGetMyTaskHoursQuery,
  type MyMemberHours,
  type MyProjectSummary,
  type MyTaskHours,
} from '../../store/api/clientPortalApi';
import { formatSharedHMS } from './clientRange';

/**
 * The client portal's export dialog.
 *
 * Same dressing as the admin/member Reports page's `ExportDialog` — a report
 * choice, a column checklist, the same footer — built from the client
 * portal's own (already-filtered, unpaginated) endpoints. People appear by
 * *name* in the file, never as bare counts or ids, and every figure the
 * admin has not shared exports as "Not shared" rather than a fabricated 0.
 */

type ClientReportId = 'projects' | 'members' | 'tasks';

const REPORT_META: Record<ClientReportId, { title: string }> = {
  projects: { title: 'Projects' },
  members: { title: 'Members' },
  tasks: { title: 'Tasks' },
};

/** What a column needs beyond its own row to compute a value. */
interface ExportContext {
  memberDetailsShared: boolean;
}

interface ColumnDef<T> {
  key: string;
  label: string;
  value: (row: T, index: number, ctx: ExportContext) => string | number;
  optional?: boolean;
}

const hoursOr = (hours: number | null | undefined, absent: string): string | number =>
  hours === null || hours === undefined ? absent : hours;

/** Exactly the format the client asked for -- five columns, nothing else:
 * name, description, created date, when its tasks were first started, and
 * the assigned members by name. */
const PROJECT_COLUMNS: ColumnDef<MyProjectSummary>[] = [
  { key: 'name', label: 'Project Name', value: (row) => row.project_name },
  { key: 'description', label: 'Project Description', value: (row) => row.description || '—' },
  { key: 'created', label: 'Project Created Date', value: (row) => row.created_date ?? '—' },
  {
    key: 'taskStarted',
    label: 'Project Task Started Date',
    value: (row) =>
      row.first_tracked_date ?? (row.total_tracked_hours === null ? 'Not shared' : 'Not started yet'),
  },
  {
    key: 'members',
    label: 'Members',
    value: (row, _index, ctx) =>
      !ctx.memberDetailsShared
        ? 'Not shared'
        : (row.members ?? []).length
          ? (row.members ?? []).map((member) => member.name).join('; ')
          : '—',
  },
];

const MEMBER_COLUMNS: ColumnDef<MyMemberHours>[] = [
  { key: 'name', label: 'Member', value: (row) => row.name },
  { key: 'designation', label: 'Designation', value: (row) => row.designation ?? '—' },
  { key: 'hours', label: 'Project Hours', value: (row) => hoursOr(row.total_tracked_hours, 'Not shared') },
  {
    key: 'time',
    label: 'Total Time (HH:MM:SS)',
    value: (row) => formatSharedHMS(row.total_tracked_seconds),
    optional: true,
  },
  {
    key: 'projects',
    label: 'Projects',
    value: (row) => ((row.project_names ?? []).length ? (row.project_names ?? []).join('; ') : '—'),
    optional: true,
  },
];

const TASK_COLUMNS: ColumnDef<MyTaskHours>[] = [
  { key: 'name', label: 'Task', value: (row) => row.task_name },
  { key: 'project', label: 'Project', value: (row) => row.project_name ?? 'Unknown project', optional: true },
  { key: 'hours', label: 'Task Hours', value: (row) => hoursOr(row.total_tracked_hours, 'Not shared') },
  {
    key: 'member',
    label: 'Member',
    value: (row, _index, ctx) => (!ctx.memberDetailsShared ? 'Not shared' : row.assignee ?? '—'),
  },
  { key: 'date', label: 'Date', value: (row) => row.created_date ?? '—' },
  {
    key: 'activity',
    label: 'Activity %',
    // Null means either Timing withheld (hours are null too) or the timer
    // recorded no samples in range — told apart honestly, never a made-up 0.
    value: (row) =>
      row.activity_percentage ?? (row.total_tracked_hours === null ? 'Not shared' : '—'),
  },
  {
    key: 'time',
    label: 'Total Time (HH:MM:SS)',
    value: (row) => formatSharedHMS(row.total_tracked_seconds),
    optional: true,
  },
];

export const ClientExportDialog: React.FC<{
  open: boolean;
  onClose: () => void;
  defaultReport: ClientReportId;
  range: DateRange;
  selectedProjectIds: string[];
  selectedMemberIds: string[];
  allProjects: { id: number; project_name: string }[];
  allMembers: { id: number; name: string }[];
}> = ({ open, onClose, defaultReport, range, selectedProjectIds, selectedMemberIds }) => {
  // No report chooser: each page's Export button exports that page's own
  // report, so the dialog is just columns + download.
  const report = defaultReport;
  const [selectedColumns, setSelectedColumns] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const dateArgs = { start_date: range.from, end_date: range.to };
  const projectIds = selectedProjectIds.map(Number);
  const memberIds = selectedMemberIds.map(Number);

  // These share a cache key with whatever the calling page already fetched,
  // so opening the dialog is instant rather than a fresh round trip.
  const { data: projectData, isFetching: projectsFetching } = useGetMyProjectsQuery(dateArgs, { skip: !open });
  const { data: memberData, isFetching: membersFetching } = useGetMyMemberHoursQuery(
    { ...dateArgs, project_ids: projectIds },
    { skip: !open },
  );
  const { data: taskData, isFetching: tasksFetching } = useGetMyTaskHoursQuery(
    { ...dateArgs, project_ids: projectIds, member_ids: memberIds },
    { skip: !open },
  );
  const columns = report === 'projects' ? PROJECT_COLUMNS : report === 'members' ? MEMBER_COLUMNS : TASK_COLUMNS;
  const isFetching = report === 'projects' ? projectsFetching : report === 'members' ? membersFetching : tasksFetching;

  useEffect(() => {
    setSelectedColumns(columns.filter((c) => !c.optional).map((c) => c.key));
  }, [columns]);

  useEffect(() => {
    if (!open) setError(null);
  }, [open]);

  if (!open) return null;

  const toggleColumn = (key: string) =>
    setSelectedColumns((current) => (current.includes(key) ? current.filter((k) => k !== key) : [...current, key]));

  const activeColumns = columns.filter((c) => selectedColumns.includes(c.key));

  const handleExport = () => {
    setError(null);
    const rows: (MyProjectSummary | MyMemberHours | MyTaskHours)[] =
      report === 'projects' ? projectData?.items ?? [] : report === 'members' ? memberData?.items ?? [] : taskData?.items ?? [];

    if (!activeColumns.length) return;
    if (!rows.length) {
      setError('No rows match these filters, so there is nothing to export.');
      return;
    }

    const ctx: ExportContext = {
      memberDetailsShared:
        (report === 'projects' ? projectData : report === 'members' ? memberData : taskData)?.permissions
          .share_member_details ?? false,
    };

    const headers = activeColumns.map((c) => c.label);
    // Every column def is typed against its own row shape; the row list here
    // is exactly that shape because `columns` and `rows` are switched on the
    // same `report` value together.
    const body = rows.map((row, index) => activeColumns.map((column) => (column.value as any)(row, index, ctx)));

    exportToCsv(`client-${report}-report_${range.from}_to_${range.to}.csv`, headers, body, []);
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
            <h2 className="text-[16px] font-bold tracking-tight text-[#0F172A]">
              Export {REPORT_META[report].title}
            </h2>
            <p className="mt-0.5 text-[12px] text-[#94A3B8]">
              Downloads every row matching the page&rsquo;s current filters, in the format you choose.
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
                      'flex cursor-pointer items-center gap-2.5 rounded-lg border px-3 py-2 transition ' +
                      (checked ? 'border-[#2563EB]/40 bg-[#EFF6FF]' : 'border-[#E2E8F0] hover:bg-[#F8FAFC]')
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
            <p className="mt-4 rounded-lg bg-[#FEF2F2] px-3 py-2 text-[12px] font-semibold text-[#DC2626]">{error}</p>
          )}
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-[#E2E8F0] bg-[#F8FAFC] px-6 py-4">
          <span className="text-[12px] font-semibold text-[#94A3B8]">
            {isFetching ? 'Loading…' : activeColumns.length ? 'Format: CSV' : 'Pick at least one column'}
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
              disabled={!activeColumns.length || isFetching}
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
