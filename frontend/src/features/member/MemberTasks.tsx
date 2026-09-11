import React, { useEffect, useMemo, useRef, useState } from "react";
import { MemberShell } from "./MemberShell";
import { Card, EmptyState, ErrorNote, Spinner, StatusPill } from "./MemberUi";
import {
  useGetAllProjectsQuery,
  useGetProjectMetadataQuery,
  useUpdateTaskMutation,
} from "../../store/api/projectsApi";
import type { ProjectTask } from "../../store/api/projectsApi";
import { useGetReactReportsListQuery } from "../../store/api/reportsApi";
import { useGetReactDashboardQuery } from "../../store/api/dashboardApi";
import { useAuth } from "../auth/authContext";
import { useFeedback } from "../../components/FeedbackProvider";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { DateRangeFilter, DEFAULT_RANGE } from "../dashboard/v2/filters";
import type { DateRange } from "../dashboard/v2/filters";
import { formatHMS, formatHoursAsHMS } from "../../utils/duration";
import { series } from "../dashboard/v2/theme";
import { FieldError, SEARCH_MAX_LENGTH, validateSearchTerm } from "../../validation";

/**
 * The member's task list.
 *
 * Two sources, joined on task id, because neither one alone is the answer:
 *
 *  - `/projects` lists the tasks of the projects the caller belongs to, with
 *    assignee and status. It knows *what* the tasks are.
 *  - `/react/reports/tasks` knows how long the caller tracked against each of
 *    them in the selected range — and, being member-scoped server-side, it
 *    reports only their own time even on a task shared with the team.
 *
 * A task with no tracked time in range shows `00:00:00`, which is the true
 * answer, not a missing one.
 *
 * The totals here will not add up to the dashboard's "Time Worked" for the
 * same range, and that is not a bug: `/react/reports/tasks` groups the
 * member's time entries by `task_id` and inner-joins the task, so any time
 * they tracked against a project *without* picking a task has no task to
 * belong to and is absent from every row. The reconciliation strip below
 * states that difference outright instead of leaving the member to discover
 * it as a contradiction between two pages.
 *
 * Rows are grouped under their project rather than repeating a project column
 * on every line.
 *
 * Only two kinds of task reach this page: the ones assigned to the signed-in
 * member, and the ones nobody owns yet — those are work they could pick up.
 * A task owned by a *different* employee is never listed: it is not their
 * work, they cannot move its status, and its presence only made their own
 * task count harder to read. There is deliberately no filter to bring those
 * back; `/projects` returns them because the member belongs to the project,
 * not because the tasks are theirs.
 */

interface TaskRow {
  task: ProjectTask;
  projectId: number;
  projectName: string;
  seconds: number;
}

const isDone = (name?: string) => /done|complete/i.test(name || "");

/* ------------------------------------------------------------------ */
/* Small primitives                                                    */
/* ------------------------------------------------------------------ */

const useClickOutside = (onOutside: () => void, active: boolean) => {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!active) return;
    const handler = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onOutside();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [active, onOutside]);
  return ref;
};

const StatCard: React.FC<{
  label: string;
  value: string;
  note: string;
  accent: string;
  icon: React.ReactNode;
}> = ({ label, value, note, accent, icon }) => (
  <div className="relative overflow-hidden rounded-2xl border border-[#E2E8F0] bg-white p-5 shadow-sm transition hover:shadow-md">
    {/* A wash of the accent rather than a solid block: it identifies the card
        without competing with the number, which is the thing being read. */}
    <div
      className="pointer-events-none absolute -right-8 -top-10 h-28 w-28 rounded-full opacity-[0.07]"
      style={{ background: accent }}
    />
    <div className="flex items-center gap-3">
      <span
        className="flex h-9 w-9 items-center justify-center rounded-xl text-white"
        style={{ background: accent }}
      >
        {icon}
      </span>
      <span className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">{label}</span>
    </div>
    <div className="mt-4 text-[30px] font-extrabold leading-none tracking-tight text-[#0F172A]">{value}</div>
    <div className="mt-1.5 text-[11px] font-medium text-[#94A3B8]">{note}</div>
  </div>
);

