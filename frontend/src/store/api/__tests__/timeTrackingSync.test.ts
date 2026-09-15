/**
 * Tracked time on the web has to move without a page reload.
 *
 * The web client keeps no timer of its own: everything it shows is a server
 * aggregate held in the RTK Query cache. Two things made that cache go stale
 * in a way a reload "fixed":
 *
 *  - `refetchOnFocus` / `refetchOnReconnect` were configured but
 *    `setupListeners` was never called, so they did nothing. Coming back to
 *    the tab after stopping a timer on the desktop showed the old total.
 *  - The manual-time mutations invalidated `{ TimeTracking, LIST }`, which the
 *    dashboard and report queries -- tagged with the bare type -- do not
 *    match. Approving an entry refreshed the day list beside KPI cards that
 *    still showed the previous figure.
 *
 * These tests drive the real slice through a real store with a fake fetch.
 * There is no DOM in this suite, so the focus event is delivered the way
 * `setupListeners`' own handler delivers it -- as the `onFocus` action -- and
 * the wiring of the listener itself is checked at the source.
 */
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import storeSource from '../../index.ts?raw';
import { baseApi } from '../baseApi';
import { dashboardApi } from '../dashboardApi';
import { manualTimeEntryApi } from '../manualTimeEntryApi';
import { reportsApi } from '../reportsApi';
import { timeTrackingApi } from '../timeTrackingApi';

const dashboardPayload = (seconds: number) => ({
  filters: { start_date: '2026-09-15', end_date: '2026-09-15', project_id: [], task_id: [], member_id: [] },
  summary: {
    activity: null,
    monthly_activity: null,
    total_seconds: seconds,
    total_hours: Math.round((seconds / 3600) * 100) / 100,
    active_projects: 1,
    team_members: 1,
    total_tasks: 1,
  },
  time_tracked: { interval: 'day', data: [] },
  top_projects: { items: [], page: 1, limit: 10, total: 0, pages: 0 },
  top_members: { items: [], page: 1, limit: 10, total: 0, pages: 0 },
  top_apps: { items: [], page: 1, limit: 10, total: 0, pages: 0, total_app_hours: 0 },
});

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const makeStore = () =>
  configureStore({
    reducer: { [baseApi.reducerPath]: baseApi.reducer },
    middleware: (getDefault) => getDefault().concat(baseApi.middleware),
  });

const range = { start_date: '2026-09-15', end_date: '2026-09-15' };

const summarySeconds = (store: ReturnType<typeof makeStore>) =>
  dashboardApi.endpoints.getReactDashboard.select(range)(store.getState() as never).data?.summary
    .total_seconds;

const urlOf = (input: RequestInfo | URL) =>
  typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;

