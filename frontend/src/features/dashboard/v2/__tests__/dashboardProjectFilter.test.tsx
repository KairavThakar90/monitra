// @vitest-environment jsdom
/**
 * The Dashboard's project filter. It opens on every project (nothing sent), lets
 * several be picked at once, and sends them as repeated `project_id` -- on the
 * previous-window request too, so the delta badges compare like with like.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { MemoryRouter } from 'react-router-dom';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../../store/api/baseApi';

vi.mock('../V2Shell', () => ({
  V2Shell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import { DashboardV2 } from '../DashboardV2';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const page = (items: unknown[] = []) => ({ items, total: items.length, page: 1, limit: 5, pages: 1 });
const dashboard = () => ({
  filters: { start_date: '', end_date: '', project_id: [], task_id: [], member_id: [] },
  summary: { activity: null, monthly_activity: null, total_seconds: 0, total_hours: 0, active_projects: 0, team_members: 0, total_tasks: 0 },
  time_tracked: { interval: 'day', data: [] },
  top_projects: page(),
  top_members: page(),
  top_apps: { ...page(), total_app_hours: 0 },
  billable_projects: [],
  internal_projects: [],
});
const project = (id: number, name: string) => ({ id, project_name: name, organization_id: 1, tasks: [] });

describe('Dashboard: project filter', () => {
  let container: HTMLDivElement;
  let root: Root;
  let dashboardQueries: URLSearchParams[];

  const flush = async () => {
    for (let i = 0; i < 6; i += 1) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const click = async (el: Element | undefined) => {
    expect(el).toBeTruthy();
    await act(async () => { el!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    await flush();
  };
  const buttonByText = (text: string) =>
    Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.includes(text));

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    window.localStorage.clear();
    dashboardQueries = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      if (url.pathname.endsWith('/react/dashboard')) {
        dashboardQueries.push(url.searchParams);
        return json(dashboard());
      }
      if (url.pathname.endsWith('/projects')) {
        const items = [project(11, 'Alpha Project'), project(12, 'Beta Project'), project(13, 'Gamma Project')];
        return json({ items, pagination: { page: 1, limit: 100, total: 3, total_pages: 1 } });
      }
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
      root.render(
        <Provider store={store}>
          <MemoryRouter><DashboardV2 /></MemoryRouter>
        </Provider>,
      );
    });
    await flush();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('defaults to all projects and sends no project filter', () => {
    expect(buttonByText('All projects')).toBeTruthy();
    expect(dashboardQueries.length).toBeGreaterThan(0);
    for (const query of dashboardQueries) expect(query.has('project_id')).toBe(false);
  });

  it('sends every picked project, on the current and the previous window', async () => {
    dashboardQueries = [];
    await click(buttonByText('All projects'));
    await click(Array.from(container.querySelectorAll('li button')).find((b) => b.textContent?.includes('Alpha Project')));
    await click(Array.from(container.querySelectorAll('li button')).find((b) => b.textContent?.includes('Gamma Project')));

    const latest = dashboardQueries.filter((q) => q.getAll('project_id').length === 2);
    expect(latest.length).toBeGreaterThanOrEqual(2); // current window + previous window
    for (const query of latest) expect(query.getAll('project_id').sort()).toEqual(['11', '13']);
    expect(new Set(latest.map((q) => q.get('start_date'))).size).toBe(2);
  });

  it('Reset puts the filter back on all projects', async () => {
    await click(buttonByText('All projects'));
    await click(Array.from(container.querySelectorAll('li button')).find((b) => b.textContent?.includes('Beta Project')));
    expect(buttonByText('Beta Project')).toBeTruthy();

    await click(buttonByText('Reset'));
    expect(buttonByText('All projects')).toBeTruthy();
  });
});
