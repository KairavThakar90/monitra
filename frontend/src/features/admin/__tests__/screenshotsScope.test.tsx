// @vitest-environment jsdom
/**
 * Whose screenshots the Screenshots page shows, and to whom.
 *
 * The page has two views behind an Employees / Own switch. The real screen is
 * rendered against a real RTK Query store with only `fetch` stubbed, so what
 * is pinned is the request a browser would make:
 *
 * - **Employees** sends no `user_id`. The backend answers with everyone in the
 *   caller's own scope -- the organization for Admin and HR, the team for a
 *   leader -- so the page cannot ask for more than the caller may see. The
 *   caller's own row is left out of this view; it belongs to the other one.
 * - **Own** pins the request to the caller's `user_id`.
 * - A leader gets the switch and their team. Someone whose scope does not
 *   reach past themselves gets no switch and only their own captures.
 * - Seeing is not deleting: a leader is never offered Delete.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { UserRead } from '../../../api/auth';
import {
  canDeleteScreenshots,
  canViewAllScreenshots,
  canViewOthersScreenshots,
  canViewTeamScreenshots,
} from '../../auth/roles';

const user = (role_name: string, permissions: Record<string, boolean> = {}, id = 3): UserRead => ({
  id,
  organization_id: 1,
  username: role_name,
  email: `${role_name}@example.invalid`,
  name: `The ${role_name}`,
  role_name,
  permissions,
  is_active: true,
});

let currentUser: UserRead = user('leader');

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ subtitle, actions, children }: {
    subtitle?: string; actions?: React.ReactNode; children: React.ReactNode;
  }) => (
    <>
      <p data-testid="subtitle">{subtitle}</p>
      {actions}
      {children}
    </>
  ),
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminScreenshots } from '../AdminScreenshots';

// ── Who may see, and who may delete ────────────────────────────────────────

describe('screenshot access by role', () => {
  it('lets Admin and HR see everyone, and a leader see their team', () => {
    for (const role of ['administrator', 'org_admin', 'super_admin', 'hr']) {
      expect(canViewAllScreenshots(user(role))).toBe(true);
      expect(canViewOthersScreenshots(user(role))).toBe(true);
    }
    for (const role of ['leader', 'project_leader']) {
      expect(canViewAllScreenshots(user(role))).toBe(false);
      expect(canViewTeamScreenshots(user(role))).toBe(true);
      expect(canViewOthersScreenshots(user(role))).toBe(true);
    }
  });

  it('shows nobody else their colleagues’ screenshots', () => {
    for (const role of ['employee', 'manager', 'client', '']) {
      expect(canViewOthersScreenshots(user(role))).toBe(false);
    }
    expect(canViewOthersScreenshots(null)).toBe(false);
  });

  it('never offers a leader Delete, because they do not hold the permission', () => {
    // A leader's issued permissions, as `app/core/permissions.py` grants them:
    // they can see people, and `screenshots:delete` is not among them.
    const leader = user('leader', { view_employees: true, 'time_entries:view_all': true });
    expect(canViewOthersScreenshots(leader)).toBe(true);
    expect(canDeleteScreenshots(leader)).toBe(false);
    expect(canDeleteScreenshots(user('hr', { 'screenshots:delete': true }))).toBe(true);
  });
});

// ── The screen ─────────────────────────────────────────────────────────────

describe('AdminScreenshots', () => {
  let container: HTMLDivElement;
  let root: Root;
  let dayRequests: string[];

  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

  /** A member who tracked time but whose day has no capture windows to draw. */
  const member = (user_id: number, user_name: string) => ({
    user_id, user_name, days: [], screenshot_count: 2, tracked_seconds: 3600,
  });

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
          <AdminScreenshots />
        </Provider>,
      );
    });
    await settle();
  };

  const tab = (name: string) =>
    Array.from(container.querySelectorAll('[role="tab"]')).find((el) => el.textContent?.trim() === name) as
      | HTMLButtonElement
      | undefined;
  const click = async (element: Element) => {
    await act(async () => {
      element.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await settle();
  };
  const sectionNames = () =>
    Array.from(container.querySelectorAll('section > button[aria-expanded]')).map((el) => el.textContent ?? '');

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    currentUser = user('leader', { view_employees: true });
    dayRequests = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes('/time-entry-screenshots/day')) {
        dayRequests.push(url);
        // What the backend answers a leader asking for their scope: their
        // team, which includes the leader themselves. Pinned to one person it
        // answers with that person alone.
        const pinned = new URL(url, 'http://localhost').searchParams.get('user_id');
        const everyone = [member(11, 'Alice Example'), member(3, 'The leader')];
        const members = pinned ? everyone.filter((row) => String(row.user_id) === pinned) : everyone;
        return json({ success: true, window_minutes: 10, members });
      }
      if (url.includes('/members') || url.includes('/projects')) {
        return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
      }
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

  it('opens a leader on their employees, asking the server for their own scope', async () => {
    await render();

    expect(tab('Employees')?.getAttribute('aria-selected')).toBe('true');
    expect(tab('Own')?.getAttribute('aria-selected')).toBe('false');
    // No `user_id`: who a leader may see is the backend's answer, not the page's.
    expect(dayRequests).toHaveLength(1);
    expect(dayRequests[0]).not.toContain('user_id');

    const names = sectionNames();
    expect(names).toHaveLength(1);
    expect(names[0]).toContain('Alice Example');
    // The leader's own row is not an employee row.
    expect(names.join(' ')).not.toContain('The leader');
    expect(container.querySelector('[data-testid="subtitle"]')!.textContent).toContain('your team');
    expect(container.textContent).toContain('from 1 employee');
  });

  it('shows the leader’s own captures when Own is chosen, pinned to their id', async () => {
    await render();
    await click(tab('Own')!);

    expect(tab('Own')?.getAttribute('aria-selected')).toBe('true');
    const latest = dayRequests[dayRequests.length - 1];
    expect(new URL(latest, 'http://localhost').searchParams.get('user_id')).toBe('3');

    const names = sectionNames();
    expect(names).toHaveLength(1);
    expect(names[0]).toContain('The leader');
    expect(container.querySelector('[data-testid="subtitle"]')!.textContent).toContain('your machine');
    // One person, nothing to narrow: the pickers and the employee count go.
    expect(container.textContent).not.toContain('All members');
    expect(container.textContent).not.toContain('employee');
  });

  it('switches back to the employees without losing them', async () => {
    await render();
    await click(tab('Own')!);
    await click(tab('Employees')!);
    expect(sectionNames()[0]).toContain('Alice Example');
  });

  it('gives an administrator the same switch over the whole organization', async () => {
    currentUser = user('administrator', { view_employees: true, 'screenshots:delete': true }, 1);
    await render();

    expect(tab('Employees')).toBeDefined();
    expect(dayRequests[0]).not.toContain('user_id');
    // Nobody in this answer is the administrator, so both rows are employees.
    expect(sectionNames()).toHaveLength(2);
    expect(container.querySelector('[data-testid="subtitle"]')!.textContent).toContain('employees');
  });

  it('gives someone who can only see themselves no switch and only their own', async () => {
    currentUser = user('manager', { view_employees: true });
    await render();

    expect(container.querySelector('[role="tab"]')).toBeNull();
    expect(new URL(dayRequests[0], 'http://localhost').searchParams.get('user_id')).toBe('3');
    expect(container.textContent).toContain('Showing your own screenshots');
  });
});
