// @vitest-environment jsdom
/**
 * Deleting a member on the Members page deletes them -- it does not deactivate.
 *
 * The real screen is rendered against a real RTK Query store with only `fetch`
 * stubbed, so what is pinned is the request a browser would make and what the
 * administrator sees afterwards:
 *
 * - Delete sends `DELETE /members/{id}` and nothing else: no PATCH to "inactive".
 * - The row leaves the list, the count drops, and the success toast shows.
 * - The confirmation says the deletion is permanent and takes the member's
 *   tracked time with it, and points at Inactive as the way to keep history.
 * - Cancelling the confirmation sends nothing.
 * - A refusal (a project they lead, a running timer) keeps the row and shows
 *   the server's reason, not a generic failure.
 * - Your own row's Delete is disabled.
 * - A deleted member does not stay selected for a later bulk change.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { UserRead } from '../../../api/auth';

const ME = 1;

const admin = (): UserRead => ({
  id: ME,
  organization_id: 1,
  username: 'admin',
  email: 'admin@example.invalid',
  name: 'The admin',
  role_name: 'administrator',
  permissions: { view_employees: true, manage_employees: true, manage_member_access: true },
  is_active: true,
});

const currentUser: UserRead = admin();
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

const row = (id: number, name: string) => ({
  id, name, email: `${name.toLowerCase()}@example.invalid`, role: 'employee', status: 'active',
  date_of_joining: '2026-01-01', date_of_birth: '1990-01-01', designation: 'Dev',
  idle_enabled: true, idle_minutes: 5, capture_frequency: 10, can_login: true, can_add_tasks: true,
});

describe('AdminMembers delete', () => {
  let container: HTMLDivElement;
  let root: Root;
  let directory: ReturnType<typeof row>[];
  let requests: { method: string; url: string }[];
  // When set, DELETE is refused with this reason instead of removing the member.
  let refuseWith: string | null;

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

  const rowOf = (name: string) =>
    Array.from(container.querySelectorAll('tbody tr')).find((tr) => tr.textContent?.includes(name)) ?? null;
  const deleteButton = (name: string) =>
    Array.from(rowOf(name)!.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Delete') as HTMLButtonElement;
  const sent = (method: string) => requests.filter((r) => r.method === method);

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    showToast.mockClear();
    confirmAction.mockClear();
    confirmAction.mockImplementation(async () => true);
    refuseWith = null;
    requests = [];
    directory = [row(ME, 'Admin1'), row(12, 'Member12'), row(13, 'Member13')];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = request ? request.url : typeof input === 'string' ? input : input.toString();
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      requests.push({ method, url });
      const target = /\/members\/(\d+)$/.exec(url.split('?')[0]);
      if (method === 'DELETE' && target) {
        if (refuseWith) return json({ detail: refuseWith }, 409);
        directory = directory.filter((m) => m.id !== Number(target[1]));
        return new Response(null, { status: 204 });
      }
      if (url.includes('/members') && method === 'GET') {
        return json({ items: directory, page: 1, limit: 20, total: directory.length, pages: 1 });
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

  it('deletes the member: one DELETE, the row leaves, the toast says so', async () => {
    await render();
    expect(rowOf('Member12')).not.toBeNull();

    await click(deleteButton('Member12'));

    const deletes = sent('DELETE');
    expect(deletes).toHaveLength(1);
    expect(deletes[0].url).toMatch(/\/members\/12$/);
    expect(rowOf('Member12')).toBeNull();
    expect(rowOf('Member13')).not.toBeNull();
    expect(rowOf('Admin1')).not.toBeNull();
    expect(showToast).toHaveBeenCalledWith('Member deleted successfully.', 'success');
  });

  it('never turns the member Inactive instead', async () => {
    await render();
    await click(deleteButton('Member12'));

    expect(sent('PATCH')).toHaveLength(0);
    expect(sent('PUT')).toHaveLength(0);
    // And the list the server returns afterwards has no such member in any status.
    expect(directory.map((m) => m.id)).toEqual([ME, 13]);
  });

  it('asks for confirmation that says it is permanent and points at Inactive', async () => {
    await render();
    await click(deleteButton('Member12'));

    expect(confirmAction).toHaveBeenCalledTimes(1);
    const [title, message] = confirmAction.mock.calls[0];
    expect(title).toBe('Delete member?');
    expect(message).toContain('permanently deleted');
    expect(message).toContain('tracked time');
    expect(message).toContain('cannot be undone');
    expect(message).toContain('Inactive');
  });

  it('sends nothing when the confirmation is cancelled', async () => {
    confirmAction.mockImplementation(async () => false);
    await render();
    await click(deleteButton('Member12'));

    expect(sent('DELETE')).toHaveLength(0);
    expect(rowOf('Member12')).not.toBeNull();
    expect(showToast).not.toHaveBeenCalled();
  });

  it("keeps the row and shows the server's reason when the delete is refused", async () => {
    refuseWith = 'Member12 owns or leads Alpha. Assign someone else before deleting them.';
    await render();
    await click(deleteButton('Member12'));

    expect(sent('DELETE')).toHaveLength(1);
    expect(rowOf('Member12')).not.toBeNull();
    expect(showToast).toHaveBeenCalledWith(refuseWith, 'error');
    expect(showToast).not.toHaveBeenCalledWith('Member deleted successfully.', 'success');
  });

  it('falls back to a generic message when the server gave no reason', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      if (method === 'DELETE') return new Response('boom', { status: 500 });
      return json({ items: directory, page: 1, limit: 20, total: directory.length, pages: 1 });
    }));
    await render();
    await click(deleteButton('Member12'));

    expect(rowOf('Member12')).not.toBeNull();
    expect(showToast).toHaveBeenCalledWith(expect.any(String), 'error');
  });

  it("disables Delete on your own row and leaves everyone else's enabled", async () => {
    await render();

    expect(deleteButton('Admin1').disabled).toBe(true);
    expect(deleteButton('Admin1').title).toBe('You cannot delete your own account');
    expect(deleteButton('Member12').disabled).toBe(false);

    await click(deleteButton('Admin1'));
    expect(sent('DELETE')).toHaveLength(0);
    expect(confirmAction).not.toHaveBeenCalled();
  });

  it('drops a deleted member from the selection', async () => {
    await render();
    const box = container.querySelector('input[type="checkbox"][aria-label="Select Member12"]') as HTMLInputElement;
    await click(box);
    expect(container.querySelector('[role="toolbar"]')).not.toBeNull();

    await click(deleteButton('Member12'));

    expect(rowOf('Member12')).toBeNull();
    expect(container.querySelector('[role="toolbar"]')).toBeNull();
  });
});
