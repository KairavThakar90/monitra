// @vitest-environment jsdom
/**
 * The Members page's multi-select Login / Add Task switches.
 *
 * The real screen is rendered against a real RTK Query store with only `fetch`
 * stubbed, so what is pinned is the request a browser would make and what the
 * person sees afterwards:
 *
 * - Whoever holds `manage_member_access` (administrators and HR) gets
 *   checkboxes and a bulk bar; HR gets them without any edit/delete control.
 * - Anyone else sees neither.
 * - Allow / Exclude sends ONE request naming every selected member, and only
 *   the switch that was pressed.
 * - The header checkbox selects every member of the directory, on every page.
 * - Allow / Exclude is disabled when it would change nothing for the selection.
 * - A member the server refuses keeps its old value and stays selected.
 * - The existing per-row switch still works, for HR as well.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { UserRead } from '../../../api/auth';

const user = (role_name: string, permissions: Record<string, boolean>, id = 1): UserRead => ({
  id,
  organization_id: 1,
  username: role_name,
  email: `${role_name}@example.invalid`,
  name: `The ${role_name}`,
  role_name,
  permissions,
  is_active: true,
});

let currentUser: UserRead = user('hr', {});
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
  useAuth: () => ({ currentUser }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction: async () => true }),
}));

import { AdminMembers } from '../AdminMembers';

const row = (id: number, name: string, extra: Record<string, unknown> = {}) => ({
  id, name, email: `${name.toLowerCase()}@example.invalid`, role: 'employee', status: 'active',
  date_of_joining: '2026-01-01', date_of_birth: '1990-01-01', designation: 'Dev',
  idle_enabled: true, idle_minutes: 5, capture_frequency: 10,
  can_login: true, can_add_tasks: true, ...extra,
});

/**
 * The text of a row's Add Task and Login cells only. The Add Billable Task cell
 * after them reads "Excluded" for everyone who has not been granted it, which is
 * the normal state, so a whole-row text probe cannot tell these two switches apart.
 */
const sharedSwitchText = (tr: Element) =>
  Array.from(tr.querySelectorAll('td')).slice(7, 9).map((cell) => cell.textContent ?? '').join(' ');