/** Inline status control, styled to match the badges it sits among. */
const StatusSelect: React.FC<{
  value: { id: number; name: string; color: string } | null | undefined;
  options: { id: number; task_status: string; color: string }[];
  disabled?: boolean;
  onChange: (statusId: number) => void;
}> = ({ value, options, disabled, onChange }) => {
  const [open, setOpen] = useState(false);
  const ref = useClickOutside(() => setOpen(false), open);
  const color = value?.color || "#64748B";

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        className="inline-flex items-center gap-1.5 rounded-full py-1 pl-2.5 pr-2 text-[11px] font-bold transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-50"
        style={{ backgroundColor: `${color}1A`, color }}
      >
        <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: color }} />
        {value?.name || "No status"}
        <svg className="h-3 w-3 opacity-70" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="absolute right-0 z-30 mt-1.5 w-44 overflow-hidden rounded-xl border border-[#E2E8F0] bg-white py-1 shadow-xl">
          {options.map((option) => {
            const active = option.id === value?.id;
            return (
              <button
                key={option.id}
                type="button"
                onClick={() => {
                  setOpen(false);
                  if (!active) onChange(option.id);
                }}
                className={
                  "flex w-full items-center gap-2.5 px-3 py-2 text-left text-[12px] font-semibold transition hover:bg-[#F8FAFC] " +
                  (active ? "text-[#0F172A]" : "text-[#64748B]")
                }
              >
                <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: option.color }} />
                <span className="flex-1 truncate">{option.task_status}</span>
                {active && (
                  <svg className="h-3.5 w-3.5 text-[#2563EB]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" d="M5 13l4 4L19 6" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

export const MemberTasks: React.FC = () => {
  const { currentUser } = useAuth();
  const { showToast } = useFeedback();

  const [range, setRange] = useState<DateRange>(DEFAULT_RANGE);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [search, setSearch] = useState("");
  const [searchError, setSearchError] = useState<string | null>(null);
  const debouncedSearch = useDebouncedValue(search);

  // This screen is built out of the tasks themselves, so it is one of the two
  // that asks for them; everywhere else the project list is fetched without.
  const { data: projects = [], isLoading, isFetching, isError } = useGetAllProjectsQuery({
    includeTasks: true,
  });
  const { data: metadata } = useGetProjectMetadataQuery();
  const [updateTask, { isLoading: isUpdating }] = useUpdateTaskMutation();

  const { data: trackedData } = useGetReactReportsListQuery({
    dimension: "tasks",
    start_date: range.from,
    end_date: range.to,
    page: 1,
    limit: 200,
    sort_by: "total_hours",
    sort_order: "desc",
  });

  // The same self-scoped total the member's dashboard shows, used only to
  // explain the gap between it and the per-task rows.
  const { data: dashboard } = useGetReactDashboardQuery({
    start_date: range.from,
    end_date: range.to,
    top_n: 1,
  });

  /** task id -> seconds this member tracked against it in the range. */
  const secondsByTask = useMemo(() => {
    const map = new Map<number, number>();
    (trackedData?.items || []).forEach((item) => {
      if (item.task_id != null) map.set(item.task_id, Math.round((item.total_hours || 0) * 3600));
    });
    return map;
  }, [trackedData]);

  /** Every task on the member's projects, before the scope filter. */
  const allRows = useMemo<TaskRow[]>(
    () =>
      projects.flatMap((project) =>
        (project.tasks || []).map((task) => ({
          task,
          projectId: project.id,
          projectName: project.project_name,
          seconds: secondsByTask.get(task.id) ?? 0,
        }))
      ),
    [projects, secondsByTask]
  );

  /** Mine, or nobody's. Everything else belongs to another employee. */
  const isOwnOrUnclaimed = (row: TaskRow) =>
    !row.task.assignee || row.task.assignee.id === currentUser?.id;

  /** Every task this page will ever show, before project/search filtering. */
  const ownRows = useMemo(
    () => allRows.filter(isOwnOrUnclaimed),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [allRows, currentUser]
  );

  const rows = useMemo(() => {
    // A rejected term filters nothing rather than filtering everything away:
    // the box explains itself while the list stays as it was.
    const checked = validateSearchTerm(debouncedSearch, { fieldLabel: "Search" });
    const term = (checked.ok ? checked.value : "").toLowerCase();
    return ownRows
      .filter((row) => projectId === null || row.projectId === projectId)
      .filter((row) =>
        term
          ? row.task.name.toLowerCase().includes(term) || row.projectName.toLowerCase().includes(term)
          : true
      );
  }, [ownRows, projectId, debouncedSearch]);

  const assignedToMe = rows.filter((row) => row.task.assignee).length;

  /** Grouped under their project, both levels ordered by tracked time. */
  const groups = useMemo(() => {
    const byProject = new Map<number, { name: string; tasks: TaskRow[]; seconds: number }>();
    rows.forEach((row) => {
      const group = byProject.get(row.projectId) ?? { name: row.projectName, tasks: [], seconds: 0 };
      group.tasks.push(row);
      group.seconds += row.seconds;
      byProject.set(row.projectId, group);
    });
    return [...byProject.entries()]
      .map(([id, group]) => ({
        id,
        ...group,
        tasks: group.tasks.sort(
          (a, b) => b.seconds - a.seconds || a.task.name.localeCompare(b.task.name)
        ),
      }))
      .sort((a, b) => b.seconds - a.seconds || a.name.localeCompare(b.name));
  }, [rows]);

  const totalSeconds = rows.reduce((sum, row) => sum + row.seconds, 0);
  /** Everything the member tracked in range, whether or not it hit a task. */
  const trackedSeconds = Math.round((dashboard?.summary.total_hours ?? 0) * 3600);
  /** ...of which this much landed on some task. */
  const onTaskSeconds = [...secondsByTask.values()].reduce((sum, value) => sum + value, 0);
  const unlinkedSeconds = Math.max(0, trackedSeconds - onTaskSeconds);
  const completed = rows.filter((row) => isDone(row.task.status?.name)).length;
  // The longest single task in view sets the scale for the time bars, so the
  // bars compare tasks against each other rather than against a fixed ceiling.
  const maxSeconds = Math.max(1, ...rows.map((row) => row.seconds));

  const changeStatus = async (row: TaskRow, statusId: number) => {
    try {
      await updateTask({ projectId: row.projectId, taskId: row.task.id, body: { status_id: statusId } }).unwrap();
      showToast("Task status updated.", "success");
    } catch (error: any) {
      // A member may update tasks in their own projects; anything else is the
      // server's call, and its reason is more useful than a generic message.
      showToast(error?.data?.detail || "Could not update this task.", "error");
    }
  };

  const resetFilters = () => {
    setProjectId(null);
    setSearch("");
    setRange(DEFAULT_RANGE);
  };

  const emptyMessage = ownRows.length
    ? "No tasks match these filters."
    : "You have no tasks right now, and every task on your projects already has another owner.";

  return (
    <MemberShell
      title="My Tasks"
      subtitle="Your tasks and the unclaimed ones, with the time you tracked against each."
      actions={<InlineRefreshIndicator active={isFetching && !isLoading} />}
    >
      <div className="w-full space-y-6 pb-20">
        {/* Filters */}
        <div className="rounded-2xl border border-[#E2E8F0] bg-white p-3 shadow-sm">
          {/* Two groups rather than one long row: when the viewport is narrow
              each group wraps as a unit, instead of single controls peeling off
              and stranding themselves against the right edge. */}
          <div className="flex flex-wrap items-center justify-between gap-y-2.5">
            <div className="flex flex-wrap items-center gap-2.5">
            <DateRangeFilter value={range} onChange={setRange} />

            </div>

            <div className="flex flex-wrap items-center gap-2.5">
              <select
                value={projectId ?? ""}
                onChange={(event) => setProjectId(event.target.value ? Number(event.target.value) : null)}
                className="rounded-xl border border-[#E2E8F0] bg-white px-3 py-2 text-[12px] font-bold text-[#0F172A] outline-none transition focus:border-[#2563EB]"
              >
                <option value="">All my projects</option>
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.project_name}
                  </option>
                ))}
              </select>

              <div className="relative">
                <svg
                  className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[#94A3B8]"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                </svg>
                <input
                  value={search}
                  onChange={(event) => {
                    const next = event.target.value;
                    setSearch(next);
                    const result = validateSearchTerm(next, { fieldLabel: "Search" });
                    setSearchError(result.ok ? null : result.error);
                  }}
                  placeholder="Search tasks…"
                  maxLength={SEARCH_MAX_LENGTH}
                  aria-invalid={searchError ? true : undefined}
                  aria-describedby={searchError ? "task-search-error" : undefined}
                  className="w-52 rounded-xl border border-[#E2E8F0] py-2 pl-9 pr-3 text-[12px] font-medium text-[#0F172A] outline-none transition focus:border-[#2563EB]"
                />
                <FieldError id="task-search-error" message={searchError} />
              </div>

              <button
                onClick={resetFilters}
                className="rounded-xl border border-[#E2E8F0] px-3 py-2 text-[12px] font-bold text-[#64748B] transition hover:bg-[#F8FAFC] hover:text-[#0F172A]"
              >
                Reset
              </button>
            </div>
          </div>
        </div>

        {isError && <ErrorNote message="Your tasks could not be loaded. Please try again." />}

        {/* Stats */}
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-4">
          <StatCard
            label="Tasks Shown"
            value={String(rows.length)}
            note={`${assignedToMe} assigned to you, ${rows.length - assignedToMe} unassigned`}
            accent={series[0]}
            icon={
              <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
              </svg>
            }
          />
          <StatCard
            label="My Time On These"
            value={formatHMS(totalSeconds)}
            note={`Your time on the ${rows.length} task${rows.length === 1 ? "" : "s"} shown`}
            accent={series[1]}
            icon={
              <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            }
          />
          <StatCard
            label="Completed"
            value={`${completed}/${rows.length}`}
            note={rows.length ? `${Math.round((completed / rows.length) * 100)}% done` : "Nothing in view"}
            accent={series[2]}
            icon={
              <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M5 13l4 4L19 6" />
              </svg>
            }
          />
          <StatCard
            label="Projects"
            value={String(groups.length)}
            note={groups.length === 1 ? "project in view" : "projects in view"}
            accent={series[3]}
            icon={
              <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
              </svg>
            }
          />
        </div>

        {/* Where the rest of the member's time went. Shown only when there is
            time that no task can account for, since otherwise it says nothing. */}
        {trackedSeconds > 0 && unlinkedSeconds > 0 && (
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-2xl border border-[#E2E8F0] bg-[#FBFCFE] px-5 py-3.5 text-[12px] font-semibold text-[#64748B]">
            <span className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">
              This range
            </span>
            <span>
              You tracked{" "}
              <span className="font-mono font-bold tabular-nums text-[#0F172A]">
                {formatHoursAsHMS(dashboard?.summary.total_hours ?? 0)}
              </span>{" "}
              in total
            </span>
            <span>
              on tasks{" "}
              <span className="font-mono font-bold tabular-nums text-[#0F172A]">
                {formatHMS(onTaskSeconds)}
              </span>
            </span>
            <span>
              not linked to any task{" "}
              <span className="font-mono font-bold tabular-nums text-[#0F172A]">
                {formatHMS(unlinkedSeconds)}
              </span>
            </span>
            <span className="basis-full text-[11px] font-medium text-[#94A3B8]">
              Time started without choosing a task is counted on your dashboard but cannot appear
              against a task row here.
            </span>
          </div>
        )}

        {/* Task groups */}
        {isLoading ? (
          <Card>
            <Spinner label="Loading your tasks…" />
          </Card>
        ) : groups.length === 0 ? (
          <Card>
            <EmptyState
              message={emptyMessage}
              hint="Tasks appear here once they exist on a project you belong to."
            />
          </Card>
        ) : (
          <div className={`space-y-5 transition-opacity ${isFetching ? "opacity-60" : ""}`}>
            {groups.map((group, groupIndex) => {
              const accent = series[groupIndex % series.length];
              return (
                // No `overflow-hidden` here: it would clip the status
                // dropdown, which opens out of a row. The corners are rounded
                // on the header and the last row instead.
                <section
                  key={group.id}
                  className="rounded-2xl border border-[#E2E8F0] bg-white shadow-sm"
                >
                  <header className="flex flex-wrap items-center gap-3 rounded-t-2xl border-b border-[#F1F5F9] bg-[#FBFCFE] px-5 py-3.5">
                    <span className="h-6 w-1 shrink-0 rounded-full" style={{ backgroundColor: accent }} />
                    <h3 className="min-w-0 flex-1 truncate text-[14px] font-bold text-[#0F172A]">
                      {group.name}
                    </h3>
                    <span className="rounded-full bg-[#F1F5F9] px-2.5 py-1 text-[11px] font-bold text-[#64748B]">
                      {group.tasks.length} task{group.tasks.length === 1 ? "" : "s"}
                    </span>
                    <span className="font-mono text-[13px] font-bold tabular-nums text-[#0F172A]">
                      {formatHMS(group.seconds)}
                    </span>
                  </header>

                  <ul>
                    {group.tasks.map((row) => {
                      const isMine = row.task.assignee?.id === currentUser?.id;
                      const done = isDone(row.task.status?.name);
                      const share = (row.seconds / maxSeconds) * 100;

                      return (
                        <li
                          key={row.task.id}
                          className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-[#F1F5F9] px-5 py-3.5 transition last:rounded-b-2xl last:border-0 hover:bg-[#F8FAFC]"
                        >
                          <div className="flex min-w-0 flex-1 items-center gap-3">
                            <span
                              className={
                                "flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 " +
                                (done ? "border-transparent bg-emerald-500 text-white" : "border-[#CBD5E1]")
                              }
                              aria-hidden="true"
                            >
                              {done && (
                                <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="4" d="M5 13l4 4L19 6" />
                                </svg>
                              )}
                            </span>
                            <span
                              className={
                                "truncate text-[13.5px] font-semibold " +
                                (done ? "text-[#94A3B8] line-through" : "text-[#0F172A]")
                              }
                              title={row.task.name}
                            >
                              {row.task.name}
                            </span>
                          </div>

                          {/* Two possible values here, and the difference
                              matters: an unassigned row is work nobody owns. */}
                          <span className="w-28 shrink-0 truncate text-[12px] font-semibold">
                            {isMine ? (
                              <span className="text-[#2563EB]">You</span>
                            ) : (
                              <span className="text-[#94A3B8]">Unassigned</span>
                            )}
                          </span>

                          <div className="shrink-0">
                            {isMine && metadata?.task_statuses?.length ? (
                              // The one edit a member is permitted: moving
                              // their own task along.
                              <StatusSelect
                                value={row.task.status}
                                options={metadata.task_statuses}
                                disabled={isUpdating}
                                onChange={(statusId) => changeStatus(row, statusId)}
                              />
                            ) : (
                              <StatusPill status={row.task.status} />
                            )}
                          </div>

                          <div className="flex w-40 shrink-0 items-center justify-end gap-2.5">
                            <span className="h-1.5 w-16 overflow-hidden rounded-full bg-[#F1F5F9]">
                              <span
                                className="block h-full rounded-full transition-all duration-500"
                                style={{
                                  width: `${share}%`,
                                  backgroundColor: row.seconds ? accent : "transparent",
                                }}
                              />
                            </span>
                            <span
                              className={
                                "w-20 text-right font-mono text-[12.5px] font-bold tabular-nums " +
                                (row.seconds ? "text-[#0F172A]" : "text-[#CBD5E1]")
                              }
                            >
                              {formatHMS(row.seconds)}
                            </span>
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                </section>
              );
            })}
          </div>
        )}
      </div>
    </MemberShell>
  );
};
