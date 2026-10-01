// @vitest-environment jsdom
/**
 * The Members page's Role filter offers Client beside the server's roles, and
 * choosing it asks the API for `role=client`.
 *
 * Clients are listed in the directory but are not a role the Add / Edit form
 * can assign, so the server's role list leaves them out and the filter adds
 * the option itself -- once, and never twice if the server ever lists it.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({
    currentUser: { id: 1, organization_id: 1, username: 'a', email: 'a@x.invalid', name: 'A', role_name: 'administrator',
      permissions: { view_employees: true, manage_employees: true, manage_member_access: true }, is_active: true },
  }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminMembers } from '../AdminMembers';

const ROLES = [
  { id: 1, role_type: 'Administrator', value: 'administrator' },
  { id: 2, role_type: 'Leader', value: 'leader' },
  { id: 3, role_type: 'HR', value: 'hr' },
  { id: 4, role_type: 'Employee', value: 'employee' },
];

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

describe('Members: Role filter', () => {
  let container: HTMLDivElement;
  let root: Root;
  let memberQueries: URLSearchParams[];
  let serverRoles: typeof ROLES;

  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const mount = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(<Provider store={store}><AdminMembers /></Provider>);
    });
    await settle();
  };
  const roleSelect = () => {
    const select = Array.from(container.querySelectorAll('select')).find((s) =>
      Array.from(s.options).some((o) => o.value === 'All'),
    );
    return select as HTMLSelectElement;
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    memberQueries = [];
    serverRoles = ROLES;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      if (url.pathname.endsWith('/members')) {
        memberQueries.push(url.searchParams);
        return json({ items: [], page: 1, limit: 20, total: 0, pages: 1 });
      }
      if (url.pathname.includes('metadata')) return json({ roles: serverRoles });
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

  it('lists the server roles and then Client', async () => {
    await mount();
    expect(Array.from(roleSelect().options).map((o) => o.textContent)).toEqual([
      'All Roles', 'Administrator', 'Leader', 'HR', 'Employee', 'Client',
    ]);
  });

  it('does not duplicate Client if the server ever lists it', async () => {
    serverRoles = [...ROLES, { id: 5, role_type: 'Client', value: 'client' }];
    await mount();
    expect(Array.from(roleSelect().options).filter((o) => o.value === 'client')).toHaveLength(1);
  });

  it('asks the API for role=client when Client is chosen', async () => {
    await mount();
    memberQueries = [];
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(roleSelect(), 'client');
    await act(async () => { roleSelect().dispatchEvent(new Event('change', { bubbles: true })); });
    await settle();

    expect(memberQueries.length).toBeGreaterThan(0);
    expect(memberQueries[memberQueries.length - 1].get('role')).toBe('client');
  });
});
