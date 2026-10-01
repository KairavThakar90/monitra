import React, { useEffect, useMemo, useState } from 'react';
import type { Project } from '../../store/api/projectsApi';
import { FieldError, FormErrorBanner, useFormValidation } from '../../validation';
import { AssigneeSelector } from './AdminProjectManagement';
import { ProjectPicker } from './AdminTaskListing';
import { holdersOf, memberOptions } from './assignTasks';

/**
 * What the drawer reports on Save: either members for a task that exists, or a
 * brand-new task together with its members -- created and assigned in one
 * request, never two.
 */
export type AssignTaskSubmit =
  | { kind: 'existing'; projectId: number; taskId: number; userIds: number[]; statusId: number }
  | { kind: 'new'; projectId: number; name: string; estimatedHours: number | null; userIds: number[]; statusId: number };

interface TaskStatusOption {
  id: number;
  task_status: string;
  color: string;
}

type Mode = 'existing' | 'new';

/**
 * "Assign Task": give a task to several members and set its status -- either a
 * task that already exists, or a new one created right here.
 *
 * A right-hand drawer, like Create Task. Presentational on purpose: it is
 * handed the projects and statuses and reports a submit, so the screen owns the
 * request and this can be exercised without a store. `editing` opens it on an
 * existing task with the project and task fixed (the Edit button).
 */
