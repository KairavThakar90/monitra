// @vitest-environment jsdom
/**
 * The live headcounts beside the Members page's Add Task and Login columns
 * ("ADD TASK (20)   LOGIN (15)").
 *
 * The real screen is rendered against a real RTK Query store with only `fetch`
 * stubbed, so what is pinned is what a browser would request and what the
 * administrator then sees:
 *
 * - The numbers come from `GET /members/access-summary`, not from the page of
 *   rows on screen.
 * - Changing a switch re-reads them from the server straight away.
 * - A change somebody else made turns up on the next poll.
 * - Searching or filtering the table does not move them.
 * - If the summary cannot be read, no number is shown (never a made-up zero).
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { UserRead } from '../../../api/auth';

const admin: UserRead = {
  id: 1,
  organization_id: 1,
  username: 'admin',
  email: 'admin@example.invalid',
  name: 'The admin',
  role_name: 'administrator',
  permissions: { view_employees: true, manage_member_access: true },
  is_active: true,
};
const showToast = vi.fn();

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => (
    <>
      {actions}
      {children}
    </>
  ),
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser: admin }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction: async () => true }),
}));

import { AdminMembers } from '../AdminMembers';

const row = (id: number, extra: Record<string, unknown> = {}) => ({
  id, name: `Member${id}`, email: `member${id}@example.invalid`, role: 'employee', status: 'active',
  date_of_joining: '2026-01-01', date_of_birth: '1990-01-01', designation: 'Dev',
  idle_enabled: true, idle_minutes: 5, capture_frequency: 10,
  can_login: true, can_add_tasks: true, ...extra,
});

describe('AdminMembers access headcounts', () => {
  let container: HTMLDivElement;
  let root: Root;
  // What the server currently holds; a test changes it to model another admin.
  let server: { add_task_allowed: number; login_allowed: number; active_members: number };
  let summaryStatus: number;
  let summaryRequests: number;
  let listUrls: string[];

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

  const header = (title: string) =>
    Array.from(container.querySelectorAll('thead th')).find((th) => th.textContent?.startsWith(title));
  const headerText = (title: string) => header(title)?.textContent?.replace(/\s+/g, ' ').trim();

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    showToast.mockClear();
    server = { add_task_allowed: 20, login_allowed: 15, active_members: 25 };
    summaryStatus = 200;
    summaryRequests = 0;
    listUrls = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = request ? request.url : typeof input === 'string' ? input : input.toString();
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      if (url.includes('/members/access-summary')) {
        summaryRequests += 1;
        return summaryStatus === 200 ? json(server) : json({ detail: 'boom' }, summaryStatus);
      }
      if (method === 'PATCH' && url.includes('/members/access')) {
        const body = JSON.parse(request ? await request.text() : String(init?.body));
        // The server applies the change and its counts move with it.
        if (body.can_add_tasks === false) server.add_task_allowed -= body.member_ids.length;
        if (body.can_login === false) server.login_allowed -= body.member_ids.length;
        return json({
          updated: (body.member_ids as number[]).map((id) => row(id, body)),
          failed: [],
        });
      }
      if (url.includes('/members')) {
        listUrls.push(url);
        return json({ items: [row(11), row(12)], page: 1, limit: 10, total: 2, pages: 1 });
      }
      if (url.includes('metadata')) return json({ roles: [] });
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
    vi.useRealTimers();
  });

  it('shows the real headcounts beside the two columns', async () => {
    await render();

    expect(headerText('Add Task')).toBe('Add Task(20)');
    expect(headerText('Login')).toBe('Login(15)');
    // The headcount is not the size of the page of rows on screen (two rows).
    expect(container.querySelectorAll('tbody tr').length).toBe(2);
  });

  it('explains the number on hover', async () => {
    await render();

    expect(header('Add Task')!.querySelector('span')!.getAttribute('title')).toBe('20 active members are allowed to add tasks');
  });

  it('says "member is" for exactly one', async () => {
    server.login_allowed = 1;
    await render();

    expect(header('Login')!.querySelector('span')!.getAttribute('title')).toBe('1 active member is allowed to log in');
  });

  it('re-reads the numbers from the server as soon as a switch is changed', async () => {
    await render();
    const before = summaryRequests;

    const switches = Array.from(container.querySelectorAll('button[role="switch"]')) as HTMLButtonElement[];
    // Row 1: Add Task, then Login.
    await act(async () => {
      switches[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await settle();

    expect(summaryRequests).toBe(before + 1);
    expect(headerText('Add Task')).toBe('Add Task(19)');
    expect(headerText('Login')).toBe('Login(15)');

    await act(async () => {
      switches[1].dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await settle();
    expect(headerText('Login')).toBe('Login(14)');
  });

  it('picks up a change another administrator made on the next poll', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await render();
    expect(headerText('Login')).toBe('Login(15)');

    server.login_allowed = 12; // someone else excluded three people
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_100);
    });
    await settle();

    expect(headerText('Login')).toBe('Login(12)');
  });

  it('does not move when the table is searched or filtered', async () => {
    await render();
    const before = summaryRequests;

    const search = container.querySelector('input[placeholder^="Search"]') as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    await act(async () => {
      setter.call(search, 'zed');
      search.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 600));
    });
    await settle();

    // The list was asked again, narrowed; the headcount query was not touched.
    expect(listUrls.some((url) => url.includes('search=zed'))).toBe(true);
    expect(summaryRequests).toBe(before);
    expect(headerText('Add Task')).toBe('Add Task(20)');
  });

  it('shows no number at all when the summary cannot be read', async () => {
    summaryStatus = 500;
    await render();

    expect(headerText('Add Task')).toBe('Add Task');
    expect(headerText('Login')).toBe('Login');
    expect(container.textContent).not.toContain('(0)');
  });

  it('shows a real zero when nobody is allowed', async () => {
    server = { add_task_allowed: 0, login_allowed: 0, active_members: 5 };
    await render();

    expect(headerText('Add Task')).toBe('Add Task(0)');
    expect(headerText('Login')).toBe('Login(0)');
  });
});
