// @vitest-environment jsdom
/**
 * The Time Tracking page has exactly one pagination footer, and it is the shared one.
 *
 * It used to have two stacked: a Previous / Next pair inside the table card and, below it,
 * a second "Showing 1 to 50 of 147 Entries" with a "1 / 3" counter -- each saying the same thing
 * in a different style from every other list. Now one footer, `components/Pagination`: the
 * "Showing X to Y of N" line, a rows-per-page picker and numbered pages, as on Members.
 * The real screen is rendered against a real RTK Query store with only `fetch` stubbed.
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

const entry = (id: number) => ({
  employee_id: id, name: `Member ${id}`, email: `m${id}@example.invalid`, designation: 'Developer',
  date: '2026-09-22', start_time: '2026-09-22T04:00:00Z', end_time: '2026-09-22T09:00:00Z',
  total_seconds: 18000, total_hours: '5h 0m', total_time: '05:00:00',
});

describe('Time Tracking: one pagination footer', () => {
  let container: HTMLDivElement;
  let root: Root;
  let requested: URL[];

  const json = (body: unknown) =>
    new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
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
  const text = () => container.textContent ?? '';

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    requested = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const raw = input instanceof Request ? input.url : String(input);
      const url = new URL(raw, 'http://localhost');
      if (url.pathname.includes('time-tracking') && !url.pathname.includes('active')) {
        requested.push(url);
        const page = Number(url.searchParams.get('page') ?? 1);
        const limit = Number(url.searchParams.get('limit') ?? 50);
        const total = 147;
        const first = (page - 1) * limit;
        const items = Array.from({ length: Math.max(0, Math.min(limit, total - first)) }, (_, i) => entry(first + i + 1));
        return json({ items, pagination: { page, limit, total, total_pages: Math.ceil(total / limit) } });
      }
      if (url.pathname.includes('manual')) {
        return json({ items: [], pagination: { page: 1, limit: 50, total: 0, total_pages: 1 } });
      }
      return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
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
          <MemoryRouter initialEntries={['/admin/time-tracking']}>
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

  it('shows the "Showing ... of N" line once, not twice', () => {
    expect(text().match(/Showing/g)).toHaveLength(1);
    expect(text()).toContain('Showing 1 to 50 of 147 entries');
  });

  it('has no Previous / Next text buttons and no "1 / 3" counter any more', () => {
    const labels = Array.from(container.querySelectorAll('button')).map((b) => b.textContent?.trim());
    expect(labels).not.toContain('Previous');
    expect(labels).not.toContain('Next');
    expect(text()).not.toMatch(/\b1 \/ 3\b/);
  });

  it('wears the shared footer: a rows-per-page picker and numbered pages with arrows', () => {
    expect(container.querySelector('select[aria-label="Rows per page"]')).not.toBeNull();
    const numbers = Array.from(container.querySelectorAll('button')).map((b) => b.textContent?.trim());
    expect(numbers).toEqual(expect.arrayContaining(['1', '2', '3']));
    expect(container.querySelector('button[aria-label="Previous page"]')).not.toBeNull();
    expect(container.querySelector('button[aria-label="Next page"]')).not.toBeNull();
  });

  it('goes to the next page when the arrow or a page number is pressed', async () => {
    await click(container.querySelector('button[aria-label="Next page"]')!);
    expect(requested.at(-1)!.searchParams.get('page')).toBe('2');
    expect(text()).toContain('Showing 51 to 100 of 147 entries');

    const three = Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.trim() === '3')!;
    await click(three);
    expect(requested.at(-1)!.searchParams.get('page')).toBe('3');
    expect(text()).toContain('Showing 101 to 147 of 147 entries');
  });

  it('changes the page size from the picker and starts again at page 1', async () => {
    await click(container.querySelector('button[aria-label="Next page"]')!);
    const picker = container.querySelector('select[aria-label="Rows per page"]') as HTMLSelectElement;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!;
      setter.call(picker, '100');
      picker.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await settle();

    const last = requested.at(-1)!;
    expect(last.searchParams.get('limit')).toBe('100');
    expect(last.searchParams.get('page')).toBe('1');
    expect(text()).toContain('Showing 1 to 100 of 147 entries');
  });
});
