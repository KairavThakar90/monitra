// @vitest-environment jsdom
/**
 * The two inline controls on the Project Management table: the Team picker and
 * the Status pill.
 *
 * Reported: an administrator assigns a member to a project from the table and
 * then changes the project's status, and the member is gone. Two things caused
 * it, and both are pinned here against the real screen and a real RTK Query
 * store with only `fetch` stubbed:
 *
 * - A status change used to PATCH the project's *whole* shape, including
 *   `employee_ids` read from the row as it was rendered. Anything that had not
 *   landed in that row yet -- a member assigned a moment earlier, whose answer
 *   was still on its way -- was written back over. A status change must send
 *   the status and nothing else, and a team change the team and nothing else.
 * - Ticking a member changed nothing on screen until the server answered, so
 *   the checkbox looked ignored and the next click was computed from a list
 *   that was already out of date. The row now shows the new team at once.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

const showToast = vi.fn();

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser: { id: 1, role_name: 'administrator', name: 'Admin' } }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction: async () => true }),
}));

import { AdminProjectManagement } from '../AdminProjectManagement';

const PEOPLE = [
  { id: 301, name: 'Ada Lovelace', email: 'ada@example.invalid', role: 'employee' },
  { id: 302, name: 'Grace Hopper', email: 'grace@example.invalid', role: 'employee' },
];
const LEADER = { id: 801, name: 'Leader One', email: 'l1@example.invalid', role: 'leader' };
const STATUSES = [
  { id: 1, project_status: 'Active', color: '#22C55E' },
  { id: 2, project_status: 'On Hold', color: '#F59E0B' },
];

type Server = {
  employees: typeof PEOPLE;
  /** `null` is a project that predates status ids. */
  statusId: number | null;
};

type Call = { method: string; path: string; body: any };

let calls: Call[];
let server: Server;
/** When set, a PATCH of the team waits for this to be released before it answers. */
let holdTeamReply: Promise<void> | null;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const projectBody = () => {
  const status = STATUSES.find((item) => item.id === server.statusId);
  return {
    id: 55,
    project_name: 'Existing project',
    description: '',
    status: status ? { id: status.id, name: status.project_status, color: status.color } : null,
    owner: null,
    leader: LEADER,
    employees: server.employees,
    deadline: '2099-06-30',
    billing_type: 'free',
    fixed_hours: null,
    organization_id: 1,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    tasks: [],
    employee_count: server.employees.length,
    task_count: 0,
  };
};

const route = async (request: Request): Promise<Response> => {
  const url = new URL(request.url);
  const path = url.pathname;
  const body = request.method === 'GET' ? null : JSON.parse((await request.clone().text()) || 'null');
  calls.push({ method: request.method, path, body });

  if (path.endsWith('/project-management/metadata')) {
    return json({ roles: [], project_statuses: STATUSES, task_statuses: [] });
  }
  if (path.endsWith('/projects/assignable-owners')) return json([]);
  if (path.endsWith('/projects/assignable-leaders')) return json([LEADER]);
  if (path.endsWith('/projects/assignable-employees')) return json(PEOPLE);
  if (path.endsWith('/projects/hours-summary')) return json({ items: [] });
  if (path.endsWith('/members')) return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
  if (path.endsWith('/projects') && request.method === 'GET') {
    return json({ items: [projectBody()], pagination: { page: 1, limit: 20, total: 1, total_pages: 1 } });
  }
  if (/\/projects\/55$/.test(path) && request.method === 'PATCH') {
    // The real endpoint applies only the keys that were sent.
    if (body.employee_ids !== undefined) {
      if (holdTeamReply) await holdTeamReply;
      server.employees = PEOPLE.filter((person) => body.employee_ids.includes(person.id));
    }
    if (body.status_id !== undefined) server.statusId = body.status_id;
    return json(projectBody());
  }
  return json({ detail: 'Not found' }, 404);
};

let container: HTMLDivElement;
let root: Root;

const flush = async () => {
  for (let i = 0; i < 5; i += 1) {
    await act(async () => { await new Promise((resolveTick) => setTimeout(resolveTick, 0)); });
  }
};

const renderPage = async () => {
  const store = configureStore({
    reducer: { [baseApi.reducerPath]: baseApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(baseApi.middleware),
  });
  await act(async () => {
    root.render(<Provider store={store}><AdminProjectManagement /></Provider>);
  });
  await flush();
};

const click = async (element: Element | null | undefined) => {
  expect(element, 'element to click').toBeTruthy();
  await act(async () => { (element as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true })); });
};

const row = () => Array.from(container.querySelectorAll('tbody tr')).find((item) => item.textContent?.includes('Existing project'))!;

/** The avatar stack / "+" marker that opens the Team picker in the row. */
const openTeamPicker = async () => {
  await click(row().querySelector('.cursor-pointer.group'));
};

