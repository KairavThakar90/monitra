// @vitest-environment jsdom
/**
 * The Project Management page's project-type filter, the same two-step control
 * the Task Listing page has: "All Project Types / Billing / Non Billing", then --
 * under Billing only -- "All Billing / Fixed Hours / Flexible Time".
 *
 * "Billing" with no second choice means *both* billed kinds, so the page sends
 * `billing_type=fixed&billing_type=free`; the API takes the parameter repeated,
 * like the Reports endpoint. The page must send exactly that, send nothing for
 * "All Project Types", and never let a stale second choice narrow a scope it no
 * longer belongs to. The old single "Budget & Billing" select is gone.
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

/**
 * A project row whose name records the billing filter the response was for.
 * RTK Query serves a filter combination it has already fetched from its cache
 * without a new request, so "the last request" is not always the current
 * filter; what the table shows is.
 */
const rowFor = (types: string[]) => ({
  id: 1,
  project_name: `types:${types.join(',') || 'none'}`,
  description: 'About it',
  status: { id: 1, name: 'Active', color: '#22C55E' },
  owner: null,
  leader: null,
  employees: [],
  deadline: null,
  billing_type: 'fixed',
  fixed_hours: '100.00',
  category: null,
  organization_id: 1,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
  tasks: [],
  employee_count: 0,
  task_count: 0,
});

describe('Project Management: project type filter', () => {
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
        // Three pages, so a test can stand on page 2 before it changes a filter.
        return json({
          items: [rowFor(url.searchParams.getAll('billing_type'))],
          pagination: { page: Number(url.searchParams.get('page')), limit: 20, total: 50, total_pages: 3 },
        });
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

  const typeSelect = () => container.querySelector<HTMLSelectElement>('select[aria-label="Filter by project type"]')!;
  const kindSelect = () => container.querySelector<HTMLSelectElement>('select[aria-label="Filter by billing type"]');
  const choose = async (select: HTMLSelectElement, value: string) => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(select, value);
    await act(async () => { select.dispatchEvent(new Event('change', { bubbles: true })); });
    await flush();
  };
  const optionsOf = (select: HTMLSelectElement) => Array.from(select.options).map((o) => [o.value, o.textContent]);
  const lastQuery = () => new URLSearchParams(projectListUrls[projectListUrls.length - 1]);
  /** What the last request asked for. Only meaningful right after a combination not seen before. */
  const sentTypes = () => lastQuery().getAll('billing_type');
  /** The billing filter the rows on screen were fetched with: 'none', or the types joined by a comma. */
  const applied = () => /types:([a-z_,]+)/.exec(container.textContent ?? '')?.[1];

  describe('the control', () => {
    it('offers All Project Types, Billing and Non Billing, defaulting to all', () => {
      expect(optionsOf(typeSelect())).toEqual([
        ['', 'All Project Types'],
        ['billing', 'Billing'],
        ['non_billing', 'Non Billing'],
      ]);
      expect(typeSelect().value).toBe('');
      expect(lastQuery().has('billing_type')).toBe(false);
    });

    it('replaces the old Budget & Billing select rather than sitting beside it', () => {
      expect(container.querySelector('select[aria-label="Filter by billing"]')).toBeNull();
      const optionTexts = Array.from(container.querySelectorAll('select option')).map((o) => o.textContent);
      expect(optionTexts).not.toContain('Budget & Billing');
      expect(optionTexts).not.toContain('Free');
    });

    it('shows no second choice until Billing is chosen', () => {
      expect(kindSelect()).toBeNull();
    });

    it('offers All Billing, Fixed Hours and Flexible Time under Billing, using the Create Project form’s names', async () => {
      await choose(typeSelect(), 'billing');
      expect(optionsOf(kindSelect()!)).toEqual([
        ['', 'All Billing'],
        ['fixed', 'Fixed Hours'],
        ['free', 'Flexible Time'],
      ]);
      expect(kindSelect()!.value).toBe('');
    });

    it('has no second choice under Non Billing, which has nothing to choose between', async () => {
      await choose(typeSelect(), 'non_billing');
      expect(kindSelect()).toBeNull();
    });
  });

  describe('what the page asks the API for', () => {
    it('asks for both billed kinds when Billing is chosen on its own', async () => {
      await choose(typeSelect(), 'billing');
      expect(sentTypes()).toEqual(['fixed', 'free']);
      expect(lastQuery().get('page')).toBe('1');
    });

    it('asks only for fixed-hours projects under Billing → Fixed Hours', async () => {
      await choose(typeSelect(), 'billing');
      await choose(kindSelect()!, 'fixed');
      expect(sentTypes()).toEqual(['fixed']);
    });

    it('asks only for flexible-time projects under Billing → Flexible Time', async () => {
      await choose(typeSelect(), 'billing');
      await choose(kindSelect()!, 'free');
      expect(sentTypes()).toEqual(['free']);
    });

    it('asks only for non-billing projects when Non Billing is chosen', async () => {
      await choose(typeSelect(), 'non_billing');
      expect(sentTypes()).toEqual(['non_billing']);
    });

    it('widens back to both billed kinds when All Billing is chosen again', async () => {
      await choose(typeSelect(), 'billing');
      await choose(kindSelect()!, 'fixed');
      await choose(kindSelect()!, '');
      expect(applied()).toBe('fixed,free');
    });

    it('drops the filter entirely when All Project Types is chosen again', async () => {
      await choose(typeSelect(), 'billing');
      await choose(kindSelect()!, 'free');
      await choose(typeSelect(), '');
      expect(typeSelect().value).toBe('');
      expect(kindSelect()).toBeNull();
      expect(applied()).toBe('none');
    });
  });

  describe('the two steps stay consistent', () => {
    it('forgets Fixed Hours on leaving Billing, so it cannot narrow Non Billing', async () => {
      await choose(typeSelect(), 'billing');
      await choose(kindSelect()!, 'fixed');
      await choose(typeSelect(), 'non_billing');
      expect(sentTypes()).toEqual(['non_billing']);
    });

    it('comes back to Billing with the second choice reset, not remembered', async () => {
      await choose(typeSelect(), 'billing');
      await choose(kindSelect()!, 'fixed');
      await choose(typeSelect(), 'non_billing');
      await choose(typeSelect(), 'billing');
      expect(kindSelect()!.value).toBe('');
      expect(applied()).toBe('fixed,free');
    });

    it('goes back to page 1 whenever either choice changes', async () => {
      const goToPage = async (label: string) => {
        const button = Array.from(container.querySelectorAll('button')).find((b) => b.textContent === label)!;
        await act(async () => { button.click(); });
        await flush();
      };

      await goToPage('2');
      expect(lastQuery().get('page')).toBe('2');
      await choose(typeSelect(), 'billing');
      expect(lastQuery().get('page')).toBe('1');

      await goToPage('2');
      expect(lastQuery().get('page')).toBe('2');
      await choose(kindSelect()!, 'free');
      expect(lastQuery().get('page')).toBe('1');
    });
  });
});
