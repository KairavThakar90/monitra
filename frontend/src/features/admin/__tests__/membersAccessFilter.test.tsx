// @vitest-environment jsdom
/**
 * The Members page's Add Task and Login filters.
 *
 * The real screen is rendered against a real RTK Query store with only `fetch`
 * stubbed -- and the stub plays the server: it filters the directory by the
 * `can_login` / `can_add_tasks` query parameters and applies access changes --
 * so what is pinned is the request a browser would make and what the person
 * sees afterwards:
 *
 * - Each filter offers All / Allowed / Not allowed, and sends nothing until
 *   one is chosen.
 * - Allowed asks for `=true`, Not allowed for `=false`, All for neither; the
 *   two combine, and a change returns to page 1.
 * - "Select all" walks the filtered list, never the whole directory.
 * - A member whose switch moves leaves a list filtered by it, and the list is
 *   read again; a change the server refuses leaves the row where it was.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { UserRead } from '../../../api/auth';

const currentUser: UserRead = {
  id: 1,
  organization_id: 1,
  username: 'admin',
  email: 'admin@example.invalid',
  name: 'The admin',
  role_name: 'administrator',
  permissions: { view_employees: true, manage_employees: true, manage_member_access: true },
  is_active: true,
};

const showToast = vi.fn();
const confirmAction = vi.fn(async (_title: string, _message: string) => true);

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => (
    <>
      {actions}
      {children}
    </>
  ),
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction }),
}));

import { AdminMembers } from '../AdminMembers';

type Row = ReturnType<typeof row>;
const row = (id: number, name: string, can_login: boolean, can_add_tasks: boolean) => ({
  id, name, email: `${name.toLowerCase()}@example.invalid`, role: 'employee', status: 'active',
  date_of_joining: '2026-01-01', date_of_birth: '1990-01-01', designation: 'Dev',
  idle_enabled: true, idle_minutes: 5, capture_frequency: 10, can_login, can_add_tasks,
});

describe('AdminMembers Add Task / Login filters', () => {
  let container: HTMLDivElement;
  let root: Root;
  let directory: Row[];
  let listQueries: URLSearchParams[];
  let accessResponse: ((body: Record<string, unknown>) => unknown) | null;

  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    }
  };

  const render = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(
        <Provider store={store}>
          <AdminMembers />
        </Provider>,
      );
    });
    await settle();
  };

  const click = async (element: Element) => {
    await act(async () => {
      element.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await settle();
  };

  const pick = async (label: 'Add Task' | 'Login', value: string) => {
    const select = container.querySelector(`select[aria-label="Filter by ${label} access"]`) as HTMLSelectElement;
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(select, value);
    await act(async () => {
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await settle();
  };

  const select = (label: 'Add Task' | 'Login') =>
    container.querySelector(`select[aria-label="Filter by ${label} access"]`) as HTMLSelectElement | null;
  // The member's name cell (the email under it is not `font-bold`).
  const names = () =>
    Array.from(container.querySelectorAll('tbody tr')).map((tr) => tr.querySelector('.font-bold.truncate')?.textContent ?? '');
  const rowOf = (name: string) =>
    Array.from(container.querySelectorAll('tbody tr')).find((tr) => tr.textContent?.includes(name)) ?? null;
  const lastList = () => listQueries[listQueries.length - 1];
  const listsWithLimit = (limit: string) => listQueries.filter((q) => q.get('limit') === limit);

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    showToast.mockClear();
    confirmAction.mockClear();
    listQueries = [];
    accessResponse = null;
    directory = [
      row(11, 'Alice', true, true),
      row(12, 'Bob', false, true),
      row(13, 'Cara', true, false),
      row(14, 'Dan', false, false),
    ];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), 'http://localhost');
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      if (method === 'PATCH' && url.pathname.endsWith('/members/access')) {
        const body = JSON.parse(request ? await request.text() : String(init?.body));
        if (accessResponse) return json(accessResponse(body));
        const updated = directory.filter((m) => (body.member_ids as number[]).includes(m.id)).map((m) => {
          Object.assign(m, { ...(body.can_login !== undefined && { can_login: body.can_login }), ...(body.can_add_tasks !== undefined && { can_add_tasks: body.can_add_tasks }) });
          return { ...m };
        });
        return json({ updated, failed: [] });
      }
      if (url.pathname.endsWith('/members') && method === 'GET') {
        listQueries.push(url.searchParams);
        const wants = (param: string, value: boolean) => {
          const asked = url.searchParams.get(param);
          return asked === null || (asked === 'true') === value;
        };
        const items = directory.filter((m) => wants('can_login', m.can_login) && wants('can_add_tasks', m.can_add_tasks));
        const limit = Number(url.searchParams.get('limit') ?? 20);
        return json({ items: items.slice(0, limit), page: 1, limit, total: items.length, pages: 1 });
      }
      if (url.pathname.includes('metadata')) return json({ roles: [] });
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

  it('offers All / Allowed / Not allowed for Add Task and Login', async () => {
    await render();
    for (const label of ['Add Task', 'Login'] as const) {
      expect(select(label)).not.toBeNull();
      expect(Array.from(select(label)!.options).map((o) => o.textContent)).toEqual(['All', 'Allowed', 'Not allowed']);
      expect(select(label)!.value).toBe('All');
    }
  });

  it('sends no access filter until one is chosen, and lists everyone', async () => {
    await render();
    expect(lastList().has('can_login')).toBe(false);
    expect(lastList().has('can_add_tasks')).toBe(false);
    expect(names()).toEqual(['Alice', 'Bob', 'Cara', 'Dan']);
  });

  it('Login: Allowed asks for can_login=true and lists only those allowed to log in', async () => {
    await render();
    await pick('Login', 'allowed');

    expect(lastList().get('can_login')).toBe('true');
    expect(lastList().has('can_add_tasks')).toBe(false);
    expect(lastList().get('page')).toBe('1');
    expect(names()).toEqual(['Alice', 'Cara']);
  });

  it('Login: Not allowed asks for can_login=false, and All goes back to everyone', async () => {
    await render();
    await pick('Login', 'not_allowed');
    expect(lastList().get('can_login')).toBe('false');
    expect(names()).toEqual(['Bob', 'Dan']);

    // Back to the unfiltered view the page opened with: that list is still
    // cached, so it is shown at once and no request is needed.
    await pick('Login', 'All');
    expect(select('Login')!.value).toBe('All');
    expect(names()).toEqual(['Alice', 'Bob', 'Cara', 'Dan']);
    expect(listQueries.some((q) => q.has('can_login') && q.get('can_login') !== 'false')).toBe(false);
  });

  it('Add Task: filters by who may add tasks', async () => {
    await render();
    await pick('Add Task', 'allowed');
    expect(lastList().get('can_add_tasks')).toBe('true');
    expect(names()).toEqual(['Alice', 'Bob']);

    await pick('Add Task', 'not_allowed');
    expect(lastList().get('can_add_tasks')).toBe('false');
    expect(names()).toEqual(['Cara', 'Dan']);
  });

  it('the two filters combine', async () => {
    await render();
    await pick('Add Task', 'not_allowed');
    await pick('Login', 'allowed');

    expect(lastList().get('can_add_tasks')).toBe('false');
    expect(lastList().get('can_login')).toBe('true');
    expect(names()).toEqual(['Cara']);
  });

  it('Select all walks the filtered list, not the whole directory', async () => {
    await render();
    await pick('Login', 'not_allowed');

    const all = container.querySelector('input[aria-label="Select all members"]') as HTMLInputElement;
    await click(all);

    const walks = listsWithLimit('100');
    expect(walks.length).toBeGreaterThan(0);
    expect(walks.every((q) => q.get('can_login') === 'false')).toBe(true);
    expect(container.querySelector('[role="toolbar"]')?.textContent).toContain('2 selected');
  });

  it('a member whose switch moves leaves a list filtered by it, and the list is read again', async () => {
    await render();
    await pick('Login', 'allowed');
    expect(names()).toEqual(['Alice', 'Cara']);
    const readsBefore = listQueries.length;

    const toggle = rowOf('Alice')!.querySelector('button[aria-label="Exclude this member from logging in"]')!;
    await click(toggle);

    expect(confirmAction).toHaveBeenCalledTimes(1);
    expect(rowOf('Alice')).toBeNull();
    expect(rowOf('Cara')).not.toBeNull();
    // The rows after the removed one moved up, so the list is read again from the server.
    expect(listQueries.length).toBeGreaterThan(readsBefore);
    expect(lastList().get('can_login')).toBe('true');
  });

  it('a member whose switch moves stays put when no filter is on, and nothing is re-read', async () => {
    await render();
    const readsBefore = listQueries.length;

    await click(rowOf('Alice')!.querySelector('button[aria-label="Exclude this member from adding tasks"]')!);

    expect(rowOf('Alice')).not.toBeNull();
    expect(rowOf('Alice')!.textContent).toContain('Excluded');
    expect(listQueries.length).toBe(readsBefore);
  });

  it('a change the server refuses leaves the row in the filtered list', async () => {
    accessResponse = (body) => ({ updated: [], failed: [{ id: (body.member_ids as number[])[0], detail: 'Not allowed.' }] });
    await render();
    await pick('Login', 'allowed');
    const readsBefore = listQueries.length;

    await click(rowOf('Alice')!.querySelector('button[aria-label="Exclude this member from logging in"]')!);

    expect(rowOf('Alice')).not.toBeNull();
    expect(names()).toEqual(['Alice', 'Cara']);
    expect(showToast).toHaveBeenCalledWith('Not allowed.', 'error');
    expect(listQueries.length).toBe(readsBefore);
  });
});
