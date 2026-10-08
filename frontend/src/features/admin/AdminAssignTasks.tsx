import React, { useMemo, useState } from 'react';
import { V2Shell } from '../dashboard/v2/V2Shell';
import {
  useGetAllProjectsQuery,
  useGetProjectMetadataQuery,
  useSetTaskAssigneesMutation,
  useCreateTaskMutation,
  type Project,
  type ProjectTask,
  type ProjectUser,
} from '../../store/api/projectsApi';
import { useFeedback } from '../../components/FeedbackProvider';
import { InlineRefreshIndicator } from '../../components/InlineRefreshIndicator';
import { Pagination } from '../../components/Pagination';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { useAuth } from '../auth/authContext';
import { formatApiError } from '../../api/utils';
import { SEARCH_MAX_LENGTH } from '../../validation';
import { AssignTaskDialog, type AssignTaskSubmit } from './AssignTaskDialog';
import { filterByCreated, filterByHolders, filterByProjectIds, filterProjects, holdersOf, taskHolderOptions } from './assignTasks';
import { DEFAULT_RANGE, DateRangeFilter, MemberMultiSelect, ProjectMultiSelect, type DateRange } from '../dashboard/v2/filters';

/** Page sizes the footer offers; the first is the default. */
const PAGE_SIZES = [10, 20, 50];
/** One stable empty list, so memos keyed on `projects` do not re-run on every render before the data lands. */
const NO_PROJECTS: Project[] = [];
const AVATAR_COLORS = ['bg-blue-500', 'bg-rose-500', 'bg-emerald-500', 'bg-amber-500', 'bg-purple-500', 'bg-cyan-500'];

const initialsOf = (name: string) =>
  (name || 'U').split(' ').map((part) => part[0]).join('').substring(0, 2).toUpperCase();

/** The members holding a task: a stack of avatars with their names beside it. */
const Holders: React.FC<{ people: ProjectUser[] }> = ({ people }) => {
  if (people.length === 0) {
    return (
      <span className="text-xs font-semibold text-slate-400" title="No one is assigned: every member of the project can see this task">
        Unassigned · shared
      </span>
    );
  }
  const names = people.map((person) => person.name).join(', ');
  return (
    <div className="flex min-w-0 items-center gap-2" title={names}>
      <div className="flex shrink-0 items-center -space-x-2">
        {people.slice(0, 4).map((person) => (
          <div
            key={person.id}
            className={`flex h-7 w-7 items-center justify-center rounded-full text-[10px] font-bold text-white shadow-sm ring-2 ring-white ${AVATAR_COLORS[person.id % AVATAR_COLORS.length]}`}
          >
            {initialsOf(person.name)}
          </div>
        ))}
        {people.length > 4 && (
          <div className="flex h-7 w-7 items-center justify-center rounded-full bg-slate-100 text-[10px] font-bold text-slate-500 shadow-sm ring-2 ring-white">
            +{people.length - 4}
          </div>
        )}
      </div>
      <span className="truncate text-xs font-semibold text-slate-600">
        {people.length <= 2 ? names : `${people[0].name} +${people.length - 1} more`}
      </span>
    </div>
  );
};

const StatusPill: React.FC<{ task: ProjectTask }> = ({ task }) => {
  const color = task.status?.color || '#64748B';
  // The status colour tints the pill and fills a dot, but the label is always
  // dark slate: "Todo" is a very pale grey (#CBD5E1), which as text on its own
  // tint is close to invisible.
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11px] font-bold tracking-wide text-slate-700"
      style={{ backgroundColor: `${color}1A`, borderColor: `${color}66` }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: color }} />
      {task.status?.name || 'No status'}
    </span>
  );
};

type DialogState = { editing: { projectId: number; taskId: number } | null } | null;

/**
 * Assign Tasks: every project with its tasks and who holds each one, and a
 * dialog to give a task to several members at once.
 *
 * Reads the same project list the rest of the admin screens use, with tasks;
 * a save writes the server's answer straight into that cache (see
 * `setTaskAssignees`), so a task shows its new members the moment it is saved.
 */
