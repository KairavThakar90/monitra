import React, { useState, useEffect, useMemo, useRef } from "react";
import { V2Shell } from "../dashboard/v2/V2Shell";
import { useGetProjectTaskSummaryQuery } from "../../store/api/reportsApi";
import {
  useGetAllProjectsQuery,
  useGetProjectMetadataQuery,
  useGetAssignableEmployeesQuery,
  useCreateTaskMutation,
  useUpdateTaskMutation
} from "../../store/api/projectsApi";
import type { ProjectTaskSummaryProject, ProjectTaskSummaryTask } from "../../store/api/reportsApi";
import { useFeedback } from "../../components/FeedbackProvider";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { formatHMS } from "../../utils/duration";
import { PaginationArrow } from '../../components/PaginationArrow';
import { DateRangeFilter, ProjectMultiSelect, rangeFor, DEFAULT_RANGE, type DateRange } from '../dashboard/v2/filters';
import { FieldError, useFormValidation } from '../../validation';
import { useAuth } from '../auth/authContext';
import { usageColor } from '../dashboard/v2/theme';
import { billingTypeLabel, billingTypesFor, type BillingKind, type BillingScope } from '../../utils/billing';

const formatDate = (dateStr: string | null) => {
  if (!dateStr) return "-";
  const date = new Date(dateStr);
  const day = date.getDate().toString().padStart(2, "0");
  const month = date.toLocaleString("en-US", { month: "short" });
  const year = date.getFullYear();
  return `${day} ${month} ${year}`;
};

const Pagination: React.FC<{
  page: number;
  totalPages: number;
  totalItems: number;
  limit: number;
  setPage: (page: number) => void;
  setLimit: (limit: number) => void;
}> = ({ page, totalPages, totalItems, limit, setPage, setLimit }) => {
  const startItem = totalItems === 0 ? 0 : (page - 1) * limit + 1;
  const endItem = Math.min(page * limit, totalItems);
  const pages = totalPages <= 7
    ? Array.from({ length: totalPages }, (_, index) => index + 1)
    : page <= 4
      ? [1, 2, 3, 4, 5, '...', totalPages]
      : page >= totalPages - 3
        ? [1, '...', totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1, totalPages]
        : [1, '...', page - 1, page, page + 1, '...', totalPages];

  return (
    <div className="mt-8 flex flex-col items-center justify-between gap-4 border-t border-slate-200 pt-5 text-sm text-slate-500 sm:flex-row">
      <div>Showing {startItem} to {endItem} of {totalItems} projects</div>
      <div className="flex items-center gap-3">
        <select value={limit} onChange={(e) => { setLimit(Number(e.target.value)); setPage(1); }} className="rounded-md border border-slate-300 py-1.5 pl-3 pr-8 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500">
          <option value={5}>5</option>
          <option value={10}>10</option>
          <option value={20}>20</option>
          <option value={50}>50</option>
        </select>
        <div className="flex items-center gap-1">
          <PaginationArrow direction="prev" disabled={page === 1} onClick={() => setPage(page - 1)} />
          {pages.map((visiblePage, index) => visiblePage === '...' ? (
            <span key={`ellipsis-${index}`} className="flex h-8 w-8 items-center justify-center text-slate-400">...</span>
          ) : (
            <button key={visiblePage} onClick={() => setPage(visiblePage as number)} className={`flex h-8 w-8 items-center justify-center rounded text-sm font-semibold transition ${visiblePage === page ? 'bg-blue-500 text-white shadow-sm' : 'text-slate-600 hover:bg-slate-100'}`}>{visiblePage}</button>
          ))}
          <PaginationArrow direction="next" disabled={page === totalPages} onClick={() => setPage(page + 1)} />
        </div>
      </div>
    </div>
  );
};

/**
 * The searchable single-select behind the Create Task drawer's Project field.
 *
 * Exported, with its wording overridable, so the Assign Task dialog's Project
 * and Task fields are the very same control rather than a lookalike. Every
 * prop beyond the first three is optional and defaults to this drawer's
 * behaviour. `projects[].project_name` is just "the label" here -- the Task
 * field passes task names through it.
 */
