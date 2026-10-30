// @vitest-environment jsdom
/**
 * The Members page's desktop version: a column, and a Version filter.
 *
 * The real screen is rendered against a real RTK Query store with only `fetch` stubbed, so what is
 * pinned is what a person sees and the request the browser makes:
 *
 * - each row shows the version and system its desktop last reported, and a dash when it never did;
 * - the Version filter offers every reported version (newest first, with how many members are on it),
 *   plus "Not reported", and its choice goes to the server -- as `desktop_version`, `none` for the
 *   never-reported -- so it is right across pages and not only for the rows on screen;
 * - choosing "All" sends no filter, and choosing one returns to the first page;
 * - a change that comes back without a version (a bulk access change) does not blank the column;
 * - if the list of versions cannot be loaded the filter still works with what it has.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { UserRead } from '../../../api/auth';

const admin: UserRead = {
  id: 1, organization_id: 1, username: 'admin', email: 'admin@example.invalid', name: 'The admin',
  role_name: 'administrator', permissions: { view_employees: true, manage_member_access: true, manage_employees: true }, is_active: true,
};

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../auth/authContext', () => ({ useAuth: () => ({ currentUser: admin }) }));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminMembers } from '../AdminMembers';

const row = (id: number, name: string, extra: Record<string, unknown> = {}) => ({
  id, name, email: `${name.toLowerCase()}@example.invalid`, role: 'employee', status: 'active',
  date_of_joining: '2026-01-01', date_of_birth: '1990-01-01', designation: 'Engineer',
  idle_enabled: true, idle_minutes: 5, capture_frequency: 10, can_login: true, can_add_tasks: true,
  desktop_version: null, desktop_platform: null, ...extra,
});

const DIRECTORY = [
  row(11, 'Asha', { desktop_version: '1.3.4', desktop_platform: 'win32' }),
  row(12, 'Bela', { desktop_version: '1.3.4', desktop_platform: 'darwin' }),
  row(13, 'Chitra', { desktop_version: '1.2.0', desktop_platform: 'linux' }),
  row(14, 'Farhan'),
];

describe('AdminMembers: desktop version', () => {
  let container: HTMLDivElement;
  let root: Root;
  let listQueries: URLSearchParams[];
  let versionsAnswer: () => Response;
  let accessAnswer: () => unknown;

  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const mount = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><AdminMembers /></Provider>); });
    await settle();
  };
  const rowOf = (name: string) =>
    Array.from(container.querySelectorAll('tbody tr')).find((tr) => tr.textContent?.includes(name)) as HTMLElement;
  const desktopCell = (name: string) => rowOf(name).querySelector('[data-testid="member-desktop"]') as HTMLElement;
  const filter = () => container.querySelector('select[aria-label="Filter by desktop version"]') as HTMLSelectElement;
  const choose = async (value: string) => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(filter(), value);
    await act(async () => { filter().dispatchEvent(new Event('change', { bubbles: true })); });
    await settle();
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    listQueries = [];
    versionsAnswer = () => json({ latest_version: '1.3.4', counts: { '1.2.0': 1, '1.3.4': 2 }, clients: [] });
    accessAnswer = () => ({ updated: [row(11, 'Asha')], failed: [] }); // no desktop_version in the reply
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), 'http://localhost');
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      if (url.pathname === '/api/v1/desktop/client-versions') return versionsAnswer();
      if (method === 'PATCH' && url.pathname.endsWith('/members/access')) return json(accessAnswer());
      if (url.pathname.endsWith('/members') && method === 'GET') {
        listQueries.push(url.searchParams);
        const wanted = url.searchParams.get('desktop_version');
        const items = DIRECTORY.filter((m) => wanted === null ? true : wanted === 'none' ? !m.desktop_version : m.desktop_version === wanted);
        return json({ items, page: 1, limit: 20, total: items.length, pages: 1 });
      }
      if (url.pathname.includes('metadata')) return json({ roles: [] });
      if (url.pathname.endsWith('/access-summary')) return json({ add_task_allowed: 4, login_allowed: 4, active_members: 4 });
      return json({}, 404);
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('shows each member\'s desktop version and system, and a dash for one that never reported', async () => {
    await mount();

    expect(Array.from(container.querySelectorAll('th')).map((th) => th.textContent!.trim())).toContain('Desktop');
    expect(desktopCell('Asha').textContent).toBe('v1.3.4Windows');
    expect(desktopCell('Bela').textContent).toBe('v1.3.4macOS');
    expect(desktopCell('Chitra').textContent).toBe('v1.2.0Linux');
    expect(desktopCell('Farhan').textContent).toBe('—');
  });

  it('offers every reported version, newest first with its member count, then "Not reported"', async () => {
    await mount();

    expect(Array.from(filter().options).map((o) => [o.value, o.textContent])).toEqual([
      ['All', 'All Versions'],
      ['1.3.4', 'v1.3.4 (2)'],
      ['1.2.0', 'v1.2.0 (1)'],
      ['none', 'Not reported'],
    ]);
  });

  it('asks the server for one version, and shows only the members on it', async () => {
    await mount();
    expect(listQueries[listQueries.length - 1].has('desktop_version')).toBe(false);

    await choose('1.3.4');

    expect(listQueries[listQueries.length - 1].get('desktop_version')).toBe('1.3.4');
    expect(listQueries[listQueries.length - 1].get('page')).toBe('1');
    expect(rowOf('Asha')).toBeTruthy();
    expect(rowOf('Bela')).toBeTruthy();
    expect(rowOf('Chitra')).toBeUndefined();
    expect(rowOf('Farhan')).toBeUndefined();
  });

  it('"Not reported" asks for `none`, and "All" drops the filter again', async () => {
    await mount();
    await choose('none');
    expect(listQueries[listQueries.length - 1].get('desktop_version')).toBe('none');
    expect(rowOf('Farhan')).toBeTruthy();
    expect(rowOf('Asha')).toBeUndefined();

    // "All" is the first answer again, which the page already holds, so it asks for nothing new and shows everyone.
    await choose('All');
    expect(listQueries.some((q) => q.has('desktop_version') && q.get('desktop_version') === 'All')).toBe(false);
    expect(rowOf('Asha')).toBeTruthy();
    expect(rowOf('Farhan')).toBeTruthy();
  });

  it('keeps the version on screen when a change comes back without one', async () => {
    await mount();
    expect(desktopCell('Asha').textContent).toBe('v1.3.4Windows');

    // Turn Asha's Add Task switch off: the server's reply is the member row, with no desktop version in it.
    const toggle = rowOf('Asha').querySelector('button[role="switch"]') as HTMLButtonElement;
    await act(async () => { toggle.click(); });
    await settle();

    expect(desktopCell('Asha').textContent).toBe('v1.3.4Windows');
  });

  it('still filters when the list of versions could not be loaded', async () => {
    versionsAnswer = () => json({ detail: 'nope' }, 500);
    await mount();

    expect(Array.from(filter().options).map((o) => o.value)).toEqual(['All', 'none']);
    await choose('none');
    expect(listQueries[listQueries.length - 1].get('desktop_version')).toBe('none');
  });
});
