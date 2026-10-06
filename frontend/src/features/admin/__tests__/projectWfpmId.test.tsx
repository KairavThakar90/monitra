// @vitest-environment jsdom
/**
 * The WFPM ID column on the Project Management page.
 *
 * Rendered against a real RTK Query store with only `fetch` stubbed, so what
 * this pins is what the browser shows for what the API sends:
 *
 * - a "WFPM ID" column sits directly after Project;
 * - a project WFPM created shows its id, and one it did not shows a dash, so
 *   the two can be told apart at a glance;
 * - an older cached/API row with no such field reads as "not a WFPM project"
 *   instead of crashing or printing "undefined";
 * - the Columns dropdown can hide and restore it like any other column.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser: { id: 1, role_name: 'administrator', name: 'Admin', permissions: {} } }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
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
  category: null,
  wfpm_project_id: null,
  fixed_hours: null,
  organization_id: 1,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
  tasks: [],
  employee_count: 0,
  task_count: 0,
  ...overrides,
});

let listed: Record<string, unknown>[];
let container: HTMLDivElement;
let root: Root;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const route = async (request: Request): Promise<Response> => {
  const path = new URL(request.url).pathname;
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
  return json({ detail: 'Not found' }, 404);
};

const flush = async () => {
  for (let i = 0; i < 5; i += 1) {
    await act(async () => { await new Promise((resolveTick) => setTimeout(resolveTick, 0)); });
  }
};

const renderPage = async () => {
  const store = configureStore({
    reducer: { [baseApi.reducerPath]: baseApi.reducer },
    middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
  });
  await act(async () => {
    root.render(<Provider store={store}><AdminProjectManagement /></Provider>);
  });
  await flush();
  // Wait for the rows themselves rather than a fixed number of ticks: under a
  // loaded machine the stubbed fetch can take longer than five of them.
  for (let waited = 0; waited < 4000 && container.querySelectorAll('tbody tr').length < listed.length; waited += 25) {
    await act(async () => { await new Promise((resolveTick) => setTimeout(resolveTick, 25)); });
  }
};

const byText = (selector: string, text: string) =>
  Array.from(container.querySelectorAll<HTMLElement>(selector)).find((node) => node.textContent?.trim() === text);

const click = async (element: Element | undefined) => {
  expect(element, 'element to click').toBeTruthy();
  await act(async () => { (element as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  await flush();
};

const headers = () => Array.from(container.querySelectorAll('thead th')).map((th) => th.textContent?.trim());
const rowFor = (name: string) =>
  Array.from(container.querySelectorAll('tbody tr')).find((tr) => tr.textContent?.includes(name))!;
const wfpmCell = (name: string) => rowFor(name).querySelector('[data-testid="wfpm-id-cell"]')!;

beforeEach(() => {
  listed = [
    project({ id: 1, project_name: 'Made in WFPM', wfpm_project_id: 'wfpm_proj_8f3a' }),
    project({ id: 2, project_name: 'Made in Monitra', wfpm_project_id: null }),
    project({ id: 3, project_name: 'From an older API' }),
  ];
  delete listed[2].wfpm_project_id;
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
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

describe('WFPM ID column', () => {
  it('sits directly after the Project column', async () => {
    await renderPage();
    const cols = headers();
    expect(cols.indexOf('WFPM ID')).toBe(cols.indexOf('Project') + 1);
  });

  it('shows the id of a project WFPM created', async () => {
    await renderPage();
    expect(wfpmCell('Made in WFPM').textContent).toBe('wfpm_proj_8f3a');
    expect(wfpmCell('Made in WFPM').querySelector('[title]')?.getAttribute('title')).toContain('wfpm_proj_8f3a');
  });

  it('shows a dash for a project that is not from WFPM', async () => {
    await renderPage();
    expect(wfpmCell('Made in Monitra').textContent).toBe('—');
  });

  it('reads a row with no such field as not-from-WFPM, never "undefined"', async () => {
    await renderPage();
    expect(wfpmCell('From an older API').textContent).toBe('—');
    expect(container.textContent).not.toContain('undefined');
  });

  it('keeps the cells and headers lined up, one cell per header', async () => {
    await renderPage();
    const columnCount = headers().length;
    for (const name of ['Made in WFPM', 'Made in Monitra', 'From an older API']) {
      expect(rowFor(name).querySelectorAll('td').length).toBe(columnCount);
    }
  });

  it('can be hidden and restored from the Columns dropdown', async () => {
    await renderPage();
    await click(byText('button', 'Columns'));
    const toggle = () => Array.from(container.querySelectorAll('label'))
      .find((label) => label.textContent?.trim() === 'WFPM ID')!.querySelector('input')!;
    await click(toggle());
    expect(headers()).not.toContain('WFPM ID');
    expect(container.querySelector('[data-testid="wfpm-id-cell"]')).toBeNull();
    await click(toggle());
    expect(headers()).toContain('WFPM ID');
    expect(wfpmCell('Made in WFPM').textContent).toBe('wfpm_proj_8f3a');
  });
});