describe('tracked-time cache synchronization', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    const entries = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => entries.get(key) ?? null,
      setItem: (key: string, value: string) => entries.set(key, value),
      removeItem: (key: string) => entries.delete(key),
      clear: () => entries.clear(),
    });
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /** Answers every GET with an empty page, and the dashboard with a counter. */
  const answerEverything = () => {
    let dashboardReads = 0;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method && init.method !== 'GET') return jsonResponse({ id: 1 }, 201);
      if (urlOf(input).includes('/react/dashboard')) {
        dashboardReads += 1;
        return jsonResponse(dashboardPayload(dashboardReads === 1 ? 10 : 3610));
      }
      return jsonResponse({ items: [], total: 0, page: 1, limit: 50, pages: 0, total_seconds: 0, total_hours: 0 });
    });
  };

  it('a manual entry approval invalidates the dashboard summary, not only the day list', async () => {
    const store = makeStore();
    answerEverything();

    const dashboard = store.dispatch(dashboardApi.endpoints.getReactDashboard.initiate(range));
    const dayList = store.dispatch(timeTrackingApi.endpoints.getTimeTracking.initiate({ ...range }));
    await Promise.all([dashboard, dayList]);
    expect(summarySeconds(store)).toBe(10);

    await store.dispatch(manualTimeEntryApi.endpoints.approveManualTimeEntryRequest.initiate(7));

    // Invalidation re-fetches every subscribed query carrying the tag.
    await vi.waitFor(() => {
      expect(summarySeconds(store)).toBe(3610);
    });
    dashboard.unsubscribe();
    dayList.unsubscribe();
  });

  it('creating manual time refreshes the dashboard the same way', async () => {
    for (const start of [
      () =>
        manualTimeEntryApi.endpoints.createManualTimeEntry.initiate({
          project_id: 1, task_id: 2, work_date: '2026-09-15', total_seconds: 600,
        } as never),
      () =>
        manualTimeEntryApi.endpoints.createManualTimeEntryRequest.initiate({
          project_id: 1, task_id: 2, work_date: '2026-09-15', total_seconds: 600, reason: 'forgot to start',
        } as never),
    ]) {
      const store = makeStore();
      answerEverything();
      const dashboard = store.dispatch(dashboardApi.endpoints.getReactDashboard.initiate(range));
      await dashboard;
      expect(summarySeconds(store)).toBe(10);

      await store.dispatch(start());
      await vi.waitFor(() => {
        expect(summarySeconds(store)).toBe(3610);
      });
      dashboard.unsubscribe();
    }
  });

  it('every tracked-time query is reached by a TimeTracking invalidation', async () => {
    const store = makeStore();
    answerEverything();
    const subscriptions = [
      store.dispatch(dashboardApi.endpoints.getReactDashboard.initiate(range)),
      store.dispatch(reportsApi.endpoints.getReactReportsSummary.initiate(range)),
      store.dispatch(reportsApi.endpoints.getReactReportsList.initiate({ dimension: 'tasks', ...range })),
      store.dispatch(reportsApi.endpoints.getReactReportsTrend.initiate(range)),
      store.dispatch(reportsApi.endpoints.getProjectTaskSummary.initiate({ page: 1, limit: 10 } as never)),
      store.dispatch(timeTrackingApi.endpoints.getTimeTracking.initiate({ ...range })),
      store.dispatch(timeTrackingApi.endpoints.getTimeTrackingDetails.initiate({ employeeId: 1, ...range })),
    ];
    await Promise.all(subscriptions);
    const firstRound = fetchMock.mock.calls.length;
    expect(firstRound).toBe(subscriptions.length);

    // The bare type is what the mutations above invalidate.
    store.dispatch(baseApi.util.invalidateTags(['TimeTracking']));

    await vi.waitFor(() => {
      expect(fetchMock.mock.calls.length).toBe(firstRound * 2);
    });
    subscriptions.forEach((subscription) => subscription.unsubscribe());
  });

  it('regaining focus re-reads a tracked-time query without a reload', async () => {
    const store = makeStore();
    fetchMock
      .mockResolvedValueOnce(jsonResponse(dashboardPayload(10)))
      .mockResolvedValueOnce(jsonResponse(dashboardPayload(25)));

    const subscription = store.dispatch(dashboardApi.endpoints.getReactDashboard.initiate(range));
    await subscription;
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(summarySeconds(store)).toBe(10);

    // The desktop stopped a timer while this tab was in the background and
    // the person comes back to it. This is exactly what the focus listener
    // dispatches; the slice's `refetchOnFocus` is what must act on it.
    store.dispatch(baseApi.internalActions.onFocus());

    await vi.waitFor(() => {
      expect(summarySeconds(store)).toBe(25);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    subscription.unsubscribe();
  });

  it('the store installs the focus and reconnect listeners the slice relies on', () => {
    // Without this call the two `refetchOn*` options are inert. It cannot be
    // exercised here (importing the store starts cache persistence against
    // the browser), so the wiring is pinned at the source.
    expect(storeSource).toMatch(/setupListeners\(store\.dispatch\)/);
  });
});
