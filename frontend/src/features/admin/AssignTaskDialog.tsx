import React, { useEffect, useMemo, useState } from 'react';
import type { Project } from '../../store/api/projectsApi';
import { FieldError, FormErrorBanner } from '../../validation';
import { AssigneeSelector } from './AdminProjectManagement';
import { ProjectPicker } from './AdminTaskListing';
import { holdersOf, memberOptions } from './assignTasks';

export interface AssignTaskSubmit {
  projectId: number;
  taskId: number;
  userIds: number[];
  statusId: number;
}

interface TaskStatusOption {
  id: number;
  task_status: string;
  color: string;
}

/**
 * "Assign Task": give one task to several members, and set its status.
 *
 * Presentational on purpose -- it is handed the projects and statuses and
 * reports a submit, so the screen owns the request and this can be exercised
 * without a store. `editing` opens it on an existing task with the project and
 * task fixed (the Edit button); without it, both are chosen here.
 */
export const AssignTaskDialog: React.FC<{
  projects: Project[];
  statuses: TaskStatusOption[];
  editing?: { projectId: number; taskId: number } | null;
  saving: boolean;
  /** A rejected save, shown above the buttons. */
  error: string | null;
  onSubmit: (value: AssignTaskSubmit) => void;
  onClose: () => void;
}> = ({ projects, statuses, editing = null, saving, error, onSubmit, onClose }) => {
  const initialProject = editing ? projects.find((project) => project.id === editing.projectId) : undefined;
  const initialTask = editing ? initialProject?.tasks?.find((task) => task.id === editing.taskId) : undefined;

  const [projectId, setProjectId] = useState<number | ''>(initialProject?.id ?? '');
  const [taskId, setTaskId] = useState<number | ''>(initialTask?.id ?? '');
  const [userIds, setUserIds] = useState<number[]>(initialTask ? holdersOf(initialTask).map((p) => p.id) : []);
  const [statusId, setStatusId] = useState<number | ''>(initialTask?.status?.id ?? statuses[0]?.id ?? '');
  const [menuOpen, setMenuOpen] = useState(false);
  const [attempted, setAttempted] = useState(false);

  const project = projects.find((candidate) => candidate.id === projectId);
  const task = project?.tasks?.find((candidate) => candidate.id === taskId);
  const options = useMemo(() => memberOptions(project, task), [project, task]);
  const taskItems = useMemo(
    // The picker's label field is called `project_name`; for tasks it carries
    // the task's name (see ProjectPicker).
    () => (project?.tasks ?? []).map((candidate) => ({ id: candidate.id, project_name: candidate.name })),
    [project],
  );
  const heldNow = task ? holdersOf(task).length : 0;
  const locked = editing !== null;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !saving && !menuOpen) onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose, saving, menuOpen]);

  const chooseProject = (value: number | '') => {
    setProjectId(value);
    // A task and its members belong to one project; carrying them across would
    // offer a task the new project does not have.
    setTaskId('');
    setUserIds([]);
    setStatusId(statuses[0]?.id ?? '');
    setMenuOpen(false);
  };

  const chooseTask = (value: number | '') => {
    setTaskId(value);
    const chosen = project?.tasks?.find((candidate) => candidate.id === value);
    // Open on what the task is now, so Save changes only what the admin touches.
    setUserIds(chosen ? holdersOf(chosen).map((person) => person.id) : []);
    setStatusId(chosen?.status?.id ?? statuses[0]?.id ?? '');
  };

  // Nothing to save when the task has no members and none were picked. Removing
  // everyone from a task that has some is a real change, and is allowed.
  const missingMembers = task !== undefined && userIds.length === 0 && heldNow === 0;
  const errors = {
    project: projectId === '' ? 'Select a project.' : null,
    task: projectId !== '' && taskId === '' ? 'Select a task.' : null,
    members: missingMembers ? 'Select at least one member.' : null,
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    setAttempted(true);
    if (projectId === '' || taskId === '' || statusId === '' || missingMembers) return;
    onSubmit({ projectId, taskId, userIds, statusId });
  };

  const labelClass = 'mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-slate-900/40 backdrop-blur-sm"
        onClick={() => !saving && onClose()}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Assign task"
        className="relative flex max-h-[92vh] w-full max-w-md flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl"
      >
        <div className="flex items-start justify-between gap-4 border-b border-slate-100 px-6 py-5">
          <div>
            <h2 className="text-xl font-black text-slate-800">Assign Task</h2>
            <p className="mt-1 text-sm font-semibold text-slate-500">Assign task to multiple members</p>
          </div>
          <button
            type="button"
            onClick={() => !saving && onClose()}
            aria-label="Close"
            className="rounded-full p-2 text-slate-400 hover:bg-slate-50 hover:text-slate-600"
          >
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <form id="assign-task-form" onSubmit={submit} noValidate className="flex-1 space-y-6 overflow-y-auto p-6">
          <FormErrorBanner message={error} />

          <div>
            <label className={labelClass}>
              Project <span className="text-rose-500">*</span>
            </label>
            <ProjectPicker
              projects={projects}
              value={projectId}
              onChange={chooseProject}
              allLabel={null}
              disabled={locked}
            />
            {attempted && <FieldError message={errors.project} />}
          </div>

          <div>
            <label className={labelClass}>
              Task <span className="text-rose-500">*</span>
            </label>
            <ProjectPicker
              projects={taskItems}
              value={taskId}
              onChange={chooseTask}
              placeholder={projectId === '' ? 'Select a project first' : 'Select Task'}
              searchPlaceholder="Search tasks..."
              allLabel={null}
              emptyText="This project has no tasks."
              disabled={locked || projectId === ''}
            />
            {attempted && <FieldError message={errors.task} />}
          </div>

          <div>
            <label className={labelClass}>
              Members <span className="text-rose-500">*</span>
            </label>
            {task === undefined ? (
              <div className="flex min-h-[46px] w-full items-center rounded-lg border border-slate-200 bg-slate-50 px-4 text-sm font-semibold text-slate-400">
                Select a task first
              </div>
            ) : (
              <>
                <AssigneeSelector
                  fullWidth
                  selectedIds={userIds}
                  options={options}
                  onChange={setUserIds}
                  isOpen={menuOpen}
                  setIsOpen={setMenuOpen}
                  onClose={() => setMenuOpen(false)}
                />
                <p className="mt-1 text-[11px] font-medium text-slate-400">
                  {options.length === 0
                    ? 'This project has no employee members yet. Add members to the project first.'
                    : userIds.length === 0
                      ? heldNow > 0
                        ? 'Saving with no one selected unassigns the task: it becomes shared work every project member can see.'
                        : 'A task with no members is shared work every project member can see.'
                      : `${userIds.length} member${userIds.length === 1 ? '' : 's'} selected. Only they will see this task.`}
                </p>
              </>
            )}
            {attempted && <FieldError message={errors.members} />}
          </div>

          <div>
            <label className={labelClass}>Status</label>
            <select
              value={statusId}
              onChange={(event) => setStatusId(Number(event.target.value))}
              disabled={task === undefined}
              className="w-full rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm font-semibold text-slate-700 outline-none transition focus:border-[#3B82F6] disabled:bg-slate-50 disabled:text-slate-400"
            >
              {statuses.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.task_status}
                </option>
              ))}
            </select>
          </div>
        </form>

        <div className="flex gap-3 border-t border-slate-100 bg-slate-50 p-6">
          <button
            type="button"
            onClick={() => !saving && onClose()}
            className="flex-1 rounded-lg border border-slate-200 bg-white py-3 text-sm font-bold text-slate-700 transition hover:bg-slate-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            form="assign-task-form"
            disabled={saving}
            className="flex-1 rounded-lg bg-gradient-to-r from-[#3B82F6] to-[#8B5CF6] py-3 text-sm font-bold text-white shadow-md transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
};
