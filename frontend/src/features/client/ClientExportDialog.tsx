import React, { useEffect, useMemo, useState } from 'react';
import type { DateRange } from '../dashboard/v2/filters';
import { exportToCsv } from '../dashboard/v2/filters';
import { formatHMS } from '../../utils/duration';
import {
  useGetMyMemberHoursQuery,
  useGetMyProjectsQuery,
  useGetMyTaskHoursQuery,
  type MyMemberHours,
  type MyProjectSummary,
  type MyTaskHours,
} from '../../store/api/clientPortalApi';

/**
 * The client portal's export dialog.
 *
 * Deliberately the same shape as the admin/member Reports page's
 * `ExportDialog` — a format choice, a filter-chip summary, a column
 * checklist, an "include filter summary" toggle, the same footer — so a
 * client sees the same export experience Monitra already offers everywhere
 * else, built from the client-portal's own (already-filtered, unpaginated)
 * endpoints rather than the staff reports pipeline.
 */

type ClientReportId = 'projects' | 'members' | 'tasks';

const REPORT_META: Record<ClientReportId, { title: string; dimensionLabel: string }> = {
  projects: { title: 'Projects', dimensionLabel: 'Project' },
  members: { title: 'Members', dimensionLabel: 'Member' },
  tasks: { title: 'Tasks', dimensionLabel: 'Task' },
};

interface ColumnDef<T> {
  key: string;
  label: string;
  value: (row: T, index: number, totalSeconds: number) => string | number;
  optional?: boolean;
}

const PROJECT_COLUMNS: ColumnDef<MyProjectSummary>[] = [
  { key: 'rank', label: 'Sr. No.', value: (_row, index) => index + 1 },
  { key: 'name', label: 'Project', value: (row) => row.project_name },
  { key: 'id', label: 'Project ID', value: (row) => row.id, optional: true },
  { key: 'status', label: 'Status', value: (row) => row.status },
  { key: 'time', label: 'Total Time (HH:MM:SS)', value: (row) => formatHMS(row.total_tracked_seconds) },
  { key: 'hours', label: 'Total Hours', value: (row) => row.total_tracked_hours },
  {
    key: 'share',
    label: '% of Total',
    value: (row, _index, total) => (total > 0 ? ((row.total_tracked_seconds / total) * 100).toFixed(2) : 0),
  },
  { key: 'members', label: 'Members', value: (row) => row.member_count },
];

const MEMBER_COLUMNS: ColumnDef<MyMemberHours>[] = [
  { key: 'rank', label: 'Sr. No.', value: (_row, index) => index + 1 },
  { key: 'name', label: 'Member', value: (row) => row.name },
  { key: 'id', label: 'Member ID', value: (row) => row.id, optional: true },
  { key: 'time', label: 'Total Time (HH:MM:SS)', value: (row) => formatHMS(row.total_tracked_seconds) },
  { key: 'hours', label: 'Total Hours', value: (row) => row.total_tracked_hours },
  {
    key: 'share',
    label: '% of Total',
    value: (row, _index, total) => (total > 0 ? ((row.total_tracked_seconds / total) * 100).toFixed(2) : 0),
  },
  { key: 'projects', label: 'Projects', value: (row) => row.project_count },
];