const memberCheckbox = (name: string) => {
  const label = Array.from(document.body.querySelectorAll('label')).find((item) => item.textContent?.includes(name));
  return label?.querySelector<HTMLInputElement>('input[type="checkbox"]') ?? null;
};

const closeMenu = async () => {
  await click(document.body.querySelector('.fixed.inset-0'));
};

const chooseStatus = async (from: string, to: string) => {
  const pill = Array.from(row().querySelectorAll('button')).find((button) => button.textContent?.trim() === from);
  await click(pill);
  const option = Array.from(document.body.querySelectorAll('button')).find((button) => button.textContent?.trim() === to);
  await click(option);
};

const patches = () => calls.filter((call) => call.method === 'PATCH');

beforeEach(() => {
  calls = [];
  server = { employees: [], statusId: 1 };
  holdTeamReply = null;
  showToast.mockReset();
  const entries = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => entries.set(key, value),
    removeItem: (key: string) => entries.delete(key),
    clear: () => entries.clear(),
  });
  vi.stubGlobal('fetch', vi.fn(route));
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => { root.unmount(); });
  container.remove();
  vi.unstubAllGlobals();
});

describe('Project Management — inline Team and Status', () => {
  it('a status change sends the status and nothing else', async () => {
    await renderPage();
    await chooseStatus('Active', 'On Hold');
    await flush();

    expect(patches()).toHaveLength(1);
    expect(patches()[0].body).toEqual({ status_id: 2 });
  });

  it('a team change sends the team and nothing else', async () => {
    await renderPage();
    await openTeamPicker();
    await click(memberCheckbox('Ada Lovelace'));
    await flush();

    expect(patches()).toHaveLength(1);
    expect(patches()[0].body).toEqual({ employee_ids: [301] });
  });

  it('keeps the member after the status is changed once the assignment has landed', async () => {
    await renderPage();
    await openTeamPicker();
    await click(memberCheckbox('Ada Lovelace'));
    await flush();
    await closeMenu();

    await chooseStatus('Active', 'On Hold');
    await flush();

    expect(server.employees.map((person) => person.id)).toEqual([301]);
    expect(server.statusId).toBe(2);
    expect(patches().map((call) => call.body)).toEqual([{ employee_ids: [301] }, { status_id: 2 }]);
    expect(row().textContent).toContain('On Hold');
    expect(row().querySelector('[title="Ada Lovelace"]')).toBeTruthy();
    expect(showToast).not.toHaveBeenCalled();
  });

  it('keeps the member when the status is changed before the assignment has been answered', async () => {
    let release!: () => void;
    holdTeamReply = new Promise<void>((resolve) => { release = resolve; });
    await renderPage();

    await openTeamPicker();
    await click(memberCheckbox('Ada Lovelace'));
    await closeMenu();
    await chooseStatus('Active', 'On Hold');
    await flush();
    await act(async () => { release(); });
    await flush();

    const statusPatch = patches().find((call) => call.body.status_id !== undefined)!;
    expect(statusPatch.body).toEqual({ status_id: 2 });
    expect(server.employees.map((person) => person.id)).toEqual([301]);
    expect(server.statusId).toBe(2);
    expect(row().querySelector('[title="Ada Lovelace"]')).toBeTruthy();
  });

  it('shows a ticked member at once, before the server has answered', async () => {
    let release!: () => void;
    holdTeamReply = new Promise<void>((resolve) => { release = resolve; });
    await renderPage();

    await openTeamPicker();
    await click(memberCheckbox('Ada Lovelace'));
    await flush();

    expect(memberCheckbox('Ada Lovelace')!.checked).toBe(true);
    expect(row().querySelector('[title="Ada Lovelace"]')).toBeTruthy();

    await act(async () => { release(); });
    await flush();
    expect(memberCheckbox('Ada Lovelace')!.checked).toBe(true);
  });

  it('a project with no status says so, and assigning a member does not give it one', async () => {
    server.statusId = null;
    await renderPage();

    expect(row().textContent).toContain('Select Status');
    expect(row().textContent).not.toContain('Active');

    await openTeamPicker();
    await click(memberCheckbox('Ada Lovelace'));
    await flush();

    expect(patches()).toHaveLength(1);
    expect(patches()[0].body).toEqual({ employee_ids: [301] });
    expect(server.statusId).toBeNull();
    expect(row().textContent).toContain('Select Status');
  });

  it('two members ticked in quick succession are both kept', async () => {
    let release!: () => void;
    holdTeamReply = new Promise<void>((resolve) => { release = resolve; });
    await renderPage();

    await openTeamPicker();
    await click(memberCheckbox('Ada Lovelace'));
    await click(memberCheckbox('Grace Hopper'));
    await act(async () => { release(); });
    await flush();

    const bodies = patches().map((call) => call.body.employee_ids);
    expect(bodies).toEqual([[301], [301, 302]]);
    expect(server.employees.map((person) => person.id)).toEqual([301, 302]);
  });
});
