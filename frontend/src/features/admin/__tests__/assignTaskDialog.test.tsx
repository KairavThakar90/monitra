// @vitest-environment jsdom
/**
 * The Assign Task dialog, driven the way a person drives it: open the Project
 * picker, pick, open the Task picker, pick, tick members, press Save.
 *
 * What is pinned here is the contract with the screen -- what `onSubmit`
 * receives -- and the rules the dialog owns: members and status follow the
 * task, only employees of the chosen project are offered, an empty selection is
 * refused for a task nobody holds but allowed as "unassign" for one somebody
 * does, and Edit opens with the project and task fixed.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Project, ProjectTask, ProjectUser } from '../../../store/api/projectsApi';
import { AssignTaskDialog, type AssignTaskSubmit } from '../AssignTaskDialog';

// Tell React these tests drive it through act(), as the rest of the suite does.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const person = (id: number, name: string, role = 'employee'): ProjectUser => ({
  id, name, email: `${name.toLowerCase()}@example.com`, role,
});
const ana = person(1, 'Ana');
const ben = person(2, 'Ben');
const cal = person(3, 'Cal');
const lead = person(9, 'Lena', 'leader');

const task = (id: number, name: string, statusId = 1, assignees: ProjectUser[] = []): ProjectTask => ({
  id,
  project_id: 1,
  name,
  assignee: assignees[0] ?? null,
  assignees,
  status: { id: statusId, name: statusId === 1 ? 'Todo' : 'In Progress', color: '#3B82F6' },
  estimated_hours: null,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
});

const project = (id: number, name: string, tasks: ProjectTask[], employees: ProjectUser[]): Project => ({
  id,
  project_name: name,
  description: '',
  status: { id: 1, name: 'Active', color: '#10B981' },
  leader: null,
  employees,
  deadline: null,
  billing_type: 'free',
  fixed_hours: null,
  organization_id: 1,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
  tasks,
  employee_count: employees.length,
  task_count: tasks.length,
});

const PROJECTS = [
  project(1, 'Website', [task(10, 'Design homepage'), task(11, 'Write copy', 2, [ana, ben])], [ana, ben, cal, lead]),
  project(2, 'Mobile', [task(20, 'Login screen')], [cal]),
  project(3, 'Empty', [], []),
];
const STATUSES = [
  { id: 1, task_status: 'Todo', color: '#CBD5E1' },
  { id: 2, task_status: 'In Progress', color: '#3B82F6' },
  { id: 3, task_status: 'Completed', color: '#10B981' },
];

describe('AssignTaskDialog', () => {
  let container: HTMLDivElement;
  let root: Root;
  let submitted: AssignTaskSubmit[];
  let closed: number;

  beforeEach(() => {
    submitted = [];
    closed = 0;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    vi.stubGlobal('innerHeight', 900);
    vi.stubGlobal('innerWidth', 1200);
  });

  afterEach(async () => {
    await act(async () => { root.unmount(); });
    container.remove();
    vi.unstubAllGlobals();
  });

  const render = async (props: Partial<React.ComponentProps<typeof AssignTaskDialog>> = {}) => {
    await act(async () => {
      root.render(
        <AssignTaskDialog
          projects={PROJECTS}
          statuses={STATUSES}
          saving={false}
          error={null}
          onSubmit={(value) => submitted.push(value)}
          onClose={() => { closed += 1; }}
          {...props}
        />,
      );
    });
  };

  const click = async (element: Element | null) => {
    expect(element, 'element to click').not.toBeNull();
    await act(async () => {
      (element as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
  };

  const field = (label: string) =>
    [...container.querySelectorAll('label')].find((l) => l.textContent?.trim().startsWith(label))!.parentElement!;

  /** Open a ProjectPicker field and pick the option with this text. */
  const pick = async (label: string, option: string) => {
    await click(field(label).querySelector('button[aria-haspopup="listbox"]'));
    const choice = [...field(label).querySelectorAll('[role="option"]')].find((o) => o.textContent === option);
    await click(choice ?? null);
  };

  const membersTrigger = () => field('Members').querySelector('[role="button"]');
  const menu = () => document.body.querySelector('.fixed.z-50.w-64') as HTMLElement | null;
  const menuRows = () => [...(menu()?.querySelectorAll('label') ?? [])].slice(1);   // [0] is "Select all"
  const rowFor = (name: string) => menuRows().find((r) => r.textContent?.includes(name))!;
  const box = (name: string) => rowFor(name).querySelector('input') as HTMLInputElement;
  const save = async () => click(document.querySelector('button[type="submit"]'));
  const statusSelect = () => field('Status').querySelector('select') as HTMLSelectElement;

  it('has the title, subtitle, and Cancel / Save of the design', async () => {
    await render();
    expect(container.textContent).toContain('Assign Task');
    expect(container.textContent).toContain('Assign task to multiple members');
    const buttons = [...container.querySelectorAll('button')].map((b) => b.textContent?.trim());
    expect(buttons).toContain('Cancel');
    expect(buttons).toContain('Save');
  });

  it('does not let a task be chosen before its project', async () => {
    await render();
    expect(field('Task').querySelector('button[aria-haspopup="listbox"]')!.hasAttribute('disabled')).toBe(true);
    expect(field('Members').textContent).toContain('Select a task first');
  });

  it('offers only the chosen project\'s tasks, and a clear message for a project with none', async () => {
    await render();
    await pick('Project', 'Website');
    await click(field('Task').querySelector('button[aria-haspopup="listbox"]'));
    expect([...field('Task').querySelectorAll('[role="option"]')].map((o) => o.textContent))
      .toEqual(['Design homepage', 'Write copy']);
    await click(field('Task').querySelector('button[aria-haspopup="listbox"]'));   // close it again

    await pick('Project', 'Empty');
    await click(field('Task').querySelector('button[aria-haspopup="listbox"]'));
    expect(field('Task').textContent).toContain('This project has no tasks.');
  });

  it('assigns one task to several members in one save', async () => {
    await render();
    await pick('Project', 'Website');
    await pick('Task', 'Design homepage');
    await click(membersTrigger());
    await click(box('Ana'));
    await click(box('Cal'));
    await click(box('Ben'));
    await save();
    expect(submitted).toEqual([{ projectId: 1, taskId: 10, userIds: [1, 3, 2], statusId: 1 }]);
  });

  it('offers only the project\'s employees -- never its leader', async () => {
    await render();
    await pick('Project', 'Website');
    await pick('Task', 'Design homepage');
    await click(membersTrigger());
    expect(menuRows().map((r) => r.textContent)).toEqual([
      expect.stringContaining('Ana'), expect.stringContaining('Ben'), expect.stringContaining('Cal'),
    ]);
    expect(menu()!.textContent).not.toContain('Lena');
  });

  it('opens a task on its current members and status, so Save changes only what is touched', async () => {
    await render();
    await pick('Project', 'Website');
    await pick('Task', 'Write copy');
    expect(statusSelect().value).toBe('2');
    await click(membersTrigger());
    expect(box('Ana').checked).toBe(true);
    expect(box('Ben').checked).toBe(true);
    expect(box('Cal').checked).toBe(false);
  });

  it('adds and removes in one save, and changes the status with it', async () => {
    await render();
    await pick('Project', 'Website');
    await pick('Task', 'Write copy');
    await click(membersTrigger());
    await click(box('Ana'));      // remove
    await click(box('Cal'));      // add
    await act(async () => {
      statusSelect().value = '3';
      statusSelect().dispatchEvent(new Event('change', { bubbles: true }));
    });
    await save();
    expect(submitted).toEqual([{ projectId: 1, taskId: 11, userIds: [2, 3], statusId: 3 }]);
  });

  it('changing the project clears the task and its members', async () => {
    await render();
    await pick('Project', 'Website');
    await pick('Task', 'Write copy');
    await pick('Project', 'Mobile');
    expect(field('Task').textContent).toContain('Select Task');
    expect(field('Members').textContent).toContain('Select a task first');
    await save();
    expect(submitted).toEqual([]);
  });

  it('refuses to save with nothing chosen, and says what is missing', async () => {
    await render();
    await save();
    expect(submitted).toEqual([]);
    expect(container.textContent).toContain('Select a project.');
  });

  it('refuses a task nobody holds when no member was picked', async () => {
    await render();
    await pick('Project', 'Website');
    await pick('Task', 'Design homepage');
    await save();
    expect(submitted).toEqual([]);
    expect(container.textContent).toContain('Select at least one member.');
  });

  it('lets an admin unassign a task that has members, and warns what that means', async () => {
    await render();
    await pick('Project', 'Website');
    await pick('Task', 'Write copy');
    await click(membersTrigger());
    await click(box('Ana'));
    await click(box('Ben'));
    expect(field('Members').textContent).toContain('unassigns the task');
    await save();
    expect(submitted).toEqual([{ projectId: 1, taskId: 11, userIds: [], statusId: 2 }]);
  });

  it('says so when the project has no employees to offer', async () => {
    await render({ projects: [project(5, 'Solo', [task(50, 'Only task')], [lead])] });
    await pick('Project', 'Solo');
    await pick('Task', 'Only task');
    expect(field('Members').textContent).toContain('no employee members yet');
  });

  it('Edit opens on the task with project and task fixed', async () => {
    await render({ editing: { projectId: 1, taskId: 11 } });
    expect(field('Project').querySelector('button[aria-haspopup="listbox"]')!.textContent).toContain('Website');
    expect(field('Project').querySelector('button[aria-haspopup="listbox"]')!.hasAttribute('disabled')).toBe(true);
    expect(field('Task').querySelector('button[aria-haspopup="listbox"]')!.textContent).toContain('Write copy');
    expect(field('Task').querySelector('button[aria-haspopup="listbox"]')!.hasAttribute('disabled')).toBe(true);
    await click(membersTrigger());
    expect(box('Ana').checked && box('Ben').checked).toBe(true);
    await click(box('Cal'));
    await save();
    expect(submitted).toEqual([{ projectId: 1, taskId: 11, userIds: [1, 2, 3], statusId: 2 }]);
  });

  it('shows a rejected save, keeps the dialog open, and blocks a second click while saving', async () => {
    await render({ error: 'Task assignee must be assigned to this project.' });
    expect(container.textContent).toContain('Task assignee must be assigned to this project.');
    await render({ saving: true });
    expect(document.querySelector('button[type="submit"]')!.hasAttribute('disabled')).toBe(true);
    expect(closed).toBe(0);
  });

  it('Cancel closes without submitting', async () => {
    await render();
    await click([...container.querySelectorAll('button')].find((b) => b.textContent === 'Cancel') ?? null);
    expect(closed).toBe(1);
    expect(submitted).toEqual([]);
  });
});
