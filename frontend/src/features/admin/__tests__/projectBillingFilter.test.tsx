// @vitest-environment jsdom
/**
 * The Project Management page's Billing filter: All Billing / Billing / Free.
 *
 * "Billing" is the fixed-hours kind and "Free" is free-time, which the API
 * already filters on as `billing_type=fixed|free`. The page must send exactly
 * that, send nothing for "All Billing", and carry it into the CSV export's
 * request so the export matches what is on screen.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser: { id: 1, role_name: 'administrator', name: 'Admin', permissions: {} } }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminProjectManagement } from '../AdminProjectManagement';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

describe('Project Management: Billing filter', () => {
  let container: HTMLDivElement;
  let root: Root;
  let projectListUrls: string[];

  const flush = async () => {
    for (let i = 0; i < 5; i += 1) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    projectListUrls = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      if (url.pathname.endsWith('/project-management/metadata')) {
        return json({ roles: [], project_statuses: [], task_statuses: [] });
      }
      if (url.pathname.endsWith('/projects') && request.method === 'GET') {
        projectListUrls.push(url.search);
        return json({ items: [], pagination: { page: 1, limit: 20, total: 0, total_pages: 0 } });
      }
      if (url.pathname.endsWith('/members')) return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
      if (url.pathname.includes('/projects/')) return json([]);
      return json({}, 404);
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(<Provider store={store}><AdminProjectManagement /></Provider>);
    });
    await flush();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const select = () => container.querySelector<HTMLSelectElement>('select[aria-label="Filter by billing"]')!;
  const choose = async (value: string) => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(select(), value);
    await act(async () => { select().dispatchEvent(new Event('change', { bubbles: true })); });
    await flush();
  };
  const lastQuery = () => new URLSearchParams(projectListUrls[projectListUrls.length - 1]);

  it('offers Budget & Billing, Billing and Free, defaulting to all', () => {
    expect(Array.from(select().options).map((o) => [o.value, o.textContent])).toEqual([
      ['', 'Budget & Billing'],
      ['fixed', 'Billing'],
      ['free', 'Free'],
    ]);
    expect(select().value).toBe('');
    expect(lastQuery().has('billing_type')).toBe(false);
  });

  it('asks the API only for fixed-billing projects when Billing is chosen', async () => {
    await choose('fixed');
    expect(lastQuery().get('billing_type')).toBe('fixed');
    expect(lastQuery().get('page')).toBe('1');
  });

  it('asks only for free-time projects when Free is chosen, and drops the filter again for Budget & Billing', async () => {
    await choose('free');
    expect(lastQuery().get('billing_type')).toBe('free');

    // Back to All reuses the cached unfiltered list, so no request is needed:
    // the proof is that the filter is cleared and only the very first (unfiltered)
    // request ever went out without it.
    await choose('');
    expect(select().value).toBe('');
    expect(projectListUrls.filter((search) => !new URLSearchParams(search).has('billing_type'))).toHaveLength(1);
  });
});