export const AssignTaskDialog: React.FC<{
  projects: Project[];
  statuses: TaskStatusOption[];
  editing?: { projectId: number; taskId: number } | null;
  /** Offer "New task". False when this account's Add Task switch is off. */
  canCreateTasks?: boolean;
  saving: boolean;
  /** A rejected save, shown above the fields. */
  error: string | null;
  onSubmit: (value: AssignTaskSubmit) => void;
  onClose: () => void;
}> = ({ projects, statuses, editing = null, canCreateTasks = true, saving, error, onSubmit, onClose }) => {
  const initialProject = editing ? projects.find((project) => project.id === editing.projectId) : undefined;
  const initialTask = editing ? initialProject?.tasks?.find((task) => task.id === editing.taskId) : undefined;
  const todoId = statuses[0]?.id ?? '';

  const [mode, setMode] = useState<Mode>('existing');
  const [projectId, setProjectId] = useState<number | ''>(initialProject?.id ?? '');
  const [taskId, setTaskId] = useState<number | ''>(initialTask?.id ?? '');
  const [taskName, setTaskName] = useState('');
  const [estimatedHours, setEstimatedHours] = useState('');
  const [userIds, setUserIds] = useState<number[]>(initialTask ? holdersOf(initialTask).map((p) => p.id) : []);
  const [statusId, setStatusId] = useState<number | ''>(initialTask?.status?.id ?? todoId);
  const [menuOpen, setMenuOpen] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [hoursError, setHoursError] = useState<string | null>(null);
  const [entered, setEntered] = useState(false);

  // The task name is validated by the shared catalogue rule, like the Create
  // Task drawer's: never a one-off check here.
  const nameForm = useFormValidation({ name: { rule: 'name', label: 'Task name', required: true } });

  const project = projects.find((candidate) => candidate.id === projectId);
  const task = project?.tasks?.find((candidate) => candidate.id === taskId);
  const creating = mode === 'new';
  const options = useMemo(() => memberOptions(project, creating ? undefined : task), [project, task, creating]);
  const taskItems = useMemo(
    // The picker's label field is called `project_name`; for tasks it carries
    // the task's name (see ProjectPicker).
    () => (project?.tasks ?? []).map((candidate) => ({ id: candidate.id, project_name: candidate.name })),
    [project],
  );
  const heldNow = !creating && task ? holdersOf(task).length : 0;
  const locked = editing !== null;
  // Members and status unlock once there is a task to give them to.
  const ready = creating ? projectId !== '' : task !== undefined;

  // Slide in on the frame after mounting, so the transition has a start state.
  useEffect(() => {
    const frame = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !saving && !menuOpen) onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose, saving, menuOpen]);

  const chooseMode = (next: Mode) => {
    if (next === mode) return;
    setMode(next);
    // A task, its members and its status belong to one choice of mode; carrying
    // them across would show an existing task's members on a new one.
    setTaskId('');
    setTaskName('');
    setEstimatedHours('');
    setHoursError(null);
    nameForm.clear();
    setUserIds([]);
    setStatusId(todoId);
    setMenuOpen(false);
    setAttempted(false);
  };

  const chooseProject = (value: number | '') => {
    setProjectId(value);
    // A task and its members belong to one project; carrying them across would
    // offer a task the new project does not have.
    setTaskId('');
    setUserIds([]);
    setStatusId(todoId);
    setMenuOpen(false);
  };

  const chooseTask = (value: number | '') => {
    setTaskId(value);
    const chosen = project?.tasks?.find((candidate) => candidate.id === value);
    // Open on what the task is now, so Save changes only what the admin touches.
    setUserIds(chosen ? holdersOf(chosen).map((person) => person.id) : []);
    setStatusId(chosen?.status?.id ?? todoId);
  };

  // An existing task nobody holds needs someone picked, or there is nothing to
  // save. Removing everyone from a task that has some is a real change, and is
  // allowed. A new task may start with no members: that is an unassigned,
  // shared task, exactly what Create Task makes.
  const missingMembers = !creating && task !== undefined && userIds.length === 0 && heldNow === 0;
  const errors = {
    project: projectId === '' ? 'Select a project.' : null,
    task: !creating && projectId !== '' && taskId === '' ? 'Select a task.' : null,
    members: missingMembers ? 'Select at least one member.' : null,
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    setAttempted(true);
    if (projectId === '' || statusId === '') return;

    if (creating) {
      const check = nameForm.validateAll({ name: taskName });
      if (!check.ok) return;
      const hours = estimatedHours.trim();
      if (hours !== '' && (!Number.isFinite(Number(hours)) || Number(hours) < 0)) {
        setHoursError('Estimated hours must be a number of 0 or more.');
        return;
      }
      setHoursError(null);
      onSubmit({
        kind: 'new',
        projectId,
        name: check.values.name as string,
        estimatedHours: hours === '' ? null : Number(hours),
        userIds,
        statusId,
      });
      return;
    }

    if (taskId === '' || missingMembers) return;
    onSubmit({ kind: 'existing', projectId, taskId, userIds, statusId });
  };

  const labelClass = 'mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500';
  const inputClass =
    'w-full rounded-lg border border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700 outline-none transition focus:border-[#3B82F6]';

  const memberHint =
    options.length === 0
      ? 'This project has no employee members yet. Add members to the project first.'
      : userIds.length === 0
        ? heldNow > 0
          ? 'Saving with no one selected unassigns the task: it becomes shared work every project member can see.'
          : 'A task with no members is shared work every project member can see.'
        : `${userIds.length} member${userIds.length === 1 ? '' : 's'} selected. Only they will see this task.`;

  return (
    <div className="fixed inset-0 z-50 overflow-hidden">
      <div
        className={`absolute inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity duration-300 ${entered ? 'opacity-100' : 'opacity-0'}`}
        onClick={() => !saving && onClose()}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Assign task"
        className={`absolute inset-y-0 right-0 w-full max-w-md bg-white shadow-2xl transition-transform duration-300 ease-in-out ${entered ? 'translate-x-0' : 'translate-x-full'}`}
      >
        <div className="flex h-full flex-col">
          <div className="flex items-center justify-between border-b border-slate-100 px-6 py-5">
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

            {canCreateTasks && !locked && (
              <div role="tablist" aria-label="Task" className="grid grid-cols-2 gap-1 rounded-lg bg-slate-100 p-1">
                {([['existing', 'Existing task'], ['new', 'New task']] as const).map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    role="tab"
                    aria-selected={mode === value}
                    onClick={() => chooseMode(value)}
                    className={
                      'rounded-md py-2 text-sm font-bold transition ' +
                      (mode === value ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700')
                    }
                  >
                    {label}
                  </button>
                ))}
              </div>
            )}

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

            {creating ? (
              <>
                <div>
                  <label className={labelClass}>
                    Task Name <span className="text-rose-500">*</span>
                  </label>
                  <input
                    type="text"
                    value={taskName}
                    onChange={(event) => setTaskName(event.target.value)}
                    onBlur={() => nameForm.validateField('name', taskName)}
                    placeholder="e.g. Design Homepage"
                    {...nameForm.fieldProps('name')}
                    className={inputClass}
                  />
                  <FieldError id={nameForm.errorId('name')} message={nameForm.errors.name} />
                </div>
                <div>
                  <label className={labelClass}>Estimated Hours</label>
                  <input
                    type="number"
                    min={0}
                    step={0.25}
                    value={estimatedHours}
                    onChange={(event) => setEstimatedHours(event.target.value)}
                    placeholder="Optional — the task's budgeted hours"
                    className={inputClass}
                  />
                  <FieldError message={hoursError} />
                </div>
              </>
            ) : (
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
            )}

            <div>
              <label className={labelClass}>
                Members {!creating && <span className="text-rose-500">*</span>}
              </label>
              {!ready ? (
                <div className="flex min-h-[46px] w-full items-center rounded-lg border border-slate-200 bg-slate-50 px-4 text-sm font-semibold text-slate-400">
                  {creating ? 'Select a project first' : 'Select a task first'}
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
                  <p className="mt-1 text-[11px] font-medium text-slate-400">{memberHint}</p>
                </>
              )}
              {attempted && <FieldError message={errors.members} />}
            </div>

            <div>
              <label className={labelClass}>Status</label>
              <select
                value={statusId}
                onChange={(event) => setStatusId(Number(event.target.value))}
                disabled={!ready}
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
    </div>
  );
};
