// @vitest-environment jsdom
/**
 * The Owner field of the create/edit project drawer.
 *
 * A project's Owner is a project-level relationship: the backend decides who
 * may hold it (`users.can_own_projects`) and serves that list from
 * `GET /projects/assignable-owners`. These tests render the real screen
 * against a real RTK Query store, with only `fetch` stubbed, so what they pin
 * is what a browser would do:
 *
 * - the Owner field sits before Leader and Deadline, and its options are
 *   exactly what the API returned (no names are baked into the client);
 * - the stable user id is what gets submitted, not the display name;
 * - a missing owner is caught before the request, and a backend refusal is
 *   shown in the backend's own words;
 * - editing preselects the current owner, a change is saved, a project that
 *   predates owners saves without one, and a leader cannot change it.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import pageSource from '../AdminProjectManagement.tsx?raw';
import projectsApiSource from '../../../store/api/projectsApi.ts?raw';

const showToast = vi.fn();
let currentUser: { id: number; role_name: string; name: string } = { id: 1, role_name: 'administrator', name: 'Admin' };

vi.mock('../../dashboard/v2/V2Shell', () => ({
  // The page header's `actions` hold "+ Create Project", so they are rendered too.
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction: async () => true }),
}));

import { AdminProjectManagement } from '../AdminProjectManagement';

/** Deliberately not anybody real: the options must be whatever the API says. */
const OWNERS = [
  { id: 701, name: 'Owner Alpha', email: 'alpha@example.invalid', role: 'administrator' },
  { id: 702, name: 'Owner Beta', email: 'beta@example.invalid', role: 'employee' },
];
const LEADERS = [{ id: 801, name: 'Leader One', email: 'l1@example.invalid', role: 'leader' }];

const project = (overrides: Record<string, unknown> = {}) => ({
  id: 55,
  project_name: 'Existing project',
  description: '',
  status: { id: 1, name: 'Active', color: '#22C55E' },
  owner: OWNERS[0],
  leader: LEADERS[0],
  employees: [],
  deadline: '2099-06-30',
  billing_type: 'free',
  fixed_hours: null,
  organization_id: 1,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
  tasks: [],
  employee_count: 0,
  task_count: 0,
  ...overrides,
});

type Call = { method: string; url: string; body: any };