export const ProjectPicker: React.FC<{
  projects: { id: number; project_name: string }[];
  value: number | "";
  onChange: (value: number | "") => void;
  placeholder?: string;
  searchPlaceholder?: string;
  /** The "clear" row at the top. `null` removes it: a required field has no "all". */
  allLabel?: string | null;
  emptyText?: string;
  disabled?: boolean;
}> = ({
  projects, value, onChange,
  placeholder = "Select Project",
  searchPlaceholder = "Search projects...",
  allLabel = "All projects",
  emptyText = "No projects found.",
  disabled = false,
}) => {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const pickerRef = useRef<HTMLDivElement | null>(null);
  const selectedProject = projects.find((project) => project.id === value);
  const filteredProjects = useMemo(
    () => projects.filter((project) => project.project_name.toLowerCase().includes(query.trim().toLowerCase())),
    [projects, query]
  );

  useEffect(() => {
    if (!open) return;
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    return () => document.removeEventListener("mousedown", closeOnOutsideClick);
  }, [open]);

  return (
    <div ref={pickerRef} className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        className={
          "flex w-full items-center justify-between gap-3 rounded-lg border bg-white px-4 py-3 text-left text-sm font-semibold outline-none transition disabled:cursor-not-allowed disabled:bg-slate-50 " +
          (open ? "border-[#3B82F6] ring-2 ring-[#3B82F6]/15" : "border-slate-200 hover:border-slate-300")
        }
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className={selectedProject ? "truncate text-slate-700" : "text-slate-400"}>
          {selectedProject?.project_name || placeholder}
        </span>
        <svg className={`h-4 w-4 shrink-0 text-slate-400 transition-transform ${open ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="absolute left-0 right-0 top-full z-30 mt-2 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl">
          <div className="border-b border-slate-100 p-2.5">
            <input
              autoFocus
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={searchPlaceholder}
              className="w-full rounded-lg bg-slate-50 px-3 py-2 text-sm font-semibold text-slate-700 outline-none placeholder:text-slate-400 focus:ring-2 focus:ring-blue-500/20"
            />
          </div>
          <div className="max-h-64 overflow-y-auto p-1.5" role="listbox">
            {allLabel !== null && (
              <button
                type="button"
                onClick={() => { onChange(""); setOpen(false); setQuery(""); }}
                className={`w-full rounded-lg px-3 py-2 text-left text-sm transition hover:bg-slate-50 ${value === "" ? "font-bold text-blue-600" : "font-semibold text-slate-500"}`}
              >
                {allLabel}
              </button>
            )}
            {filteredProjects.length > 0 ? filteredProjects.map((project) => (
              <button
                type="button"
                key={project.id}
                role="option"
                aria-selected={project.id === value}
                onClick={() => { onChange(project.id); setOpen(false); setQuery(""); }}
                className={`block w-full truncate rounded-lg px-3 py-2 text-left text-sm transition hover:bg-slate-50 ${project.id === value ? "bg-blue-50 font-bold text-blue-700" : "font-semibold text-slate-700"}`}
                title={project.project_name}
              >
                {project.project_name}
              </button>
            )) : (
              <p className="px-3 py-5 text-center text-xs font-semibold text-slate-400">{emptyText}</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
};


/**
 * The task's budgeted hours, shown and edited in place on its row.
 *
 * The saved value is kept in local state after a successful PATCH rather
 * than re-fetching the whole task-summary report: the report takes the
 * better part of a second to answer, and its cache is not invalidated by
 * `updateTask` (see `patchTaskSummaries` for the same reasoning on create).
 */
const TaskBudgetCell: React.FC<{ projectId: number; task: ProjectTaskSummaryTask }> = ({ projectId, task }) => {
  const [updateTask, { isLoading: isSaving }] = useUpdateTaskMutation();
  const { showToast } = useFeedback();
  const [saved, setSaved] = useState<number | null>(task.estimated_hours ?? null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');

  const startEditing = () => {
    setDraft(saved === null ? '' : String(saved));
    setEditing(true);
  };

  const save = async () => {
    const trimmed = draft.trim();
    // An empty field clears the budget (sent as an explicit null).
    const value = trimmed === '' ? null : Number(trimmed);
    if (value !== null && (!Number.isFinite(value) || value < 0)) {
      showToast('Estimated hours must be a number of 0 or more.', 'error');
      return;
    }
    try {
      await updateTask({ projectId, taskId: task.id, body: { estimated_hours: value } }).unwrap();
      setSaved(value);
      setEditing(false);
    } catch (err: any) {
      showToast(err?.data?.detail || 'Could not save the estimated hours.', 'error');
    }
  };

  if (editing) {
    return (
      <div className="flex items-center gap-1.5">
        <input
          type="number"
          min={0}
          step={0.25}
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { e.preventDefault(); void save(); }
            if (e.key === 'Escape') setEditing(false);
          }}
          placeholder="hours"
          className="w-20 rounded-md border border-slate-200 px-2 py-1 text-xs font-semibold text-slate-700 outline-none focus:border-[#3B82F6]"
        />
        <button
          type="button"
          disabled={isSaving}
          onClick={() => void save()}
          className="rounded-md bg-[#3B82F6] px-2 py-1 text-xs font-bold text-white hover:bg-blue-600 disabled:opacity-50"
        >
          {isSaving ? '…' : 'Save'}
        </button>
        <button
          type="button"
          onClick={() => setEditing(false)}
          className="rounded-md px-2 py-1 text-xs font-bold text-slate-500 hover:bg-slate-100"
        >
          Cancel
        </button>
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={startEditing}
      title="Edit the task's estimated (budgeted) hours"
      className="rounded-md px-2 py-1 text-xs font-semibold text-slate-500 transition hover:bg-slate-100 hover:text-slate-700"
    >
      {saved === null ? 'Set budget' : `Budget: ${saved}h`}
    </button>
  );
};

/** The chip's colour per billing type, matching the Project Management table's badges. */
const BILLING_CHIP_COLOR: Record<string, string> = {
  fixed: '#8B5CF6',
  free: '#14B8A6',
  non_billing: '#64748B',
};

/** The shared look of the toolbar's native selects (the same as Project Management's billing filter). */
const FILTER_SELECT_CLASS =
  'min-h-[38px] w-full appearance-none rounded-lg border border-slate-200 bg-white py-2 pl-3 pr-9 text-sm font-semibold text-slate-700 shadow-sm outline-none transition hover:bg-slate-50 focus:border-[#38bdf8] focus:ring-2 focus:ring-[#38bdf8]/15 sm:w-auto';

const SelectChevron: React.FC = () => (
  <svg
    className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-600"
    fill="none"
    viewBox="0 0 24 24"
    stroke="currentColor"
  >
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
  </svg>
);

/**
 * How much of a fixed-hours project's budget has been spent, coloured by the
 * dashboard's own bands (blue under 80%, yellow 80-99%, green exactly on
 * budget, red over). The figures are the server's, from the same calculation
 * the dashboard uses, so a project reads the same here as there. Only a fixed
 * project with a budget has one; flexible and non-billing projects have nothing
 * to measure and show no meter.
 */
const BudgetUsage: React.FC<{ project: ProjectTaskSummaryProject }> = ({ project }) => {
  // `== null` on purpose: a backend that predates these fields leaves them
  // undefined, and that must read as "no budget to show", never as NaN%.
  if (project.usage_percentage == null || project.fixed_hours == null || project.used_seconds == null) {
    return null;
  }
  const pct = project.usage_percentage;
  const color = usageColor(pct);
  const over = project.remaining_seconds !== null && project.remaining_seconds < 0;
  return (
    <div
      className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1"
      data-testid="budget-usage"
      title="Hours spent so far, all time, against this project's fixed hours. Internal tasks do not count."
    >
      <span className="text-[11px] font-bold" style={{ color }} data-testid="budget-usage-hours">
        Used {formatHMS(project.used_seconds)} of {Number(project.fixed_hours)}h
      </span>
      {/* The whole track carries the band colour as a light tint, so the state reads at
          a glance even at 0%; the solid fill is the true usage and never grows past it. */}
      <div className="h-1.5 w-32 overflow-hidden rounded-full" style={{ backgroundColor: `${color}33` }}>
        <div
          className="h-full rounded-full transition-all duration-500 ease-out"
          style={{ width: `${Math.min(Math.max(pct, 0), 100)}%`, backgroundColor: color }}
        />
      </div>
      <span className="text-[11px] font-black" style={{ color }} data-testid="budget-usage-percent">
        {pct.toFixed(0)}%
      </span>
      {over && project.remaining_seconds !== null && (
        <span className="text-[11px] font-bold" style={{ color }}>
          Over by {formatHMS(-project.remaining_seconds)}
        </span>
      )}
    </div>
  );
};

export const AdminTaskListing: React.FC = () => {
  const [page, setPage] = useState(1);
  const [limit, setLimit] = useState(10);
  
  // Defaults to Today, not All Time: the task list itself always shows only
  // what's active today (the backend narrows it regardless of this filter),
  // so the displayed hour totals should match that on first load rather than
  // reading "All Time" next to a list that is quietly scoped to today.
  const [dateRange, setDateRange] = useState<DateRange>(() => rangeFor('today', DEFAULT_RANGE));
  const startDate = dateRange.from;
  const endDate = dateRange.to;
  
  const [filterProjectIds, setFilterProjectIds] = useState<string[]>([]);
  // Project type, in two steps like the Create Project form: Billing or Non
  // Billing first, then (under Billing only) Fixed Hours or Flexible Time.
  const [billingScope, setBillingScope] = useState<BillingScope>('');
  const [billingKind, setBillingKind] = useState<BillingKind>('');
  const billingTypes = useMemo(() => billingTypesFor(billingScope, billingKind), [billingScope, billingKind]);

  const [expandedProjects, setExpandedProjects] = useState<Record<number, boolean>>({});

  const { data: allProjects } = useGetAllProjectsQuery();
  const { data: metadata } = useGetProjectMetadataQuery();
  const { data: employeesData } = useGetAssignableEmployeesQuery();
  const [createTask, { isLoading: isCreatingTask }] = useCreateTaskMutation();
  const { showToast } = useFeedback();
  // The Members directory's Allow / Not allow switch applies to this
  // account too: the backend refuses the create, so the button is not
  // offered rather than shown and bounced.
  const { currentUser } = useAuth();
  const canAddTasks = currentUser?.can_add_tasks !== false;

  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [formProjectId, setFormProjectId] = useState<number | "">("");
  const [formTaskName, setFormTaskName] = useState("");
  const [formAssigneeId, setFormAssigneeId] = useState<number | "">("");
  const [formStatusId, setFormStatusId] = useState<number>(1);
  const [formEstimatedHours, setFormEstimatedHours] = useState('');
  const [formError, setFormError] = useState<string | null>(null);

  // The project is picked from a list, so it is validated as an identifier: the
  // picker's "" placeholder must not reach the API as project 0.
  const taskForm = useFormValidation({
    projectId: { rule: 'identifier', label: 'Project', required: true },
    name: { rule: 'name', label: 'Task name', required: true },
  });

  const { data, isLoading, isFetching } = useGetProjectTaskSummaryQuery({
    page,
    limit,
    start_date: startDate || undefined,
    end_date: endDate || undefined,
    project_id: filterProjectIds.length ? filterProjectIds.map(Number) : undefined,
    billing_type: billingTypes,
  });

  const showFirstLoad = isLoading && !data;
  const projects = data?.projects || [];
  const pagination = data?.pagination;

  // Initialize expanded state for newly loaded projects — collapsed by
  // default, so the page opens as a compact project list and the admin
  // expands only what they want to read.
  useEffect(() => {
    if (projects.length > 0) {
      setExpandedProjects((prev) => {
        const next = { ...prev };
        let changed = false;
        projects.forEach((p) => {
          if (next[p.id] === undefined) {
            next[p.id] = false;
            changed = true;
          }
        });
        return changed ? next : prev;
      });
    }
  }, [projects]);

  const toggleProject = (id: number) => {
    setExpandedProjects((prev) => ({
      ...prev,
      [id]: !prev[id],
    }));
  };

  const collapseAll = () => {
    const next: Record<number, boolean> = {};
    projects.forEach((p) => {
      next[p.id] = false;
    });
    setExpandedProjects(next);
  };
  
  const expandAll = () => {
    const next: Record<number, boolean> = {};
    projects.forEach((p) => {
      next[p.id] = true;
    });
    setExpandedProjects(next);
  };
  
  const isAnyExpanded = Object.values(expandedProjects).some((v) => v);

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();

    // This used to be `if (!formProjectId || !formTaskName) return;` — a silent
    // return that closed nothing, saved nothing and said nothing.
    const check = taskForm.validateAll({ projectId: formProjectId, name: formTaskName });
    if (!check.ok) {
      setFormError(null);
      return;
    }

    const estimated = formEstimatedHours.trim();
    if (estimated !== '' && (!Number.isFinite(Number(estimated)) || Number(estimated) < 0)) {
      setFormError('Estimated hours must be a number of 0 or more.');
      return;
    }

    try {
      await createTask({
        projectId: check.values.projectId as number,
        body: {
          name: check.values.name as string,
          assignee_id: formAssigneeId === "" ? null : Number(formAssigneeId),
          status_id: formStatusId,
          ...(estimated !== '' ? { estimated_hours: Number(estimated) } : {}),
        },
      }).unwrap();
      setIsDrawerOpen(false);
      setFormTaskName("");
      setFormEstimatedHours("");
      setFormError(null);
      taskForm.clear();
      showToast("Task created successfully.", "success");
      // Deliberately no refetch here.
      //
      // `createTask` puts the created task straight into this report's cache,
      // so the row is on screen the moment the server confirms it. Calling
      // `refetch()` immediately afterwards hid it again: while a query is
      // re-fetching, `useQuery` keeps serving the snapshot it held when the
      // *previous* request fulfilled, so a patch written to the cache mid-flight
      // does not reach the component until the new response lands. The cache was
      // correct the whole time and the screen still showed the old list.
      //
      // Measured in a browser, with the report artificially held for 6s: with
      // the refetch the task appeared 8.1s after Create (exactly when the report
      // answered); without it, 28ms after the POST returned.
      //
      // Nothing is lost by not refetching. A task created a moment ago has no
      // tracked time, and its project's total is unchanged, so the patched row
      // is exactly what the report would return. Ordinary staleness is handled
      // as everywhere else in the app, by `refetchOnMountOrArgChange` and by
      // changing a filter.
    } catch (err: any) {
      console.error(err);
      const errorMsg = err?.data?.detail || err?.data?.message || "Unable to create task. Please try again.";
      setFormError(errorMsg);
      showToast(errorMsg, "error");
    }
  };

  return (
    <V2Shell 
      title="Project Tasks" 
      subtitle="Review tasks grouped by project"
      actions={
        <div className="flex items-center gap-3">
          <InlineRefreshIndicator active={isFetching && !showFirstLoad} />
          {canAddTasks && (
            <button
              onClick={() => {
                setFormProjectId("");
                setFormTaskName("");
                setFormError(null);
                setFormAssigneeId("");
                setFormStatusId(metadata?.task_statuses?.[0]?.id || 1);
                setFormEstimatedHours("");
                setIsDrawerOpen(true);
              }}
              className="rounded-lg bg-gradient-to-r from-[#3B82F6] to-[#8B5CF6] px-4 py-2 text-sm font-bold text-white shadow-md transition hover:opacity-90"
            >
              + Add Task
            </button>
          )}
        </div>
      }
    >
      <div className="w-full px-4 py-8 sm:px-6 lg:px-8">
        {/* Toolbar */}
        <div className="mb-6 flex flex-wrap items-center justify-end gap-3">
            {/* Project Filter */}
            <ProjectMultiSelect
              projects={allProjects || []}
              selected={filterProjectIds}
              onChange={(ids) => {
                setFilterProjectIds(ids);
                setPage(1);
              }}
              compact
            />

            {/* Project type: Billing / Non Billing, then Fixed Hours / Flexible Time under Billing */}
            <div className="relative">
              <select
                aria-label="Filter by project type"
                value={billingScope}
                onChange={(e) => {
                  setBillingScope(e.target.value as BillingScope);
                  // The second choice belongs to Billing; leaving Billing clears it.
                  setBillingKind('');
                  setPage(1);
                }}
                className={FILTER_SELECT_CLASS}
              >
                <option value="">All Project Types</option>
                <option value="billing">Billing</option>
                <option value="non_billing">Non Billing</option>
              </select>
              <SelectChevron />
            </div>
            {billingScope === 'billing' && (
              <div className="relative">
                <select
                  aria-label="Filter by billing type"
                  value={billingKind}
                  onChange={(e) => { setBillingKind(e.target.value as BillingKind); setPage(1); }}
                  className={FILTER_SELECT_CLASS}
                >
                  <option value="">All Billing</option>
                  <option value="fixed">{billingTypeLabel('fixed')}</option>
                  <option value="free">{billingTypeLabel('free')}</option>
                </select>
                <SelectChevron />
              </div>
            )}

            {/* Date Filter */}
            <DateRangeFilter
              value={dateRange}
              onChange={(range) => { setDateRange(range); setPage(1); }}
            />

            <button
              type="button"
              onClick={isAnyExpanded ? collapseAll : expandAll}
              className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-bold text-slate-500 transition hover:bg-slate-50 hover:text-slate-700"
            >
              {isAnyExpanded ? "Collapse All" : "Expand All"}
            </button>
        </div>

        {/* Grouped Projects */}
        {showFirstLoad ? (
          <div className="flex justify-center p-20">
            <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-500 border-t-transparent"></div>
          </div>
        ) : (
          <div className="relative space-y-6">
            {projects.length === 0 ? (
              <div className="rounded-xl border border-slate-200 bg-white p-12 text-center shadow-sm">
                <svg
                  className="mx-auto h-12 w-12 text-slate-300"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth="2"
                    d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"
                  />
                </svg>
                <h3 className="mt-4 text-sm font-bold text-slate-800">
                  Nothing worked on today
                </h3>
                <p className="mt-1 text-xs font-medium text-slate-500">
                  A project appears here once someone starts one of its tasks today.
                  If you filtered by project or project type, those filters apply too.
                </p>
              </div>
            ) : (
              projects.map((project) => {
                // Collapsed until explicitly opened — `undefined` (not yet
                // initialised) must render closed, or the first paint flashes
                // every project open before the init effect runs.
                const isExpanded = expandedProjects[project.id] === true;
                return (
                  <div
                    key={project.id}
                    className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden transition-all"
                  >
                    {/* Header */}
                    <div
                      onClick={() => toggleProject(project.id)}
                      className="flex cursor-pointer items-center justify-between bg-slate-50 p-6 transition hover:bg-slate-100"
                    >
                      <div className="flex items-center gap-4">
                        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-gradient-to-r from-[#3B82F6] to-[#8B5CF6] text-sm font-bold text-white shadow-sm">
                          {project.project_name ? project.project_name.charAt(0).toUpperCase() : 'P'}
                        </div>
                        <div>
                          <h3 className="text-lg font-black text-slate-800">
                            {project.project_name}
                          </h3>
                          <div className="mt-0.5 flex flex-wrap items-center gap-2">
                            <p className="text-xs font-semibold text-slate-500">
                              {project.total_task_count} Task{project.total_task_count !== 1 ? 's' : ''} &bull;{" "}
                              {formatHMS(project.total_task_seconds)} Total Time
                            </p>
                            {project.status && (
                              <span
                                className="inline-block px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider text-white"
                                style={{ backgroundColor: project.status.color }}
                              >
                                {project.status.name}
                              </span>
                            )}
                            {project.billing_type && (
                              <span
                                data-testid="billing-chip"
                                className="inline-flex items-center rounded border bg-white px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider"
                                style={{
                                  color: BILLING_CHIP_COLOR[project.billing_type] ?? '#64748B',
                                  borderColor: BILLING_CHIP_COLOR[project.billing_type] ?? '#64748B',
                                }}
                              >
                                {billingTypeLabel(project.billing_type)}
                              </span>
                            )}
                          </div>
                          <BudgetUsage project={project} />
                        </div>
                      </div>
                      <div className="flex items-center gap-6">
                        <span className="text-xs font-bold text-slate-400">Created {formatDate(project.created_date)}</span>
                        <svg
                          className={`h-5 w-5 text-slate-400 transition-transform ${isExpanded ? "rotate-180" : ""}`}
                          fill="none"
                          stroke="currentColor"
                          viewBox="0 0 24 24"
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth="2"
                            d="M19 9l-7 7-7-7"
                          />
                        </svg>
                      </div>
                    </div>

                    {/* Tasks List */}
                    {isExpanded && (
                      <div className="divide-y divide-slate-100">
                        {project.tasks.length === 0 ? (
                          <div className="p-6 text-center text-sm font-semibold text-slate-500">
                            No tasks found for this project.
                          </div>
                        ) : (
                          project.tasks.map((task) => (
                            <div
                              key={task.id}
                              className="flex items-center justify-between p-6 transition hover:bg-slate-50/50"
                            >
                              <div className="flex items-start gap-4">
                                <svg
                                  className="mt-0.5 h-5 w-5 text-slate-400"
                                  fill="none"
                                  stroke="currentColor"
                                  viewBox="0 0 24 24"
                                >
                                  <path
                                    strokeLinecap="round"
                                    strokeLinejoin="round"
                                    strokeWidth="2"
                                    d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                                  />
                                </svg>
                                <div>
                                  <h4 className="text-sm font-bold text-slate-700 line-clamp-2">
                                    {task.task_name}
                                  </h4>
                                  <div className="mt-1 flex items-center gap-3 text-[11px] font-semibold text-slate-400">
                                    <span className="flex items-center gap-1">
                                      Created {formatDate(task.task_created_date)}
                                    </span>
                                  </div>
                                </div>
                              </div>
                              <div className="flex items-center gap-4 text-right">
                                <TaskBudgetCell projectId={project.id} task={task} />
                                <div className="text-sm font-bold text-slate-800">
                                  {formatHMS(task.total_tracked_seconds)}
                                </div>
                              </div>
                            </div>
                          ))
                        )}
                      </div>
                    )}
                  </div>
                );
              })
            )}

            {/* Pagination */}
            {pagination && pagination.total_pages > 1 && (
              <Pagination page={page} totalPages={pagination.total_pages} totalItems={pagination.total_projects} limit={limit} setPage={setPage} setLimit={setLimit} />
            )}
          </div>
        )}
      </div>
        {/* Create Task Drawer */}
        <div className={`fixed inset-0 z-50 overflow-hidden ${isDrawerOpen ? "pointer-events-auto" : "pointer-events-none"}`}>
          <div
            className={`absolute inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity duration-300 ${isDrawerOpen ? "opacity-100" : "opacity-0"}`}
            onClick={() => setIsDrawerOpen(false)}
          />
          <div
            className={`absolute inset-y-0 right-0 w-full max-w-md bg-white shadow-2xl transition-transform duration-300 ease-in-out ${isDrawerOpen ? "translate-x-0" : "translate-x-full"}`}
          >
            <div className="flex h-full flex-col">
              <div className="flex items-center justify-between border-b border-slate-100 px-6 py-5">
                <div>
                  <h2 className="text-xl font-black text-slate-800">
                    Create Task
                  </h2>
                  <p className="mt-1 text-sm font-semibold text-slate-500">
                    Assign a new task to a project
                  </p>
                </div>
                <button
                  onClick={() => setIsDrawerOpen(false)}
                  className="rounded-full p-2 text-slate-400 hover:bg-slate-50 hover:text-slate-600"
                >
                  <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
  
              <div className="flex-1 overflow-y-auto p-6">
                
                {formError && (
                  <div className="mb-6 rounded-lg border border-rose-200 bg-rose-50 p-4 flex items-start gap-3 text-rose-600">
                    <svg className="mt-0.5 h-5 w-5 shrink-0 text-rose-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <p className="text-sm font-semibold">{formError}</p>
                  </div>
                )}
                <form id="task-form" onSubmit={handleCreateTask} className="space-y-6">
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">
                      Project <span className="text-rose-500">*</span>
                    </label>
                    <ProjectPicker
                      projects={allProjects || []}
                      value={formProjectId}
                      onChange={setFormProjectId}
                    />
                    <FieldError id={taskForm.errorId('projectId')} message={taskForm.errors.projectId} />
                  </div>
  
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">
                      Task Name <span className="text-rose-500">*</span>
                    </label>
                    <input
                      required
                      type="text"
                      value={formTaskName}
                      onChange={(e) => setFormTaskName(e.target.value)}
                      onBlur={() => taskForm.validateField('name', formTaskName)}
                      placeholder="e.g. Design Homepage"
                      {...taskForm.fieldProps('name')}
                      className="w-full rounded-lg border border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700 outline-none transition focus:border-[#3B82F6]"
                    />
                    <FieldError id={taskForm.errorId('name')} message={taskForm.errors.name} />
                  </div>
  
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">
                      Assign To
                    </label>
                    <select
                      value={formAssigneeId}
                      onChange={(e) => setFormAssigneeId(e.target.value === "" ? "" : Number(e.target.value))}
                      className="w-full rounded-lg border border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700 outline-none transition focus:border-[#3B82F6]"
                    >
                      <option value="">Unassigned</option>
                      {employeesData?.map((emp) => (
                        <option key={emp.id} value={emp.id}>
                          {emp.name}
                        </option>
                      ))}
                    </select>
                  </div>
  
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">
                      Estimated Hours
                    </label>
                    <input
                      type="number"
                      min={0}
                      step={0.25}
                      value={formEstimatedHours}
                      onChange={(e) => setFormEstimatedHours(e.target.value)}
                      placeholder="Optional — the task's budgeted hours"
                      className="w-full rounded-lg border border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700 outline-none transition focus:border-[#3B82F6]"
                    />
                    <p className="mt-1 text-[11px] font-medium text-slate-400">
                      Shown on the client Billing page as the task's total, with remaining hours measured against it.
                    </p>
                  </div>

                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">
                      Status
                    </label>
                    <select
                      value={formStatusId}
                      onChange={(e) => setFormStatusId(Number(e.target.value))}
                      className="w-full rounded-lg border border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700 outline-none transition focus:border-[#3B82F6]"
                    >
                      {metadata?.task_statuses?.map((st: any) => (
                        <option key={st.id} value={st.id}>
                          {st.task_status}
                        </option>
                      ))}
                    </select>
                  </div>
                </form>
              </div>
  
              <div className="border-t border-slate-100 bg-slate-50 p-6 flex gap-3">
                <button
                  type="button"
                  onClick={() => setIsDrawerOpen(false)}
                  className="flex-1 rounded-lg border border-slate-200 bg-white py-3 text-sm font-bold text-slate-700 transition hover:bg-slate-50"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  form="task-form"
                  disabled={isCreatingTask}
                  className="flex-1 rounded-lg bg-gradient-to-r from-[#3B82F6] to-[#8B5CF6] py-3 text-sm font-bold text-white transition hover:opacity-90 shadow-md disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {isCreatingTask ? "Creating…" : "Create Task"}
                </button>
              </div>
            </div>
          </div>
        </div>
      </V2Shell>
  );
};
