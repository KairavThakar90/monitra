import React, { useEffect, useState } from 'react';
import { useLazyGetProjectsQuery, type GetProjectsArgs, type Project } from '../../store/api/projectsApi';
import { buildProjectRows, EXPORT_COLUMNS, type ExportColumnKey } from './projectExport';
import { exportToCsv } from '../dashboard/v2/filters';
import { istTodayISO } from '../../utils/duration';
import { formatApiError } from '../../api/utils';

/**
 * Export dialog for the Project Management page, styled as the Reports page's
 * export dialog is (`features/dashboard/v2/ExportDialog`).
 *
 * Like that one it does not export what is on screen: the table holds a single
 * page, so the file is built by asking the API for the same filters the page
 * sends and walking every page. The walk happens only when Download is
 * pressed -- opening the dialog, or loading the page, costs nothing extra.
 */

/** The API's page size ceiling for projects. */
const PAGE_LIMIT = 100;
/** Stops the walk if the server ever reports an inconsistent `total_pages`. */
const MAX_PAGES = 500;

export const ProjectExportDialog: React.FC<{
  open: boolean;
  onClose: () => void;
  /** The filters exactly as the page sends them for the table. */
  filters: Omit<GetProjectsArgs, 'page' | 'limit'>;
}> = ({ open, onClose, filters }) => {
  const [columns, setColumns] = useState<ExportColumnKey[]>(EXPORT_COLUMNS.map((column) => column.key));
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [fetchProjects] = useLazyGetProjectsQuery();

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
      if (event.key === 'Escape' && !busy) onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, busy, onClose]);

  if (!open) return null;

  const allSelected = columns.length === EXPORT_COLUMNS.length;
  const toggle = (key: ExportColumnKey) =>
    setColumns((current) => (current.includes(key) ? current.filter((item) => item !== key) : [...current, key]));

  /** Every page of the projects the filters match. */
  const fetchAllProjects = async (): Promise<Project[]> => {
    const all: Project[] = [];
    let page = 1;
    for (;;) {
      setProgress(`Fetching page ${page}…`);
      const response = await fetchProjects({ ...filters, page, limit: PAGE_LIMIT }).unwrap();
      all.push(...(response.items || []));
      const lastPage = Math.max(1, response.pagination?.total_pages || 1);
      if (page >= lastPage || !response.items?.length || page >= MAX_PAGES) break;
      page += 1;
    }
    return all;
  };

  const handleExport = async () => {
    if (busy || columns.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const projects = await fetchAllProjects();
      if (!projects.length) {
        setError('No projects match the current filters, so there is nothing to export.');
        return;
      }
      setProgress('Building file…');
      // In the table's own column order, whatever order they were ticked in.
      const chosen = EXPORT_COLUMNS.filter((column) => columns.includes(column.key));
      // The header is the file's first line and the projects follow it, as in the
      // Reports export: nothing sits above the table, so the first screen of a
      // spreadsheet shows the data, and its filter/sort/import tools see a plain table.
      exportToCsv(
        `projects_${istTodayISO()}.csv`,
        chosen.map((column) => column.label),
        buildProjectRows(projects, chosen.map((column) => column.key)),
      );
      onClose();
    } catch (caught) {
      setError(formatApiError((caught as { data?: unknown })?.data, 'Export failed. Please check your connection and try again.'));
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
        aria-label="Export projects"
        className="relative flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-[#E2E8F0] bg-white shadow-2xl"
      >
        <header className="flex items-start justify-between gap-4 border-b border-[#E2E8F0] px-6 py-5">
          <div>
            <h2 className="text-[16px] font-bold tracking-tight text-[#0F172A]">Export projects</h2>
            <p className="mt-0.5 text-[12px] text-[#94A3B8]">
              Downloads every project matching the filters — not just what is on screen.
            </p>
          </div>
          <button
            type="button"
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
            <div className="flex items-center justify-between">
              <h3 className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">Columns</h3>
              <button
                type="button"
                onClick={() => setColumns(allSelected ? [] : EXPORT_COLUMNS.map((column) => column.key))}
                disabled={busy}
                className="text-[11px] font-bold text-[#2563EB] transition hover:text-[#1D4ED8] disabled:opacity-40"
              >
                {allSelected ? 'Clear all' : 'Select all'}
              </button>
            </div>
            <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
              {EXPORT_COLUMNS.map((column) => {
                const checked = columns.includes(column.key);
                return (
                  <label
                    key={column.key}
                    className={`flex cursor-pointer items-center gap-3 rounded-lg border px-3 py-2.5 transition ${
                      checked ? 'border-[#2563EB]/40 bg-[#EFF6FF]' : 'border-[#E2E8F0] bg-white hover:bg-[#F8FAFC]'
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={busy}
                      onChange={() => toggle(column.key)}
                      className="h-4 w-4 rounded border-[#CBD5E1] text-[#2563EB] focus:ring-[#2563EB]"
                    />
                    <span className="text-[13px] font-bold text-[#0F172A]">{column.label}</span>
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
            {busy ? progress : `CSV · ${columns.length} column${columns.length === 1 ? '' : 's'}`}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="rounded-lg border border-[#E2E8F0] bg-white px-4 py-2 text-[13px] font-bold text-[#64748B] transition hover:text-[#0F172A] disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleExport}
              disabled={busy || columns.length === 0}
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
              {busy ? 'Exporting…' : 'Download CSV'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
};