describe('AdminMembers bulk access', () => {
  let container: HTMLDivElement;
  let root: Root;
  let accessRequests: Record<string, unknown>[];
  let accessResponse: (body: Record<string, unknown>) => unknown;
  // How many members the directory holds. The screen shows 3 per page here;
  // a request for 100 (what "Select all" makes) is answered from the full set.
  let directorySize: number;

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
  const box = (label: string) =>
    container.querySelector(`input[type="checkbox"][aria-label="${label}"]`) as HTMLInputElement | null;
  const toolbar = () => container.querySelector('[role="toolbar"]');
  const barButton = (group: string, name: string) => {
    const label = Array.from(toolbar()!.querySelectorAll('span')).find((el) => el.textContent === `${group}:`)!;
    return Array.from(label.parentElement!.querySelectorAll('button')).find(
      (b) => b.textContent === name,
    ) as HTMLButtonElement;
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    showToast.mockClear();
    currentUser = user('hr', { view_employees: true, manage_member_access: true });
    accessRequests = [];
    directorySize = 3;
    accessResponse = (body) => ({
      updated: (body.member_ids as number[]).map((id) => row(id, `Member${id}`, body)),
      failed: [],
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = request ? request.url : typeof input === 'string' ? input : input.toString();
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      if (method === 'PATCH' && url.includes('/members/access')) {
        const body = JSON.parse(request ? await request.text() : String(init?.body));
        accessRequests.push(body);
        return json(accessResponse(body));
      }
      if (url.includes('/members')) {
        const params = new URL(url, 'http://localhost').searchParams;
        const limit = Number(params.get('limit') ?? 20);
        const page = Number(params.get('page') ?? 1);
        const perPage = limit >= 100 ? 100 : 3;
        const all = Array.from({ length: directorySize }, (_, i) => row(11 + i, `Member${11 + i}`));
        return json({
          items: all.slice((page - 1) * perPage, page * perPage),
          page, limit, total: directorySize, pages: Math.max(1, Math.ceil(directorySize / perPage)),
        });
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
  });

  it('gives HR checkboxes and a bulk bar, but no way to edit or delete a member', async () => {
    await render();

    expect(box('Select Member11')).not.toBeNull();
    expect(box('Select all members')).not.toBeNull();
    expect(toolbar()).toBeNull();
    expect(container.textContent).not.toContain('Add Member');
    expect(container.textContent).not.toContain('Delete');

    await click(box('Select Member11')!);
    expect(toolbar()!.textContent).toContain('1 selected');
  });

  it('shows no checkboxes to someone without the access permission', async () => {
    currentUser = user('manager', { view_employees: true });
    await render();

    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
    expect(toolbar()).toBeNull();
    expect(container.textContent).toContain('Member11');
  });

  it('sends one request for the selection and flips only those rows', async () => {
    await render();
    await click(box('Select Member11')!);
    await click(box('Select Member13')!);
    expect(toolbar()!.textContent).toContain('2 selected');

    await click(barButton('Login', 'Exclude'));

    expect(accessRequests).toEqual([{ member_ids: [11, 13], can_login: false }]);
    const excluded = Array.from(container.querySelectorAll('tbody tr')).map(
      (tr) => sharedSwitchText(tr).includes('Excluded'),
    );
    expect(excluded).toEqual([true, false, true]);
    // A clean result clears the selection and says how many changed.
    expect(toolbar()).toBeNull();
    expect(showToast).toHaveBeenCalledWith('Updated 2 members.', 'success');
  });

  it('disables the button that would change nothing, and enables it once something is excluded', async () => {
    await render();
    await click(box('Select all members')!);

    // Everyone starts allowed: there is nothing for "Allow" to do.
    expect(barButton('Login', 'Allow').disabled).toBe(true);
    expect(barButton('Add Task', 'Allow').disabled).toBe(true);
    expect(barButton('Login', 'Exclude').disabled).toBe(false);

    await click(barButton('Add Task', 'Exclude'));
    expect(accessRequests).toEqual([{ member_ids: [11, 12, 13], can_add_tasks: false }]);

    // Now everyone is excluded from Add Task: Exclude is the dead one.
    await click(box('Select all members')!);
    expect(barButton('Add Task', 'Exclude').disabled).toBe(true);
    expect(barButton('Add Task', 'Allow').disabled).toBe(false);
    // The other switch is untouched.
    expect(barButton('Login', 'Allow').disabled).toBe(true);

    await click(barButton('Add Task', 'Allow'));
    expect(accessRequests[1]).toEqual({ member_ids: [11, 12, 13], can_add_tasks: true });
  });

  it('leaves Allow enabled while any selected member is still excluded', async () => {
    await render();
    await click(box('Select Member11')!);
    await click(box('Select Member12')!);
    await click(barButton('Login', 'Exclude'));       // 11 and 12 are now excluded

    await click(box('Select Member11')!);              // excluded
    await click(box('Select Member13')!);              // allowed
    expect(barButton('Login', 'Allow').disabled).toBe(false);
    expect(barButton('Login', 'Exclude').disabled).toBe(false);
  });

  it('checks every member of the directory, not just the visible page', async () => {
    directorySize = 5;
    await render();
    expect(container.querySelectorAll('tbody input[type="checkbox"]')).toHaveLength(3);

    await click(box('Select all members')!);

    expect(toolbar()!.textContent).toContain('5 selected');
    expect(box('Select all members')!.checked).toBe(true);
    expect(toolbar()!.textContent).toContain('Every member in this list');

    await click(barButton('Add Task', 'Exclude'));
    expect(accessRequests).toEqual([{ member_ids: [11, 12, 13, 14, 15], can_add_tasks: false }]);
  });

  it('un-checks everything when the header box is pressed again', async () => {
    directorySize = 5;
    await render();
    await click(box('Select all members')!);
    expect(toolbar()!.textContent).toContain('5 selected');

    await click(box('Select all members')!);
    expect(toolbar()).toBeNull();
    expect(box('Select Member11')!.checked).toBe(false);
  });

  it('marks the header box as partly checked when only some members are', async () => {
    await render();
    await click(box('Select Member11')!);
    expect(box('Select all members')!.indeterminate).toBe(true);
    expect(box('Select all members')!.checked).toBe(false);
  });

  it('keeps a refused member unchanged and selected, and says why', async () => {
    accessResponse = () => ({
      updated: [row(11, 'Member11', { can_login: false })],
      failed: [{ id: 12, detail: 'Only an administrator can change an administrator\'s access.' }],
    });
    await render();
    await click(box('Select Member11')!);
    await click(box('Select Member12')!);

    await click(barButton('Login', 'Exclude'));

    const rows = Array.from(container.querySelectorAll('tbody tr'));
    expect(sharedSwitchText(rows[0])).toContain('Excluded');
    expect(sharedSwitchText(rows[1])).not.toContain('Excluded');
    expect(toolbar()!.textContent).toContain('1 selected');
    expect(box('Select Member12')!.checked).toBe(true);
    expect(showToast).toHaveBeenCalledWith(
      expect.stringContaining('1 could not be changed'), 'info',
    );
    expect(showToast.mock.calls.at(-1)![0]).toContain('Only an administrator');
  });

  it('still lets the per-row switch work, through the same endpoint', async () => {
    await render();
    const firstRowSwitches = container.querySelectorAll('tbody tr:first-child button[role="switch"]');
    // Add Task is the first switch, Login the second.
    await click(firstRowSwitches[0]);

    expect(accessRequests).toEqual([{ member_ids: [11], can_add_tasks: false }]);
    expect(showToast).toHaveBeenCalledWith('Member11 is now excluded from adding tasks.', 'success');
  });
});
