// @vitest-environment jsdom
/**
 * The Manual Requests tab's View button.
 *
 * Every request row gets a View button (styled like the Feedback page's), not
 * only the pending ones, and it opens a dialog with the request's details.
 * The real screen is rendered against a real RTK Query store with only `fetch`
 * stubbed.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { MemoryRouter } from 'react-router-dom';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { UserRead } from '../../../api/auth';

const currentUser: UserRead = {
  id: 1, organization_id: 1, username: 'admin', email: 'admin@example.invalid', name: 'Admin',
  role_name: 'administrator',
  permissions: { 'manual_time_entries:approve': true, view_employees: true },
  is_active: true,
};

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock('../../auth/authContext', () => ({ useAuth: () => ({ currentUser }) }));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminTimeTracking } from '../AdminTimeTracking';

const request = (id: number, approval_status: string, extra: Record<string, unknown> = {}) => ({
  id, user_id: 7, project_id: 1, task_id: 2, work_date: '2026-09-30',
  start_time: '2026-09-30T04:30:00Z', end_time: '2026-09-30T06:45:00Z', total_seconds: 8100,
  description: 'Fixed the login bug after the timer was left off.', is_billable: true,
  approval_status, approved_by: null, approved_at: null, mirrored_time_entry_id: null,
  member_name: 'Asha Patel', member_email: 'asha@example.invalid',
  project_name: 'Website Revamp', task_name: 'Login fix', has_conflict: false,
  reason: 'forgot_timer', created_at: '2026-09-30T08:00:00Z', ...extra,
});

describe('Manual Requests: View', () => {
  let container: HTMLDivElement;
  let root: Root;

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
  const click = async (element: Element) => {
    await act(async () => {
      element.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await settle();
  };
  const viewButtons = () =>
    Array.from(container.querySelectorAll('tbody button')).filter((b) => b.textContent === 'View') as HTMLButtonElement[];
  const dialog = () => document.querySelector('[role="dialog"]');

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes('manual')) {
        return json({
          items: [
            request(1, 'pending', { has_conflict: true }),
            request(2, 'approved', { approved_at: '2026-10-01T05:00:00Z', description: '', reason: null }),
            request(3, 'rejected', { approved_at: '2026-10-01T06:00:00Z' }),
          ],
          pagination: { page: 1, limit: 20, total: 3, total_pages: 1 },
        });
      }
      if (url.includes('/members') || url.includes('/projects')) {
        return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
      }
      return json({ items: [], pagination: { page: 1, limit: 20, total: 0, total_pages: 1 } });
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(
        <Provider store={store}>
          <MemoryRouter initialEntries={['/admin/time-tracking?tab=requests']}>
            <AdminTimeTracking />
          </MemoryRouter>
        </Provider>,
      );
    });
    await settle();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('puts a View button on every request, whatever its status', () => {
    expect(viewButtons()).toHaveLength(3);
    // Same look as the Feedback page's View button.
    expect(viewButtons()[0].className).toContain('bg-[#EFF6FF]');
    expect(viewButtons()[0].className).toContain('text-[#2563EB]');
    // Approve / Reject stay on pending rows only.
    const buttons = Array.from(container.querySelectorAll('tbody button')).map((b) => b.textContent);
    expect(buttons.filter((label) => label === 'Approve')).toHaveLength(1);
  });

  it('opens the request\'s details in a dialog', async () => {
    expect(dialog()).toBeNull();
    await click(viewButtons()[0]);

    const text = dialog()!.textContent!;
    expect(text).toContain('Manual Time Request');
    expect(text).toContain('Asha Patel');
    expect(text).toContain('asha@example.invalid');
    expect(text).toContain('Website Revamp');
    expect(text).toContain('Login fix');
    expect(text).toContain('02:15:00');                       // 8100 s
    expect(text).toContain('10:00');                          // 04:30Z in IST
    expect(text).toContain('12:15');                          // 06:45Z in IST
    expect(text).toContain('Forgot to start/stop timer');
    expect(text).toContain('Fixed the login bug after the timer was left off.');
    expect(text).toContain('Pending');
    expect(text).toContain('overlaps time that is already recorded');
  });

  it('is honest about what a request does not have', async () => {
    await click(viewButtons()[1]);                            // approved, no description, no reason

    const text = dialog()!.textContent!;
    expect(text).toContain('No description was provided.');
    expect(text).toContain('Approved on');
    expect(text).not.toContain('overlaps');
  });

  it('shows the reviewed date for a rejected request', async () => {
    await click(viewButtons()[2]);
    expect(dialog()!.textContent).toContain('Rejected on');
  });

  it('closes from the X button, the backdrop and Escape', async () => {
    await click(viewButtons()[0]);
    await click(dialog()!.querySelector('button[aria-label="Close request details"]')!);
    expect(dialog()).toBeNull();

    await click(viewButtons()[0]);
    await click(dialog()!.parentElement!);                    // the backdrop
    expect(dialog()).toBeNull();

    await click(viewButtons()[0]);
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    expect(dialog()).toBeNull();
  });
});