let calls: Call[];
let owners: typeof OWNERS;
let listed: ReturnType<typeof project>[];
let createReply: { status: number; body: unknown };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const route = async (request: Request): Promise<Response> => {
  const url = new URL(request.url);
  const path = url.pathname;
  const body = request.method === 'GET' ? null : JSON.parse((await request.clone().text()) || 'null');
  calls.push({ method: request.method, url: path + url.search, body });

  if (path.endsWith('/project-management/metadata')) {
    return json({ roles: [], project_statuses: [{ id: 1, project_status: 'Active', color: '#22C55E' }], task_statuses: [] });
  }
  if (path.endsWith('/projects/assignable-owners')) return json(owners);
  if (path.endsWith('/projects/assignable-leaders')) return json(LEADERS);
  if (path.endsWith('/projects/assignable-employees')) return json([]);
  if (path.endsWith('/projects/hours-summary')) return json({ items: [] });
  if (path.endsWith('/members')) return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
  if (path.endsWith('/projects') && request.method === 'GET') {
    return json({ items: listed, pagination: { page: 1, limit: 20, total: listed.length, total_pages: 1 } });
  }
  if (path.endsWith('/projects') && request.method === 'POST') return json(createReply.body, createReply.status);
  if (/\/projects\/\d+$/.test(path) && request.method === 'PATCH') {
    const current = listed.find((item) => path.endsWith(`/${item.id}`))!;
    const owner = owners.find((item) => item.id === body.owner_id) ?? current.owner;
    return json({ ...current, ...body, owner });
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

const byText = (selector: string, text: string) =>
  Array.from(container.querySelectorAll<HTMLElement>(selector)).find((node) => node.textContent?.trim() === text);

const click = async (element: Element | undefined) => {
  expect(element, 'element to click').toBeTruthy();
  await act(async () => { (element as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  await flush();
};

/** Set a controlled field the way a user's input would, so React sees it. */
const setValue = async (element: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement, value: string) => {
  const prototype = Object.getPrototypeOf(element);
  Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value);
  await act(async () => {
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
  });
};

/** A drawer label's text with the required marker ("*") stripped, so a
 * field gaining or losing the asterisk does not break the lookup. */
const labelText = (item: Element) => (item.textContent ?? '').replace(/\s*\*\s*$/, '').trim();

/** The control that follows a drawer label ("Owner", "Leader", "Deadline"). */
const field = <T extends Element>(label: string) => {
  const node = Array.from(container.querySelectorAll('#project-form label')).find((item) => labelText(item) === label);
  expect(node, `label ${label}`).toBeTruthy();
  return node!.nextElementSibling as unknown as T;
};

const ownerSelect = () => container.querySelector<HTMLSelectElement>('#project-owner')!;
const optionLabels = (select: HTMLSelectElement) =>
  Array.from(select.options).filter((option) => option.value !== '').map((option) => option.textContent);

const submit = async () => {
  await act(async () => {
    container.querySelector('#project-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  });
  await flush();
};

const futureDate = () => {
  const date = new Date();
  date.setDate(date.getDate() + 30);
  return date.toISOString().slice(0, 10);
};

const fillRequiredFields = async () => {
  await setValue(field<HTMLInputElement>('Project Name'), 'Owner feature project');
  await setValue(field<HTMLSelectElement>('Leader'), String(LEADERS[0].id));
  await setValue(field<HTMLInputElement>('Deadline'), futureDate());
  // The Organization is required to create a project.
  await setValue(container.querySelector<HTMLSelectElement>('#project-category')!, 'kyle');
  // Free billing, so no hour budget is required.
  await click(byText('span', 'Flexible Time'));
};

const openCreateDrawer = async () => {
  await click(byText('button', '+ Create Project'));
};

const openEditDrawer = async (projectName: string) => {
  const row = Array.from(container.querySelectorAll('tbody tr')).find((item) => item.textContent?.includes(projectName));
  await click(Array.from(row!.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Manage'));
  await click(byText('button', 'Edit'));
};

const requests = (method: string, suffix: RegExp) => calls.filter((call) => call.method === method && suffix.test(call.url));

beforeEach(() => {
  calls = [];
  owners = [...OWNERS];
  listed = [project()];
  createReply = { status: 201, body: project({ id: 99, project_name: 'Owner feature project' }) };
  currentUser = { id: 1, role_name: 'administrator', name: 'Admin' };
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

describe('Owner field — create', () => {
  it('renders Owner before Leader and Deadline, then Status', async () => {
    await renderPage();
    await openCreateDrawer();
    const labels = Array.from(container.querySelectorAll('#project-form label'))
      .map(labelText)
      .filter((text) => ['Project Name', 'Description', 'Owner', 'Leader', 'Deadline', 'Status', 'Project Members'].includes(text || ''));
    expect(labels).toEqual(['Project Name', 'Description', 'Owner', 'Leader', 'Deadline', 'Status', 'Project Members']);
    expect(ownerSelect()).toBeTruthy();
  });

  it('offers exactly the owners the API returned', async () => {
    await renderPage();
    await openCreateDrawer();
    expect(requests('GET', /\/projects\/assignable-owners$/)).toHaveLength(1);
    expect(optionLabels(ownerSelect())).toEqual(['Owner Alpha', 'Owner Beta']);
  });

  it('follows the API when it answers with different people', async () => {
    owners = [
      { id: 11, name: 'Deepak Gunani', email: 'd@example.invalid', role: 'administrator' },
      { id: 12, name: 'Piyush Gunani', email: 'p@example.invalid', role: 'administrator' },
      { id: 13, name: 'Bharat Gunani', email: 'b@example.invalid', role: 'administrator' },
    ];
    await renderPage();
    await openCreateDrawer();
    expect(optionLabels(ownerSelect())).toEqual(['Deepak Gunani', 'Piyush Gunani', 'Bharat Gunani']);
    expect(Array.from(ownerSelect().options).filter((o) => o.value).map((o) => o.value)).toEqual(['11', '12', '13']);
  });

  it('offers nobody when the API lists nobody (no names are built in)', async () => {
    owners = [];
    await renderPage();
    await openCreateDrawer();
    expect(optionLabels(ownerSelect())).toEqual([]);
  });

  it('never names the designated owners in client source', () => {
    for (const source of [pageSource, projectsApiSource]) {
      expect(source).not.toMatch(/deepak|piyush|bharat|gunani/i);
    }
  });

  it('submits the chosen owner id with the leader and deadline', async () => {
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await setValue(ownerSelect(), '702');
    await submit();

    const [created] = requests('POST', /\/projects$/);
    expect(created.body.owner_id).toBe(702);
    expect(created.body.leader_id).toBe(LEADERS[0].id);
    expect(created.body.deadline).toBe(futureDate());
    expect(showToast).toHaveBeenCalledWith('Project created successfully.', 'success');
  });

  it('shows a field error and sends nothing when no owner is chosen', async () => {
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await submit();

    expect(requests('POST', /\/projects$/)).toHaveLength(0);
    expect(container.querySelector('#project-owner-error')?.textContent).toBe('Project owner is required.');
    expect(ownerSelect().getAttribute('aria-invalid')).toBe('true');
    expect(showToast).toHaveBeenCalledWith('Please correct the highlighted fields.', 'error');

    await setValue(ownerSelect(), '701');
    expect(container.querySelector('#project-owner-error')).toBeNull();
  });

  it("shows the backend's own reason when it refuses the owner", async () => {
    createReply = { status: 400, body: { detail: 'Selected owner is not eligible to own projects.' } };
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await setValue(ownerSelect(), '701');
    await submit();

    expect(showToast).toHaveBeenCalledWith('Selected owner is not eligible to own projects.', 'error');
    expect(showToast).not.toHaveBeenCalledWith('Unable to save project. Please try again.', 'error');
  });
});

describe('Owner field — edit and list', () => {
  it('shows the owner in the project table', async () => {
    await renderPage();
    const headers = Array.from(container.querySelectorAll('thead th')).map((th) => th.textContent);
    expect(headers.indexOf('Owner')).toBeGreaterThan(-1);
    expect(headers.indexOf('Owner')).toBeLessThan(headers.indexOf('Leader'));
    expect(container.querySelector('tbody')?.textContent).toContain('Owner Alpha');
  });

  it('preselects the current owner and saves a change as an id', async () => {
    await renderPage();
    await openEditDrawer('Existing project');
    expect(ownerSelect().value).toBe('701');
    expect(ownerSelect().disabled).toBe(false);

    await setValue(ownerSelect(), '702');
    await submit();

    const [patched] = requests('PATCH', /\/projects\/55$/);
    expect(patched.body.owner_id).toBe(702);
    expect(patched.body.leader_id).toBe(LEADERS[0].id);
    expect(patched.body.deadline).toBe('2099-06-30');
    expect(showToast).toHaveBeenCalledWith('Project updated successfully.', 'success');
    const row = Array.from(container.querySelectorAll('tbody tr')).find((item) => item.textContent?.includes('Existing project'));
    expect(row?.textContent).toContain('Owner Beta');
    expect(row?.textContent).not.toContain('Owner Alpha');
  });

  it('saves a project that predates owners without inventing one', async () => {
    listed = [project({ owner: null })];
    await renderPage();
    await openEditDrawer('Existing project');
    expect(ownerSelect().value).toBe('');
    await submit();

    const [patched] = requests('PATCH', /\/projects\/55$/);
    expect(patched.body).not.toHaveProperty('owner_id');
    expect(container.querySelector('#project-owner-error')).toBeNull();
  });

  it('still displays an owner who is no longer eligible', async () => {
    owners = [OWNERS[1]];
    await renderPage();
    await openEditDrawer('Existing project');
    expect(ownerSelect().value).toBe('701');
    expect(optionLabels(ownerSelect())).toEqual(['Owner Beta', 'Owner Alpha']);
  });

  it('locks the owner for a leader, who may not change it', async () => {
    currentUser = { id: 801, role_name: 'leader', name: 'Leader One' };
    await renderPage();
    await openEditDrawer('Existing project');
    expect(ownerSelect().disabled).toBe(true);
    expect(ownerSelect().value).toBe('701');
  });

  it('keeps leader selection working on edit', async () => {
    await renderPage();
    await openEditDrawer('Existing project');
    expect(field<HTMLSelectElement>('Leader').value).toBe(String(LEADERS[0].id));
  });
});
