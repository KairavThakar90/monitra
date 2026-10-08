// @vitest-environment jsdom
/**
 * The Members page's "Add Billable Task" switch.
 *
 * It governs the desktop's second Add button (tasks created with
 * " - Non billable" on the end of their name) and works like Add Task and Login
 * -- a column, a count, a filter, a per-row switch and a bulk action -- with
 * three differences these tests pin:
 *
 * - it is off until granted, so a member row that does not carry the field is
 *   "Excluded", the opposite of the two switches beside it;
 * - changing it sends no email, so there is nothing to confirm and nothing to
 *   say about mail;
 * - it sits after Login, and moving it sends only its own key.
 *
 * The real page is rendered against a real RTK Query store with only `fetch`
 * stubbed, so what is pinned is the request a browser would make.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { UserRead } from '../../../api/auth';

const user = (role_name: string, permissions: Record<string, boolean>, id = 1): UserRead => ({
  id, organization_id: 1, username: role_name, email: `${role_name}@example.invalid`,
  name: `The ${role_name}`, role_name, permissions, is_active: true,
});

let currentUser: UserRead = user('administrator', {});
const showToast = vi.fn();
const confirmAction = vi.fn(async () => true);

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../auth/authContext', () => ({ useAuth: () => ({ currentUser }) }));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction }),
}));

import { AdminMembers } from '../AdminMembers';

const LABEL = 'Add Billable Task';

type Row = Record<string, unknown>;
const row = (id: number, name: string, extra: Row = {}): Row => ({
  id, name, email: `${name.toLowerCase()}@example.invalid`, role: 'employee', status: 'active',
  date_of_joining: '2026-01-01', date_of_birth: '1990-01-01', designation: 'Dev',
  idle_enabled: true, idle_minutes: 5, capture_frequency: 10,
  can_login: true, can_add_tasks: true, ...extra,
});

describe('AdminMembers: Add Billable Task', () => {
  let container: HTMLDivElement;
  let root: Root;
  let directory: Row[];
  let summary: Record<string, unknown>;
  let accessRequests: Record<string, unknown>[];
  let listQueries: URLSearchParams[];
  let refuse: boolean;

  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  const render = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><AdminMembers /></Provider>); });
    // Wait for the rows themselves rather than a fixed number of ticks.
    for (let waited = 0; waited < 4000 && container.querySelectorAll('tbody tr td').length < 5; waited += 25) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 25)); });
    }
    await settle();
  };

  const click = async (element: Element | null | undefined) => {
    expect(element, 'element to click').toBeTruthy();
    await act(async () => { element!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    await settle();
  };

  const headers = () => Array.from(container.querySelectorAll('thead th')).map((th) => th.textContent?.trim() ?? '');
  const rowOf = (name: string) => Array.from(container.querySelectorAll('tbody tr')).find((tr) => tr.textContent?.includes(name))!;
  const billableCell = (name: string) => {
    const index = headers().findIndex((h) => h.startsWith(LABEL));
    return rowOf(name).querySelectorAll('td')[index] as HTMLElement;
  };
  const switchIn = (cell: Element) => cell.querySelector('button[role="switch"]') as HTMLButtonElement | null;
  const select = () => container.querySelector(`select[aria-label="Filter by ${LABEL} access"]`) as HTMLSelectElement | null;
  const toolbar = () => container.querySelector('[role="toolbar"]');
  const barButton = (group: string, name: string) => {
    const label = Array.from(toolbar()!.querySelectorAll('span')).find((el) => el.textContent === `${group}:`)!;
    return Array.from(label.parentElement!.querySelectorAll('button')).find((b) => b.textContent === name) as HTMLButtonElement;
  };
  const box = (label: string) =>
    container.querySelector(`input[type="checkbox"][aria-label="${label}"]`) as HTMLInputElement | null;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    showToast.mockClear();
    confirmAction.mockClear();
    currentUser = user('administrator', { view_employees: true, manage_member_access: true, manage_employees: true });
    directory = [
      row(11, 'Alice', { can_add_nonbillable_tasks: true }),
      row(12, 'Bob'),                                           // an older row: the field is absent
      row(13, 'Cara', { can_add_nonbillable_tasks: false }),
    ];
    summary = { add_task_allowed: 3, login_allowed: 3, active_members: 3, add_nonbillable_task_allowed: 1 };
    accessRequests = [];
    listQueries = [];
    refuse = false;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = request ? request.url : typeof input === 'string' ? input : input.toString();
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      if (method === 'PATCH' && url.includes('/members/access')) {
        const body = JSON.parse(request ? await request.text() : String(init?.body));
        accessRequests.push(body);
        if (refuse) {
          return json({
            updated: [],
            failed: (body.member_ids as number[]).map((id) => ({ id, detail: 'Only an administrator can change an administrator\'s access.' })),
          });
        }
        return json({
          updated: (body.member_ids as number[]).map((id) => ({ ...directory.find((m) => m.id === id)!, ...body, member_ids: undefined })),
          failed: [],
        });
      }
      if (url.includes('/members/access-summary')) return json(summary);
      if (url.includes('/members')) {
        const params = new URL(url, 'http://localhost').searchParams;
        listQueries.push(params);
        let items = directory;
        const wanted = params.get('can_add_nonbillable_tasks');
        if (wanted === 'true') items = items.filter((m) => m.can_add_nonbillable_tasks === true);
        if (wanted === 'false') items = items.filter((m) => m.can_add_nonbillable_tasks !== true);
        return json({ items, page: 1, limit: 20, total: items.length, pages: 1 });
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

  it('adds the column after Login, with the count of members who are allowed', async () => {
    await render();
    const cols = headers();
    expect(cols.findIndex((h) => h.startsWith(LABEL))).toBe(cols.findIndex((h) => h.startsWith('Login')) + 1);
    expect(cols.find((h) => h.startsWith(LABEL))).toBe(`${LABEL}(1)`);
  });

  it('shows no number rather than a made-up zero when the backend does not send one', async () => {
    delete summary.add_nonbillable_task_allowed;
    await render();
    expect(headers().find((h) => h.startsWith(LABEL))).toBe(LABEL);
  });

  it('reads a member as Allowed only when explicitly granted; an absent field is Excluded', async () => {
    await render();
    expect(billableCell('Alice').textContent).toContain('Allowed');
    expect(billableCell('Bob').textContent).toContain('Excluded');
    expect(billableCell('Cara').textContent).toContain('Excluded');
  });

  it('leaves the Add Task and Login columns reading as before for the same rows', async () => {
    await render();
    const cols = headers();
    const cells = rowOf('Bob').querySelectorAll('td');
    expect(cells[cols.findIndex((h) => h.startsWith('Add Task') && !h.startsWith(LABEL))].textContent).toContain('Allowed');
    expect(cells[cols.findIndex((h) => h.startsWith('Login'))].textContent).toContain('Allowed');
  });

  it('allowing sends only that switch, asks nothing and says nothing about email', async () => {
    await render();
    await click(switchIn(billableCell('Bob')));
    expect(accessRequests).toEqual([{ member_ids: [12], can_add_nonbillable_tasks: true }]);
    expect(confirmAction).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith('Bob is now allowed to add billable tasks.', 'success');
    expect(JSON.stringify(showToast.mock.calls)).not.toMatch(/mail/i);
    expect(billableCell('Bob').textContent).toContain('Allowed');
  });

  it('excluding sends false for that switch alone', async () => {
    await render();
    await click(switchIn(billableCell('Alice')));
    expect(accessRequests).toEqual([{ member_ids: [11], can_add_nonbillable_tasks: false }]);
    expect(confirmAction).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith('Alice is now excluded from adding billable tasks.', 'success');
    expect(billableCell('Alice').textContent).toContain('Excluded');
  });

  it('puts the row back and says why when the server refuses', async () => {
    refuse = true;
    await render();
    await click(switchIn(billableCell('Bob')));
    expect(billableCell('Bob').textContent).toContain('Excluded');
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('Only an administrator'), 'error');
  });

  it('keeps the Add Task switch sending only its own key', async () => {
    await render();
    const cols = headers();
    const addTaskCell = rowOf('Bob').querySelectorAll('td')[cols.findIndex((h) => h.startsWith('Add Task') && !h.startsWith(LABEL))];
    await click(switchIn(addTaskCell));
    expect(accessRequests).toEqual([{ member_ids: [12], can_add_tasks: false }]);
  });

  it('shows the state but no switch to someone who cannot manage member access', async () => {
    currentUser = user('manager', { view_employees: true });
    await render();
    expect(switchIn(billableCell('Alice'))).toBeNull();
    expect(billableCell('Alice').textContent).toContain('Allowed');
    expect(select()).not.toBeNull();                                // reading and filtering stay available
  });

  it('offers All / Allowed / Not allowed and sends nothing until one is chosen', async () => {
    await render();
    expect(Array.from(select()!.options).map((o) => o.textContent)).toEqual(['All', 'Allowed', 'Not allowed']);
    expect(listQueries.every((q) => !q.has('can_add_nonbillable_tasks'))).toBe(true);
  });

  it('Allowed asks for =true and lists only those granted; Not allowed asks for =false', async () => {
    await render();
    const choose = async (value: string) => {
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(select()!, value);
        select()!.dispatchEvent(new Event('change', { bubbles: true }));
      });
      await settle();
    };
    await choose('allowed');
    expect(listQueries[listQueries.length - 1].get('can_add_nonbillable_tasks')).toBe('true');
    expect(Array.from(container.querySelectorAll('tbody tr')).map((tr) => tr.textContent?.includes('Alice'))).toEqual([true]);
    await choose('not_allowed');
    expect(listQueries[listQueries.length - 1].get('can_add_nonbillable_tasks')).toBe('false');
    expect(Array.from(container.querySelectorAll('tbody tr')).length).toBe(2);
  });

  it('has its own Allow / Exclude group in the bulk bar and sends only its key', async () => {
    await render();
    await click(box('Select Bob'));
    await click(box('Select Cara'));
    expect(toolbar()!.textContent).toContain(`${LABEL}:`);
    await click(barButton(LABEL, 'Allow'));
    expect(accessRequests).toEqual([{ member_ids: [12, 13], can_add_nonbillable_tasks: true }]);
  });

  it('disables Exclude when nobody selected is granted, and Allow when everybody is', async () => {
    await render();
    await click(box('Select Bob'));                                   // absent = not granted
    expect(barButton(LABEL, 'Exclude').disabled).toBe(true);
    expect(barButton(LABEL, 'Allow').disabled).toBe(false);
    await click(box('Select Bob'));
    await click(box('Select Alice'));                                 // granted
    expect(barButton(LABEL, 'Allow').disabled).toBe(true);
    expect(barButton(LABEL, 'Exclude').disabled).toBe(false);
  });
});
