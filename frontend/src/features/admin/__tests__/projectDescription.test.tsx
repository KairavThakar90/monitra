// @vitest-environment jsdom
/**
 * The project description on the admin Create / Edit Project drawer is plain text.
 *
 * The real screen is rendered against a real RTK Query store with only `fetch`
 * stubbed, so what is pinned is the request a browser would make and what the
 * administrator sees:
 *
 * - HTML, a script, jQuery, JavaScript and CSS are each refused on the spot, with
 *   a message beside the field, and nothing is sent;
 * - an ordinary sentence -- punctuation, `<`, `&`, an apostrophe, another
 *   script -- is saved exactly as typed (trimmed);
 * - an edit is held to the same rule as a create;
 * - a description already stored with markup is *shown* as the text it is, never
 *   interpreted.
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

const OWNER = { id: 701, name: 'Owner Alpha', email: 'alpha@example.invalid', role: 'administrator' };
const LEADER = { id: 801, name: 'Leader One', email: 'l1@example.invalid', role: 'leader' };

const project = (overrides: Record<string, unknown> = {}) => ({
  id: 55,
  project_name: 'Existing project',
  description: '',
  status: { id: 1, name: 'Active', color: '#22C55E' },
  owner: OWNER,
  leader: LEADER,
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
let listed: ReturnType<typeof project>[];

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
  if (path.endsWith('/projects/assignable-owners')) return json([OWNER]);
  if (path.endsWith('/projects/assignable-leaders')) return json([LEADER]);
  if (path.endsWith('/projects/assignable-employees')) return json([]);
  if (path.endsWith('/projects/hours-summary')) return json({ items: [] });
  if (path.endsWith('/members')) return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
  if (path.endsWith('/projects') && request.method === 'GET') {
    return json({ items: listed, pagination: { page: 1, limit: 20, total: listed.length, total_pages: 1 } });
  }
  if (path.endsWith('/projects') && request.method === 'POST') return json(project({ id: 99, ...body }), 201);
  if (/\/projects\/\d+$/.test(path) && request.method === 'PATCH') {
    const current = listed.find((item) => path.endsWith(`/${item.id}`))!;
    return json({ ...current, ...body });
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

const click = async (element: Element | null | undefined) => {
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

const labelText = (item: Element) => (item.textContent ?? '').replace(/\s*\*\s*$/, '').trim();
const field = <T extends Element>(label: string) => {
  const node = Array.from(container.querySelectorAll('#project-form label')).find((item) => labelText(item) === label);
  expect(node, `label ${label}`).toBeTruthy();
  return node!.nextElementSibling as unknown as T;
};

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

const requests = (method: string, suffix: RegExp) => calls.filter((call) => call.method === method && suffix.test(call.url));
const description = () => field<HTMLTextAreaElement>('Description');
const fieldError = () => container.querySelector('#project-form [role="alert"]')?.textContent ?? '';

/** Everything the form needs but the description, which each test types. */
const fillRequiredFields = async () => {
  await setValue(field<HTMLInputElement>('Project Name'), 'Internal tooling');
  await setValue(container.querySelector<HTMLSelectElement>('#project-owner')!, String(OWNER.id));
  await setValue(field<HTMLSelectElement>('Leader'), String(LEADER.id));
  await setValue(field<HTMLInputElement>('Deadline'), futureDate());
  await setValue(container.querySelector<HTMLSelectElement>('#project-category')!, 'kyle');
  // Flexible Time: no hour budget to fill in.
  const flexible = Array.from(container.querySelectorAll<HTMLInputElement>('input[name="billingType"]')).find((input) => input.value === 'free')!;
  await click(flexible.closest('label'));
};

const openCreateDrawer = async () => {
  await click(byText('button', '+ Create Project'));
};

const openManage = async (projectName: string) => {
  const row = Array.from(container.querySelectorAll('tbody tr')).find((item) => item.textContent?.includes(projectName))!;
  await click(Array.from(row.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Manage'));
};

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  calls = [];
  listed = [project()];
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

const CODE: Array<[string, string]> = [
  ['HTML', '<b>Important</b> project'],
  ['a script tag', '<script>alert(1)</script>'],
  ['an event handler', '<img src=x onerror=alert(1)>'],
  ['a javascript: link', 'javascript:alert(1)'],
  ['jQuery', "$('#btn').click(function(){ alert(1) })"],
  ['JavaScript', "document.write('x')"],
  ['CSS', 'body { color: red; }'],
];

describe('Create Project — the description is plain text', () => {
  it.each(CODE)('refuses %s, says why, and sends nothing', async (_kind, text) => {
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await setValue(description(), text);
    await submit();

    expect(requests('POST', /\/projects$/)).toHaveLength(0);
    expect(fieldError()).toContain('Description:');
    expect(fieldError()).toMatch(/not allowed/i);
    expect(showToast).toHaveBeenCalledWith('Please correct the highlighted fields.', 'error');
  });

  it('saves an ordinary description exactly as typed, trimmed', async () => {
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await setValue(description(), "  Rebuild the site: 5 < 10 pages, O'Brien & Sons.\nPhase 2 follows.  ");
    await submit();

    const [created] = requests('POST', /\/projects$/);
    expect(created, 'a create request').toBeTruthy();
    expect(created.body.description).toBe("Rebuild the site: 5 < 10 pages, O'Brien & Sons.\nPhase 2 follows.");
    expect(showToast).toHaveBeenCalledWith('Project created successfully.', 'success');
  });

  it('saves a description in another script', async () => {
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await setValue(description(), 'ウェブサイトの再構築プロジェクト');
    await submit();

    expect(requests('POST', /\/projects$/)[0].body.description).toBe('ウェブサイトの再構築プロジェクト');
  });

  it('still creates a project with no description at all', async () => {
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await submit();

    const [created] = requests('POST', /\/projects$/);
    expect(created, 'a create request').toBeTruthy();
    expect(created.body.description).toBe('');
  });
});

describe('Edit Project — the same rule', () => {
  it('refuses code typed into an existing project and sends nothing', async () => {
    listed = [project({ description: 'Old text' })];
    await renderPage();
    await openManage('Existing project');
    await click(byText('button', 'Edit'));
    expect(description().value).toBe('Old text');

    await setValue(description(), "$(document).ready(function(){ alert('x') })");
    await submit();

    expect(requests('PATCH', /\/projects\/55$/)).toHaveLength(0);
    expect(fieldError()).toMatch(/not allowed/i);
  });

  it('saves a corrected description', async () => {
    listed = [project({ description: 'Old text' })];
    await renderPage();
    await openManage('Existing project');
    await click(byText('button', 'Edit'));
    await setValue(description(), 'Now with detail');
    await submit();

    const [patch] = requests('PATCH', /\/projects\/55$/);
    expect(patch, 'an update request').toBeTruthy();
    expect(patch.body.description).toBe('Now with detail');
  });
});

describe('Showing a description', () => {
  it('renders a stored description that holds markup as literal text, never as elements', async () => {
    // A row written before this rule existed: it must still be readable, and it
    // must never be interpreted.
    listed = [project({ description: '<img src=x onerror="window.__pwned=1"><b>bold?</b>' })];
    await renderPage();
    await openManage('Existing project');
    await click(byText('button', 'View'));

    expect(container.textContent).toContain('<img src=x onerror="window.__pwned=1"><b>bold?</b>');
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });
});