export const AdminAssignTasks: React.FC = () => {
  const { data: projects = NO_PROJECTS, isLoading, isFetching, isError, refetch } = useGetAllProjectsQuery({ includeTasks: true });
  const { data: metadata } = useGetProjectMetadataQuery();
  const [setTaskAssignees, { isLoading: assigning }] = useSetTaskAssigneesMutation();
  const [createTask, { isLoading: creating }] = useCreateTaskMutation();
  const saving = assigning || creating;
  // The Members directory's Add Task switch applies here too: the backend
  // refuses the create, so "New task" is not offered rather than offered and bounced.
  const { currentUser } = useAuth();
  const canCreateTasks = currentUser?.can_add_tasks !== false;
  const { showToast } = useFeedback();

  const [searchInput, setSearchInput] = useState('');
  const query = useDebouncedValue(searchInput, 250);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);
  // Opens on the last 7 days, the same default as the reports. "All Time" is
  // one click away in the picker for tasks created earlier.
  const [dateRange, setDateRange] = useState<DateRange>(DEFAULT_RANGE);
  const [projectIds, setProjectIds] = useState<string[]>([]);
  // The Reports page's member filter: empty is everyone; picked members narrow the page to the tasks they hold.
  const [memberIds, setMemberIds] = useState<string[]>([]);
  const [collapsed, setCollapsed] = useState<Record<number, boolean>>({});
  const [dialog, setDialog] = useState<DialogState>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  const holderChoices = useMemo(() => taskHolderOptions(projects), [projects]);
  const visible = useMemo(
    () => filterProjects(filterByCreated(filterByHolders(filterByProjectIds(projects, projectIds), memberIds), dateRange), query),
    [projects, projectIds, memberIds, dateRange, query],
  );
  const filtering = query.trim() !== '' || projectIds.length > 0 || memberIds.length > 0 || dateRange.preset !== 'all';
  const totalPages = Math.max(1, Math.ceil(visible.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pageProjects = visible.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const showFirstLoad = isLoading && projects.length === 0;

  const openDialog = (editing: { projectId: number; taskId: number } | null) => {
    setSaveError(null);
    setDialog({ editing });
  };

  const plural = (n: number) => `${n} member${n === 1 ? '' : 's'}`;

  const handleSubmit = async (value: AssignTaskSubmit) => {
    setSaveError(null);
    try {
      if (value.kind === 'new') {
        // One request creates the task and its holders together, so a failure
        // never leaves a half-made, unassigned (= shared) task behind.
        await createTask({
          projectId: value.projectId,
          body: {
            name: value.name,
            status_id: value.statusId,
            ...(value.userIds.length > 0 ? { assignee_ids: value.userIds } : {}),
            ...(value.estimatedHours !== null ? { estimated_hours: value.estimatedHours } : {}),
          },
        }).unwrap();
        setDialog(null);
        showToast(
          value.userIds.length === 0 ? 'Task created.' : `Task created and assigned to ${plural(value.userIds.length)}.`,
          'success',
        );
        return;
      }
      await setTaskAssignees({
        projectId: value.projectId,
        taskId: value.taskId,
        body: { user_ids: value.userIds, status_id: value.statusId },
      }).unwrap();
      setDialog(null);
      showToast(value.userIds.length === 0 ? 'Task unassigned.' : `Task assigned to ${plural(value.userIds.length)}.`, 'success');
    } catch (err: any) {
      const message = formatApiError(err?.data, 'Could not save the assignment. Please try again.');
      setSaveError(message);
      showToast(message, 'error');
    }
  };

  return (
    <V2Shell
      title="Assign Tasks"
      subtitle="Give a task to one or more members"
      actions={
        <div className="flex items-center gap-3">
          <InlineRefreshIndicator active={isFetching && !showFirstLoad} />
          <button
            type="button"
            onClick={() => openDialog(null)}
            className="rounded-lg bg-gradient-to-r from-[#3B82F6] to-[#8B5CF6] px-4 py-2 text-sm font-bold text-white shadow-md transition hover:opacity-90"
          >
            + Assign Task
          </button>
        </div>
      }
    >
      <div className="w-full px-4 py-8 sm:px-6 lg:px-8">
        {/* Search and filters: the same card as Project Management's toolbar. */}
        <div className="mb-6 flex flex-col items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:flex-row">
          <div className="relative w-full sm:min-w-[12rem] sm:max-w-md sm:flex-1">
            <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input
              type="text"
              value={searchInput}
              maxLength={SEARCH_MAX_LENGTH}
              onChange={(event) => { setSearchInput(event.target.value); setPage(1); }}
              placeholder="Search projects, tasks or members..."
              aria-label="Search projects, tasks or members"
              className="w-full rounded-lg border border-slate-200 bg-slate-50 py-2 pl-10 pr-4 text-sm font-semibold text-slate-700 outline-none transition focus:border-[#3B82F6] focus:bg-white focus:ring-1 focus:ring-[#3B82F6]"
            />
          </div>
          <div className="flex w-full flex-wrap items-center gap-3 sm:w-auto">
            <MemberMultiSelect
              members={holderChoices}
              selected={memberIds}
              onChange={(ids) => { setMemberIds(ids); setPage(1); }}
            />
            <ProjectMultiSelect
              projects={projects}
              selected={projectIds}
              onChange={(ids) => { setProjectIds(ids); setPage(1); }}
            />
            <DateRangeFilter
              allowAll
              value={dateRange}
              onChange={(range) => { setDateRange(range); setPage(1); }}
            />
            <button
              type="button"
              onClick={() => {
                const anyOpen = pageProjects.some((project) => !collapsed[project.id]);
                setCollapsed((previous) => ({
                  ...previous,
                  ...Object.fromEntries(pageProjects.map((project) => [project.id, anyOpen])),
                }));
              }}
              className="h-9 rounded-lg border border-slate-200 bg-white px-4 text-sm font-bold text-slate-700 shadow-sm transition hover:bg-slate-50"
            >
              {pageProjects.some((project) => !collapsed[project.id]) ? 'Collapse All' : 'Expand All'}
            </button>
          </div>
        </div>

        {showFirstLoad ? (
          <div className="flex justify-center p-20">
            <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-500 border-t-transparent" />
          </div>
        ) : isError && projects.length === 0 ? (
          <div className="rounded-xl border border-rose-200 bg-rose-50 p-8 text-center">
            <p className="text-sm font-bold text-rose-700">Could not load projects.</p>
            <button
              type="button"
              onClick={() => refetch()}
              className="mt-3 rounded-lg border border-rose-200 bg-white px-3 py-1.5 text-sm font-bold text-rose-700 hover:bg-rose-100"
            >
              Try again
            </button>
          </div>
        ) : pageProjects.length === 0 ? (
          <div className="rounded-xl border border-slate-200 bg-white p-12 text-center shadow-sm">
            <h3 className="text-sm font-bold text-slate-800">
              {filtering ? 'No matching projects or tasks' : 'No projects yet'}
            </h3>
            <p className="mt-1 text-xs font-medium text-slate-500">
              {filtering
                ? 'Nothing matches the current search, member, project and date filters. Widen them or choose All Time.'
                : 'Projects and their tasks appear here once they are created in Project Management.'}
            </p>
          </div>
        ) : (
          <div className="space-y-6">
            {pageProjects.map((project) => {
              const tasks = project.tasks ?? [];
              const open = !collapsed[project.id];
              return (
                <section key={project.id} className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
                  <button
                    type="button"
                    aria-expanded={open}
                    onClick={() => setCollapsed((previous) => ({ ...previous, [project.id]: open }))}
                    className="flex w-full items-center justify-between bg-slate-50 p-5 text-left transition hover:bg-slate-100"
                  >
                    <div className="flex min-w-0 items-center gap-4">
                      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-gradient-to-r from-[#3B82F6] to-[#8B5CF6] text-sm font-bold text-white shadow-sm">
                        {project.project_name ? project.project_name.charAt(0).toUpperCase() : 'P'}
                      </div>
                      <div className="min-w-0">
                        <h3 className="truncate text-lg font-black text-slate-800">{project.project_name}</h3>
                        <p className="text-xs font-semibold text-slate-500">
                          {tasks.length} task{tasks.length === 1 ? '' : 's'} &bull; {project.employee_count} member
                          {project.employee_count === 1 ? '' : 's'}
                        </p>
                      </div>
                    </div>
                    <svg
                      className={`h-5 w-5 shrink-0 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7" />
                    </svg>
                  </button>

                  {open && (
                    <ul className="divide-y divide-slate-100">
                      {tasks.length === 0 ? (
                        <li className="p-6 text-center text-sm font-semibold text-slate-500">No tasks in this project.</li>
                      ) : (
                        tasks.map((task) => (
                          <li key={task.id} className="flex flex-wrap items-center justify-between gap-4 px-5 py-4 transition hover:bg-slate-50/50">
                            <div className="min-w-0 flex-1 basis-56">
                              <h4 className="truncate text-sm font-bold text-slate-700" title={task.name}>{task.name}</h4>
                            </div>
                            <div className="min-w-0 basis-56">
                              <Holders people={holdersOf(task)} />
                            </div>
                            <div className="flex items-center gap-3">
                              <StatusPill task={task} />
                              <button
                                type="button"
                                onClick={() => openDialog({ projectId: project.id, taskId: task.id })}
                                aria-label={`Edit ${task.name}`}
                                className="rounded-md border border-slate-200 px-3 py-1 text-xs font-bold text-slate-600 transition hover:border-[#3B82F6] hover:text-[#3B82F6]"
                              >
                                Edit
                              </button>
                            </div>
                          </li>
                        ))
                      )}
                    </ul>
                  )}
                </section>
              );
            })}

            {visible.length > PAGE_SIZES[0] && (
              <Pagination
                page={currentPage}
                totalPages={totalPages}
                totalItems={visible.length}
                limit={pageSize}
                setPage={setPage}
                setLimit={setPageSize}
                noun="projects"
                pageSizes={PAGE_SIZES}
                className="mt-2"
              />
            )}
          </div>
        )}
      </div>

      {dialog && (
        <AssignTaskDialog
          // A fresh dialog per open: its fields are seeded from the task being
          // edited, and must not carry over from the previous one.
          key={dialog.editing ? `edit-${dialog.editing.taskId}` : 'new'}
          projects={projects}
          statuses={metadata?.task_statuses ?? []}
          editing={dialog.editing}
          canCreateTasks={canCreateTasks}
          saving={saving}
          error={saveError}
          onSubmit={handleSubmit}
          onClose={() => setDialog(null)}
        />
      )}
    </V2Shell>
  );
};
