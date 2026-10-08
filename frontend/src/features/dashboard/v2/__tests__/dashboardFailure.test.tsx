// @vitest-environment jsdom
/**
 * "Your dashboard could not be loaded for this range. Please try again." -- with
 * nothing to press, over a row of false zeros, and (after a range change) over
 * the PREVIOUS range's numbers.
 *
 * The dashboard now says what kind of failure it was, always offers Retry for a
 * failure that can pass, shows nothing rather than zeros when it has nothing,
 * and never shows another range's figures under this range's heading. A session
 * that has really ended is told apart from a bad connection.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { MemoryRouter } from 'react-router-dom';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../../store/api/baseApi';
import { dashboardApi } from '../../../../store/api/dashboardApi';

vi.mock('../V2Shell', () => ({
  V2Shell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import { DashboardV2 } from '../DashboardV2';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const page = (items: unknown[] = []) => ({ items, total: items.length, page: 1, limit: 5, pages: 1 });
const dashboardBody = (activeProjects = 7) => ({
  filters: { start_date: '', end_date: '', project_id: [], task_id: [], member_id: [] },
  summary: {
    activity: 55, monthly_activity: null, total_seconds: 3600, total_hours: 1,
    active_projects: activeProjects, team_members: 3, total_tasks: 2,
  },
  time_tracked: { interval: 'day', data: [] },
  top_projects: page(), top_members: page(), top_apps: { ...page(), total_app_hours: 0 },
  billable_projects: [], internal_projects: [],
});

// These tests wait out RTK's own backoff (up to ~2.5 s) and a real failure
// banner; the default 5 s test timeout is not enough under a loaded runner.
vi.setConfig({ testTimeout: 30_000 });

describe('Dashboard: a failed load', () => {
  let container: HTMLDivElement;
  let root: Root;
  let dashboardStatus: number | 'network';
  let dashboardCalls: number;

  const settle = async (ms = 60) => {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, ms)); });
  };
  const waitFor = async (condition: () => boolean, timeoutMs = 20000) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (condition()) return;
      await settle(40);
    }
    throw new Error('timed out waiting for the condition');
  };
  const retryButton = () =>
    Array.from(container.querySelectorAll('[role="alert"] button')).find((b) => b.textContent?.includes('Retry'));

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.clear();
    localStorage.setItem('accessToken', 'a');
    localStorage.setItem('refreshToken', 'r');
    localStorage.setItem('monitra.session.expiresAt', String(Date.now() + 86_400_000));
    dashboardStatus = 503;
    dashboardCalls = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      if (url.pathname.endsWith('/react/dashboard')) {
        dashboardCalls += 1;
        if (dashboardStatus === 'network') throw new TypeError('Failed to fetch');
        return dashboardStatus === 200 ? json(dashboardBody()) : json({ detail: 'x' }, dashboardStatus);
      }
      if (url.pathname.endsWith('/projects')) {
        return json({ items: [], pagination: { page: 1, limit: 100, total: 0, total_pages: 1 } });
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
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('says it is temporary, offers Retry, and shows no false zeros', async () => {
    await waitFor(() => !!container.querySelector('[role="alert"]'));
    const alert = container.querySelector('[role="alert"]')!;
    expect(alert.getAttribute('data-error-kind')).toBe('server');
    expect(alert.textContent).toMatch(/temporarily unavailable/i);
    expect(retryButton()).toBeTruthy();
    expect(container.textContent).not.toContain('00:00:00');
    expect(container.textContent).not.toMatch(/Please try again/);
  });

  it('tried again by itself before giving up (3 requests per window)', async () => {
    await waitFor(() => !!container.querySelector('[role="alert"]'));
    expect(dashboardCalls).toBeGreaterThanOrEqual(3);
  });

  it('Retry recovers the page and the notice goes away', async () => {
    await waitFor(() => !!retryButton());
    dashboardStatus = 200;
    await act(async () => { retryButton()!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    await waitFor(() => !container.querySelector('[role="alert"]'));
    expect(container.textContent).toContain('7');   // the real figure, not a zero
  });

  it('a lost connection is told apart from a failing server', async () => {
    // fresh mount with the network down
    await act(async () => root.unmount());
    dashboardStatus = 'network';
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(<Provider store={store}><MemoryRouter><DashboardV2 /></MemoryRouter></Provider>);
    });
    await waitFor(() => !!container.querySelector('[role="alert"]'));
    expect(container.querySelector('[role="alert"]')!.getAttribute('data-error-kind')).toBe('network');
    expect(container.querySelector('[role="alert"]')!.textContent).toMatch(/Connection temporarily unavailable/);
  });

  it('a session that has ended is not presented as a bad connection, and is not retried', async () => {
    // The mount from beforeEach is still riding out its own 503s (RTK re-sends
    // after up to ~2.5 s of backoff); let it finish so its requests are not
    // counted as this test's.
    await waitFor(() => !!container.querySelector('[role="alert"]'));
    await settle(4000);
    await act(async () => root.unmount());
    localStorage.removeItem('refreshToken');          // nothing to renew it from
    dashboardStatus = 401;
    dashboardCalls = 0;
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(<Provider store={store}><MemoryRouter><DashboardV2 /></MemoryRouter></Provider>);
    });
    await waitFor(() => !!container.querySelector('[role="alert"]'));
    const alert = container.querySelector('[role="alert"]')!;
    expect(alert.getAttribute('data-error-kind')).toBe('auth');
    expect(alert.textContent).toMatch(/sign in again/i);
    expect(retryButton()).toBeFalsy();
    // one request per window, not a retry loop against a token the server refused
    expect(dashboardCalls).toBeLessThanOrEqual(2);
  });
});

describe('Dashboard: an older response can never overwrite a newer range', () => {
  it('each range is its own cache entry, so a slow answer for range A cannot replace range B', async () => {
    localStorage.clear();
    localStorage.setItem('accessToken', 'a');
    localStorage.setItem('refreshToken', 'r');
    localStorage.setItem('monitra.session.expiresAt', String(Date.now() + 86_400_000));
    let releaseA: () => void = () => undefined;
    const gateA = new Promise<void>((resolve) => { releaseA = resolve; });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input));
      if (url.searchParams.get('start_date') === '2026-01-01') {
        await gateA;                                   // range A answers last
        return json(dashboardBody(111));
      }
      return json(dashboardBody(222));                 // range B answers at once
    }));
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    const argsA = { start_date: '2026-01-01', end_date: '2026-01-07', top_n: 5 };
    const argsB = { start_date: '2026-02-01', end_date: '2026-02-07', top_n: 5 };

    const a = store.dispatch(dashboardApi.endpoints.getReactDashboard.initiate(argsA));
    const b = store.dispatch(dashboardApi.endpoints.getReactDashboard.initiate(argsB));
    expect((await b).data?.summary.active_projects).toBe(222);

    releaseA();
    expect((await a).data?.summary.active_projects).toBe(111);

    const state = store.getState();
    expect(dashboardApi.endpoints.getReactDashboard.select(argsB)(state).data?.summary.active_projects).toBe(222);
    expect(dashboardApi.endpoints.getReactDashboard.select(argsA)(state).data?.summary.active_projects).toBe(111);
    vi.unstubAllGlobals();
  });
});
