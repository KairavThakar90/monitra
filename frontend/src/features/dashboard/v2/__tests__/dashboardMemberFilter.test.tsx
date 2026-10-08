// @vitest-environment jsdom
/**
 * The Dashboard's member filter -- the Reports page's `MemberMultiSelect`. It opens on
 * every member (nothing sent), lets several be picked at once, and sends them as repeated
 * `member_id` -- on the previous-window request too, so the delta badges compare like with
 * like -- and it composes with the project filter. Reset puts it back on everyone.
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
/** Everyone averages 11% activity; any member-filtered answer says 77%, so the screen shows which one it got. */
const EVERYONE = '11.0%';
const PICKED = '77.0%';
const dashboard = (filteredByMember: boolean) => ({
  filters: { start_date: '', end_date: '', project_id: [], task_id: [], member_id: [] },
  summary: { activity: filteredByMember ? 77 : 11, monthly_activity: null, total_seconds: 0, total_hours: 0, active_projects: 0, team_members: 0, total_tasks: 0 },
  time_tracked: { interval: 'day', data: [] },
  top_projects: page(),
  top_members: page(),
  top_apps: { ...page(), total_app_hours: 0 },
  billable_projects: [],
  internal_projects: [],
});
const project = (id: number, name: string) => ({ id, project_name: name, organization_id: 1, tasks: [] });
const member = (id: number, name: string) => ({ id, name, email: `${name.split(' ')[0].toLowerCase()}@example.com`, role: 'employee', status: 'active' });

describe('Dashboard: member filter', () => {
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
  const memberOption = (name: string) =>
    Array.from(container.querySelectorAll('.custom-scrollbar button')).find((b) => b.textContent?.includes(name));
  const projectOption = (name: string) =>
    Array.from(container.querySelectorAll('li button')).find((b) => b.textContent?.includes(name));

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    window.localStorage.clear();
    dashboardQueries = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      if (url.pathname.endsWith('/react/dashboard')) {
        dashboardQueries.push(url.searchParams);
        return json(dashboard(url.searchParams.has('member_id')));
      }
      if (url.pathname.endsWith('/members')) {
        const items = [member(2, 'Alice Active'), member(3, 'Bob Builder'), member(4, 'Carl Casual')];
        return json({ items, page: 1, limit: 100, total: 3, pages: 1 });
      }
      if (url.pathname.endsWith('/projects')) {
        const items = [project(11, 'Alpha Project'), project(12, 'Beta Project')];
        return json({ items, pagination: { page: 1, limit: 100, total: 2, total_pages: 1 } });
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

  it('shows the Reports page member filter, on all members, and sends no member filter', () => {
    expect(buttonByText('All members')).toBeTruthy();
    expect(dashboardQueries.length).toBeGreaterThan(0);
    for (const query of dashboardQueries) expect(query.has('member_id')).toBe(false);
    expect(container.textContent).toContain(EVERYONE);
  });

  it('lists the organization members in a searchable list', async () => {
    await click(buttonByText('All members'));
    expect(container.querySelector('input[placeholder="Search members..."]')).toBeTruthy();
    expect(memberOption('Alice Active')).toBeTruthy();
    expect(memberOption('Bob Builder')).toBeTruthy();
    expect(memberOption('Carl Casual')).toBeTruthy();
  });

  it('sends every picked member, on the current and the previous window', async () => {
    await click(buttonByText('All members'));
    dashboardQueries = [];
    await click(memberOption('Alice Active'));
    await click(memberOption('Carl Casual'));

    const latest = dashboardQueries.filter((q) => q.getAll('member_id').length === 2);
    expect(latest.length).toBeGreaterThanOrEqual(2); // current window + previous window
    for (const query of latest) expect(query.getAll('member_id').sort()).toEqual(['2', '4']);
    expect(new Set(latest.map((q) => q.get('start_date'))).size).toBe(2);
    // ...and the page is showing the member-filtered answer, not the everyone one.
    expect(container.textContent).toContain(PICKED);
    expect(container.textContent).not.toContain(EVERYONE);
  });

  it('un-picking the last member goes back to everyone', async () => {
    await click(buttonByText('All members'));
    await click(memberOption('Bob Builder'));
    expect(container.textContent).toContain(PICKED);

    dashboardQueries = [];
    await click(memberOption('Bob Builder'));

    // Back on the unfiltered request (RTK Query may serve it from cache, so it
    // is the screen, not a new request, that proves it) and nothing sent member_id.
    expect(container.textContent).toContain(EVERYONE);
    expect(container.textContent).not.toContain(PICKED);
    for (const query of dashboardQueries) expect(query.has('member_id')).toBe(false);
  });

  it('combines with the project filter', async () => {
    await click(buttonByText('All projects'));
    await click(projectOption('Beta Project'));
    await click(buttonByText('All members'));
    dashboardQueries = [];
    await click(memberOption('Bob Builder'));

    const latest = dashboardQueries.filter((q) => q.getAll('member_id').length === 1);
    expect(latest.length).toBeGreaterThanOrEqual(2);
    for (const query of latest) {
      expect(query.getAll('member_id')).toEqual(['3']);
      expect(query.getAll('project_id')).toEqual(['12']);
    }
  });

  it('Reset puts the member filter back on all members and stops sending it', async () => {
    await click(buttonByText('All members'));
    await click(memberOption('Alice Active'));
    expect(buttonByText('All members')).toBeFalsy();
    expect(container.textContent).toContain(PICKED);

    dashboardQueries = [];
    await click(buttonByText('Reset'));
    expect(buttonByText('All members')).toBeTruthy();
    expect(container.textContent).toContain(EVERYONE);
    expect(container.textContent).not.toContain(PICKED);
    for (const query of dashboardQueries) expect(query.has('member_id')).toBe(false);
  });
});
