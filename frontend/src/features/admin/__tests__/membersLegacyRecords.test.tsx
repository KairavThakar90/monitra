// @vitest-environment jsdom
/**
 * Existing records that hold something other than a name are displayed safely.
 *
 * Before the backend applied the NAME rule to members, a name or a designation could be anything the
 * request (or an identity provider) carried: `{"first":"Ravi"}`, `<img src=x onerror=...>`. Those rows
 * are still in the database -- they are never rewritten -- so the Members page has to show them
 * correctly: a JSON document is not shown as somebody's name, markup is shown as the literal text it is
 * and is never interpreted, and a normal row beside them is untouched. The real page is rendered
 * against a real RTK Query store with only `fetch` stubbed.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { UserRead } from '../../../api/auth';

const currentUser: UserRead = {
  id: 1, organization_id: 1, username: 'admin', email: 'admin@example.invalid', name: 'Admin',
  role_name: 'administrator',
  permissions: { view_employees: true, manage_member_access: true, manage_employees: true },
  is_active: true,
};

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../auth/authContext', () => ({ useAuth: () => ({ currentUser }) }));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminMembers } from '../AdminMembers';

const row = (id: number, name: unknown, designation: unknown) => ({
  id, name, email: `m${id}@example.invalid`, role: 'employee', status: 'active',
  date_of_joining: '2026-01-01', date_of_birth: '1990-01-01', designation,
  idle_enabled: true, idle_minutes: 5, capture_frequency: 10, can_login: true, can_add_tasks: true,
});

describe('Members page: stored records that are not names', () => {
  let container: HTMLDivElement;
  let root: Root;

  const json = (body: unknown) =>
    new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const items = [
      row(11, 'Alice Rao', 'Developer'),
      row(12, '{"first": "Ravi", "last": "Acharya"}', '{"title": "Engineer", "level": 3}'),
      row(13, '<img src=x onerror=alert(1)>Mallory', '<script>alert(1)</script>'),
      row(14, { first: 'Object', last: 'Name' }, ['Not', 'text']),
      { ...row(15, 'Mail Holder', 'Dev'), email: '{"mail": "x@y.z"}' },
    ];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes('/members/access-summary')) {
        return json({ add_task_allowed: 4, login_allowed: 4, active_members: 5, add_nonbillable_task_allowed: 0 });
      }
      if (url.includes('/members')) return json({ items, page: 1, limit: 20, total: items.length, pages: 1 });
      if (url.includes('metadata')) return json({ roles: [] });
      return json({});
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><AdminMembers /></Provider>); });
    for (let waited = 0; waited < 4000 && container.querySelectorAll('tbody tr td').length < 5; waited += 25) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 25)); });
    }
    await settle();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const rows = () => Array.from(container.querySelectorAll('tbody tr'));

  it('shows a normal row exactly as before', () => {
    const alice = rows().find((tr) => tr.textContent?.includes('Alice Rao'))!;
    expect(alice.textContent).toContain('Developer');
  });

  it('never shows a JSON document or a non-text value as a name or designation', () => {
    const text = container.textContent ?? '';
    expect(text).not.toContain('"first"');
    expect(text).not.toContain('"title"');
    expect(text).not.toContain('"mail"');                       // an email column that holds a JSON document
    expect(text).not.toContain('[object Object]');
    expect(text).not.toContain('Not,text');
  });

  it('shows markup as the literal text it is and never builds an element from it', () => {
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.textContent).toContain('<script>alert(1)</script>');   // inert text, not markup
  });

  it('keeps every record listed, so the administrator can still find and correct it', () => {
    expect(rows()).toHaveLength(5);
  });
});
