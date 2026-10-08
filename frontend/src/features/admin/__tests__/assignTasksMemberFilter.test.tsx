// @vitest-environment jsdom
/**
 * Assign Tasks: the member filter -- the Reports page's `MemberMultiSelect`, in the same toolbar as
 * the project and date filters.
 *
 * It opens on everyone (nothing narrowed), lists the people who can hold a task, and picking one or
 * several narrows the page to the tasks they hold. It composes with the project filter and the
 * search, says so when nothing matches, and Reset-by-clearing brings everything back.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { MemoryRouter } from 'react-router-dom';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));
vi.mock('../../auth/authContext', () => ({ useAuth: () => ({ currentUser: { id: 1, role: 'administrator', can_add_tasks: true } }) }));

import { AdminAssignTasks } from '../AdminAssignTasks';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const person = (id: number, name: string, role = 'employee') => ({ id, name, email: `${name.toLowerCase()}@example.com`, role });
const ana = person(1, 'Ana Active');
const ben = person(2, 'Ben Builder');
const cal = person(3, 'Cal Casual');
const dan = person(4, 'Dan Dormant');                                 // on a project, holds no task
const manager = person(9, 'Mona Manager', 'manager');
const client = person(8, 'Client Co', 'client');

const now = new Date().toISOString();
const task = (id: number, projectId: number, name: string, assignees: ReturnType<typeof person>[] = []) => ({
  id, project_id: projectId, name,
  assignee: assignees[0] ?? null, assignees,
  status: { id: 1, name: 'Todo', color: '#CBD5E1' },
  estimated_hours: null, created_at: now, updated_at: now,
});
const project = (id: number, name: string, tasks: ReturnType<typeof task>[], employees: ReturnType<typeof person>[]) => ({
  id, project_name: name, description: '', status: { id: 1, name: 'Active', color: '#10B981' }, leader: null,
  employees, deadline: null, billing_type: 'free', fixed_hours: null, organization_id: 1,
  created_at: now, updated_at: now, tasks, employee_count: employees.length, task_count: tasks.length,
});

const PROJECTS = [
  project(10, 'Alpha Portal', [
    task(1, 10, 'Wire up login', [ana]),
    task(2, 10, 'Write the docs', [ben]),
    task(3, 10, 'Fix the build', [ana, ben]),
    task(4, 10, 'Triage the backlog'),
  ], [ana, ben, manager, client]),
  project(11, 'Beta Redesign', [
    task(5, 11, 'Design the logo', [cal]),
    task(6, 11, 'Pick the colours', [ana]),
  ], [ana, cal, dan]),
];

describe('Assign Tasks: member filter', () => {
  let container: HTMLDivElement;
  let root: Root;

  const flush = async () => {
    for (let i = 0; i < 6; i += 1) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const click = async (el: Element | undefined) => {
    expect(el).toBeTruthy();
    await act(async () => { el!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    await flush();
  };
  const buttonByText = (text: string) =>
    Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.includes(text));
  const memberOption = (name: string) =>
    Array.from(container.querySelectorAll('.custom-scrollbar button')).find((b) => b.textContent?.includes(name));
  const projectOption = (name: string) =>
    Array.from(container.querySelectorAll('li button')).find((b) => b.textContent?.includes(name));
  const taskNames = () => Array.from(container.querySelectorAll('h4')).map((h) => h.textContent);
  const projectNames = () => Array.from(container.querySelectorAll('section h3')).map((h) => h.textContent);
  const typeInSearch = async (value: string) => {
    const input = container.querySelector('input[type="search"]') as HTMLInputElement;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    await act(async () => { input.dispatchEvent(new Event('input', { bubbles: true })); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 300)); });
    await flush();
  };

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    window.localStorage.clear();
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      if (url.pathname.endsWith('/projects/metadata')) return json({ task_statuses: [{ id: 1, name: 'Todo', color: '#CBD5E1' }] });
      if (url.pathname.endsWith('/projects')) {
        return json({ items: PROJECTS, pagination: { page: 1, limit: 100, total: PROJECTS.length, total_pages: 1 } });
      }
      return json({}, 404);
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(
        <Provider store={store}>
          <MemoryRouter><AdminAssignTasks /></MemoryRouter>
        </Provider>,
      );
    });
    // wait until the projects have rendered, however slowly this machine answers
    for (let i = 0; i < 400 && !container.textContent?.includes('Alpha Portal'); i += 1) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    }
    await flush();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('puts the Reports page member filter in the toolbar, on everyone, narrowing nothing', () => {
    expect(buttonByText('All members')).toBeTruthy();
    expect(projectNames()).toEqual(['Alpha Portal', 'Beta Redesign']);
    expect(taskNames()).toEqual([
      'Wire up login', 'Write the docs', 'Fix the build', 'Triage the backlog', 'Design the logo', 'Pick the colours',
    ]);
  });

  it('sits beside the project and date filters, before them, as on the Reports page', () => {
    const labels = Array.from(container.querySelectorAll('button')).map((b) => b.textContent ?? '');
    const member = labels.findIndex((l) => l.includes('All members'));
    const projects = labels.findIndex((l) => l.includes('All projects'));
    expect(member).toBeGreaterThan(-1);
    expect(projects).toBeGreaterThan(member);
  });

  it('lists the people who can hold a task, each once, in name order -- no manager, no client', async () => {
    await click(buttonByText('All members'));

    const options = Array.from(container.querySelectorAll('.custom-scrollbar button')).map((b) => b.textContent ?? '');
    expect(options.map((o) => o.replace(/employee$/, ''))).toEqual(['AAAna Active', 'BBBen Builder', 'CCCal Casual', 'DDDan Dormant']);
    expect(container.querySelector('input[placeholder="Search members..."]')).toBeTruthy();   // the same searchable list
    expect(container.textContent).not.toContain('Mona Manager');
    expect(container.textContent).not.toContain('Client Co');
  });

  it('shows only the tasks the chosen member holds, and drops a project left with none', async () => {
    await click(buttonByText('All members'));
    await click(memberOption('Cal Casual'));

    expect(projectNames()).toEqual(['Beta Redesign']);
    expect(taskNames()).toEqual(['Design the logo']);
    expect(container.textContent).toContain('1 task');
  });

  it('shows a task held by several members when any one of them is chosen', async () => {
    await click(buttonByText('All members'));
    await click(memberOption('Ben Builder'));

    expect(taskNames()).toEqual(['Write the docs', 'Fix the build']);
  });

  it('is the union of every member picked, across projects', async () => {
    await click(buttonByText('All members'));
    await click(memberOption('Ben Builder'));
    await click(memberOption('Cal Casual'));

    expect(projectNames()).toEqual(['Alpha Portal', 'Beta Redesign']);
    expect(taskNames()).toEqual(['Write the docs', 'Fix the build', 'Design the logo']);
  });

  it('does not show the unassigned (shared) task while a member is chosen', async () => {
    await click(buttonByText('All members'));
    await click(memberOption('Ana Active'));

    expect(taskNames()).not.toContain('Triage the backlog');
    expect(taskNames()).toEqual(['Wire up login', 'Fix the build', 'Pick the colours']);
  });

  it('composes with the project filter', async () => {
    await click(buttonByText('All members'));
    await click(memberOption('Ana Active'));
    await click(buttonByText('All members') ?? container.querySelector('button[aria-haspopup="listbox"]') ?? undefined);   // close the list
    await click(buttonByText('All projects'));
    await click(projectOption('Beta Redesign'));

    expect(projectNames()).toEqual(['Beta Redesign']);
    expect(taskNames()).toEqual(['Pick the colours']);
  });

  it('composes with the search box', async () => {
    await click(buttonByText('All members'));
    await click(memberOption('Ana Active'));
    await typeInSearch('build');

    expect(taskNames()).toEqual(['Fix the build']);
  });

  it('says so, naming the member filter, when nothing matches', async () => {
    await click(buttonByText('All members'));
    await click(memberOption('Cal Casual'));
    await typeInSearch('login');                                   // Cal holds nothing called that

    expect(container.textContent).toContain('No matching projects or tasks');
    expect(container.textContent).toContain('current search, member, project and date filters');
  });

  it('says nothing matches, rather than that there are no projects, when only the member filter is narrowing', async () => {
    // The date filter opens on the last 7 days, which already counts as filtering; All Time takes it out of play.
    await click(buttonByText('Last 7 days'));
    await click(Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'All Time'));
    expect(buttonByText('All Time')).toBeTruthy();
    await click(buttonByText('All members'));
    await click(memberOption('Dan Dormant'));                      // a project member who holds no task

    expect(taskNames()).toEqual([]);
    expect(container.textContent).toContain('No matching projects or tasks');
    expect(container.textContent).not.toContain('No projects yet');
  });

  it('goes back to everyone when the selection is cleared', async () => {
    await click(buttonByText('All members'));
    await click(memberOption('Cal Casual'));
    expect(taskNames()).toEqual(['Design the logo']);

    await click(buttonByText('Clear'));

    expect(taskNames().length).toBe(6);
    expect(projectNames()).toEqual(['Alpha Portal', 'Beta Redesign']);
  });

  it('shows the picked member as an avatar in the button, as on the Reports page', async () => {
    await click(buttonByText('All members'));
    await click(memberOption('Cal Casual'));

    expect(buttonByText('All members')).toBeUndefined();
    expect(container.querySelector('button[aria-haspopup="listbox"] [title="Cal Casual"]')).toBeTruthy();
  });

  it('leaves the Edit button of a shown task working', async () => {
    await click(buttonByText('All members'));
    await click(memberOption('Cal Casual'));

    expect(container.querySelector('button[aria-label="Edit Design the logo"]')).toBeTruthy();
  });
});