const TASK_COLUMNS: ColumnDef<MyTaskHours>[] = [
  { key: 'rank', label: 'Sr. No.', value: (_row, index) => index + 1 },
  { key: 'name', label: 'Task', value: (row) => row.task_name },
  { key: 'id', label: 'Task ID', value: (row) => row.id, optional: true },
  { key: 'project', label: 'Project', value: (row) => row.project_name ?? 'Unknown project' },
  { key: 'time', label: 'Total Time (HH:MM:SS)', value: (row) => formatHMS(row.total_tracked_seconds) },
  { key: 'hours', label: 'Total Hours', value: (row) => row.total_tracked_hours },
  {
    key: 'share',
    label: '% of Total',
    value: (row, _index, total) => (total > 0 ? ((row.total_tracked_seconds / total) * 100).toFixed(2) : 0),
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
}> = ({ open, onClose, defaultReport, range, selectedProjectIds, selectedMemberIds, allProjects, allMembers }) => {
  const [report, setReport] = useState<ClientReportId>(defaultReport);
  const [selectedColumns, setSelectedColumns] = useState<string[]>([]);
  const [includeFilterHeader, setIncludeFilterHeader] = useState(true);
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
    if (open) setReport(defaultReport);
  }, [open, defaultReport]);

  useEffect(() => {
    setSelectedColumns(columns.filter((c) => !c.optional).map((c) => c.key));
  }, [columns]);

  useEffect(() => {
    if (!open) setError(null);
  }, [open]);

  const projectNames = useMemo(
    () => allProjects.filter((p) => selectedProjectIds.includes(String(p.id))).map((p) => p.project_name),
    [allProjects, selectedProjectIds],
  );
  const memberNames = useMemo(
    () => allMembers.filter((m) => selectedMemberIds.includes(String(m.id))).map((m) => m.name),
    [allMembers, selectedMemberIds],
  );
  const projectsLabel = projectNames.length ? projectNames.join('; ') : 'All shared projects';
  const membersLabel = memberNames.length ? memberNames.join('; ') : 'All members';

  if (!open) return null;

  const toggleColumn = (key: string) =>
    setSelectedColumns((current) => (current.includes(key) ? current.filter((k) => k !== key) : [...current, key]));

  const activeColumns = columns.filter((c) => selectedColumns.includes(c.key));

  const chip = (text: string) => (
    <span key={text} className="rounded-md bg-[#EFF6FF] px-2 py-1 text-[11px] font-bold text-[#2563EB]">
      {text}
    </span>
  );

  const handleExport = () => {
    setError(null);
    const rows: (MyProjectSummary | MyMemberHours | MyTaskHours)[] =
      report === 'projects' ? projectData?.items ?? [] : report === 'members' ? memberData?.items ?? [] : taskData?.items ?? [];

    if (!activeColumns.length) return;
    if (!rows.length) {
      setError('No rows match these filters, so there is nothing to export.');
      return;
    }

    const totalSeconds = rows.reduce((sum, row) => sum + row.total_tracked_seconds, 0);
    const headers = activeColumns.map((c) => c.label);
    // Every column def is typed against its own row shape; the row list here
    // is exactly that shape because `columns` and `rows` are switched on the
    // same `report` value together.
    const body = rows.map((row, index) => activeColumns.map((column) => (column.value as any)(row, index, totalSeconds)));

    const filterLines: (string | number)[][] = includeFilterHeader
      ? [
          ['Report', REPORT_META[report].title],
          ['Date range', `${range.from} to ${range.to}`],
          ['Projects', projectsLabel],
          ['Members', membersLabel],
          ['Rows', rows.length],
          ['Generated', new Date().toLocaleString('en-GB')],
          [],
        ]
      : [];

    exportToCsv(`client-${report}-report_${range.from}_to_${range.to}.csv`, headers, body, filterLines);
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
              Downloads every row matching the filters below, in the format you choose.
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
          <section className="mb-6">
            <h3 className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">Report</h3>
            <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
              {(Object.keys(REPORT_META) as ClientReportId[]).map((id) => {
                const active = report === id;
                return (
                  <button
                    key={id}
                    type="button"
                    onClick={() => setReport(id)}
                    className={
                      'rounded-lg border px-3 py-2.5 text-left transition ' +
                      (active ? 'border-[#2563EB]/40 bg-[#EFF6FF]' : 'border-[#E2E8F0] hover:bg-[#F8FAFC]')
                    }
                  >
                    <span className="block truncate text-[13px] font-bold text-[#0F172A]">{REPORT_META[id].title}</span>
                  </button>
                );
              })}
            </div>
          </section>

          <section>
            <h3 className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">Applied filters</h3>
            <dl className="mt-3 space-y-2.5">
              <div className="flex items-start gap-3">
                <dt className="w-20 shrink-0 pt-1 text-[12px] font-semibold text-[#94A3B8]">Dates</dt>
                <dd className="flex flex-wrap gap-1.5">{chip(`${range.from} → ${range.to}`)}</dd>
              </div>
              <div className="flex items-start gap-3">
                <dt className="w-20 shrink-0 pt-1 text-[12px] font-semibold text-[#94A3B8]">Projects</dt>
                <dd className="flex flex-wrap gap-1.5">
                  {projectNames.length ? projectNames.map(chip) : chip('All shared projects')}
                </dd>
              </div>
              <div className="flex items-start gap-3">
                <dt className="w-20 shrink-0 pt-1 text-[12px] font-semibold text-[#94A3B8]">Members</dt>
                <dd className="flex flex-wrap gap-1.5">{memberNames.length ? memberNames.map(chip) : chip('All members')}</dd>
              </div>
            </dl>
          </section>

          <section className="mt-6">
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

          <label className="mt-5 flex cursor-pointer items-start gap-2.5">
            <input
              type="checkbox"
              checked={includeFilterHeader}
              onChange={(e) => setIncludeFilterHeader(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
            />
            <span className="text-[13px] font-semibold text-[#0F172A]">
              Include the filter summary at the top of the file
              <span className="block text-[11px] font-normal text-[#94A3B8]">
                So the spreadsheet records which range, projects and members it covers.
              </span>
            </span>
          </label>

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
