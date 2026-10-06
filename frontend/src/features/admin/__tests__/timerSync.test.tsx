// @vitest-environment jsdom
/**
 * Admin Active Users and Admin Time Tracking after a machine sleeps.
 *
 * While a desktop sleeps, its entry stays open on the server, so the figure the
 * web shows keeps growing; when the desktop wakes it ends the session
 * retroactively (the idle answer, the interruption cap, or the IST-midnight
 * split), and the true total is LOWER than the last one the web saw. Nothing
 * pushes that to the browser, and after a wake the window is usually still
 * focused, so `refetchOnFocus` never fires. These tests pin what the pages do
 * instead, with the real pages and a real RTK Query store, and only `fetch` and
 * the clock replaced.
 *
 * Only the interval and `Date` are faked. React, RTK Query's own timers and the
 * test's awaits stay on real timers, so a "sleep" is simply the clock jumping
 * between two ticks -- exactly what a suspend looks like to a page.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { MemoryRouter } from 'react-router-dom';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import { RESUME_TICK_MS } from '../../../hooks/useResume';
import { formatHMS } from '../../../utils/duration';

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({
    currentUser: {
      id: 1, organization_id: 1, username: 'admin', email: 'admin@example.invalid', name: 'Admin',
      role_name: 'administrator', permissions: {}, is_active: true,
    },
  }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminActiveUsers } from '../AdminActiveUsers';
import { AdminTimeTracking } from '../AdminTimeTracking';

const HOUR = 60 * 60 * 1000;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

type Reply = () => Response | Promise<Response>;

const activeEntry = (over: Record<string, unknown> = {}) => ({
  time_entry_id: 501, employee_id: 11, name: 'Asha Patel', email: 'asha@example.com', designation: 'Developer',
  project_id: 92, project_name: 'Alpha Portal', task_id: 185, task_name: 'Build login screen',
  start_time: '2026-10-05T04:30:00Z', elapsed_seconds: 3725, elapsed_time: '01:02:05', ...over,
});
const activePayload = (items: unknown[], serverTime = '2026-10-05T10:00:00Z') => ({
  items, total: items.length, server_time: serverTime,
});

const dayRow = (seconds: number, over: Record<string, unknown> = {}) => ({
  employee_id: 7, name: 'Asha Patel', email: 'asha@example.com', designation: 'Developer',
  date: '2026-10-05', start_time: '2026-10-05T04:30:00Z', end_time: null,
  total_seconds: seconds, total_hours: '', total_time: formatHMS(seconds), ...over,
});
const daysPayload = (seconds: number) => ({
  items: [dayRow(seconds)], pagination: { page: 1, limit: 50, total: 1, total_pages: 1 },
});

describe('timers after a sleep', () => {
  let container: HTMLDivElement;
  let root: Root;
  let store: ReturnType<typeof makeStore>;
  let requests: { path: string; search: string }[];
  let reply: { active: Reply; days: Reply };

  const makeStore = () =>
    configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });

  const settle = async () => {
    for (let i = 0; i < 40; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  const mount = async (page: React.ReactNode) => {
    store = makeStore();
    await act(async () => {
      root.render(
        <Provider store={store}>
          <MemoryRouter initialEntries={['/admin']}>{page}</MemoryRouter>
        </Provider>,
      );
    });
    await settle();
  };

  /** The machine slept for `ms` and woke with this tab still in front: the clock jumps, one tick follows. */
  const sleepFor = async (ms: number) => {
    vi.setSystemTime(Date.now() + ms);
    await act(async () => { await vi.advanceTimersByTimeAsync(RESUME_TICK_MS); });
    await settle();
  };

  const activeRequests = () => requests.filter((r) => r.path.endsWith('/time-tracking/active'));
  const dayRequests = () => requests.filter((r) => r.path.endsWith('/time-tracking'));
  const rows = () => Array.from(container.querySelectorAll('[data-testid="active-user-row"]'));
  const text = () => container.textContent ?? '';
  const activeTable = () => container.querySelector('[data-testid="active-users-table"]');
  const trackingTable = () => container.querySelector('div.overflow-x-auto');
  const paramsOf = (search: string) => new URLSearchParams(search);

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    vi.setSystemTime(new Date('2026-10-05T10:00:00Z')); // 15:30 IST
    requests = [];
    reply = {
      active: () => json(activePayload([activeEntry()])),
      days: () => json(daysPayload(11_565)),
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), 'http://localhost');
      requests.push({ path: url.pathname, search: url.search });
      if (url.pathname.endsWith('/time-tracking/active')) return reply.active();
      if (url.pathname.endsWith('/time-tracking')) return reply.days();
      if (url.pathname.endsWith('/members') || url.pathname.endsWith('/projects')) {
        return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
      }
      return json({ items: [], pagination: { page: 1, limit: 20, total: 0, total_pages: 1 } });
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  describe('Active Users', () => {
    it('re-reads the list as soon as the machine wakes, without any focus event', async () => {
      await mount(<AdminActiveUsers />);
      expect(rows()).toHaveLength(1);
      expect(text()).toContain('01:02:05');
      const before = activeRequests().length;

      // While asleep nothing was reported; on waking, the desktop ended the session.
      reply.active = () => json(activePayload([], '2026-10-05T12:00:05Z'));
      await sleepFor(2 * HOUR);

      expect(activeRequests().length).toBeGreaterThan(before);
      expect(rows()).toHaveLength(0);
      expect(text()).toContain('No one is tracking time right now.');
    });

    it('shows the figure the server now reports, not the one it had before the sleep', async () => {
      await mount(<AdminActiveUsers />);
      expect(text()).toContain('01:02:05');

      reply.active = () => json(activePayload([activeEntry({ elapsed_seconds: 1_800, elapsed_time: '00:30:00' })], '2026-10-05T12:00:05Z'));
      await sleepFor(2 * HOUR);

      expect(text()).toContain('00:30:00');
      expect(text()).not.toContain('01:02:05');
    });

    it('dims the stale figures while they are being replaced, then shows the fresh ones undimmed', async () => {
      await mount(<AdminActiveUsers />);
      expect(activeTable()!.getAttribute('data-stale')).toBe('false');

      let release!: (response: Response) => void;
      reply.active = () => new Promise<Response>((resolve) => { release = resolve; });
      await sleepFor(2 * HOUR); // the re-read is in flight

      expect(activeTable()!.getAttribute('data-stale')).toBe('true');
      expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();

      await act(async () => { release(json(activePayload([activeEntry({ elapsed_time: '00:30:00' })], '2026-10-05T12:00:05Z'))); });
      await settle();

      expect(activeTable()!.getAttribute('data-stale')).toBe('false');
      expect(container.querySelector('[aria-busy="true"]')).toBeNull();
      expect(text()).toContain('00:30:00');
    });

    it('does not dim a snapshot that is only seconds old when a routine re-read runs', async () => {
      await mount(<AdminActiveUsers />);
      let release!: (response: Response) => void;
      reply.active = () => new Promise<Response>((resolve) => { release = resolve; });

      await act(async () => { store.dispatch(baseApi.internalActions.onFocus()); }); // an ordinary refetch, no sleep
      await settle();

      expect(activeTable()!.getAttribute('data-stale')).toBe('false');
      await act(async () => { release(json(activePayload([activeEntry()]))); });
      await settle();
    });

    it('keeps the rows and says they are old when the re-read fails, then clears the note on success', async () => {
      await mount(<AdminActiveUsers />);
      expect(container.querySelector('[data-testid="active-users-refresh-failed"]')).toBeNull();

      reply.active = () => json({ detail: 'down' }, 500);
      await sleepFor(2 * HOUR);

      expect(rows()).toHaveLength(1);
      const note = container.querySelector('[data-testid="active-users-refresh-failed"]');
      expect(note).not.toBeNull();
      expect(note!.textContent).toContain('Could not refresh');
      expect(note!.textContent).toContain('3:30 pm'); // the list is "as of" the last good answer (10:00Z = 3:30 pm IST)

      reply.active = () => json(activePayload([activeEntry({ elapsed_time: '00:45:00' })], '2026-10-05T14:00:05Z'));
      await sleepFor(2 * HOUR);

      expect(container.querySelector('[data-testid="active-users-refresh-failed"]')).toBeNull();
      expect(text()).toContain('00:45:00');
    });

    it('says so even when the list that failed to refresh was empty', async () => {
      reply.active = () => json(activePayload([]));
      await mount(<AdminActiveUsers />);
      reply.active = () => json({ detail: 'down' }, 500);
      await sleepFor(2 * HOUR);
      expect(container.querySelector('[data-testid="active-users-refresh-failed"]')).not.toBeNull();
    });
  });

  describe('Time Tracking', () => {
    it('opens on today’s IST day', async () => {
      await mount(<AdminTimeTracking />);
      const first = paramsOf(dayRequests()[0].search);
      expect(first.get('start_date')).toBe('2026-10-05');
      expect(first.get('end_date')).toBe('2026-10-05');
      expect(first.get('page')).toBe('1');
    });

    it('moves "Today" to the new day when the machine slept through midnight, without a reload', async () => {
      vi.setSystemTime(new Date('2026-10-05T18:00:00Z')); // 23:30 IST
      await mount(<AdminTimeTracking />);
      expect(paramsOf(dayRequests()[0].search).get('start_date')).toBe('2026-10-05');

      await sleepFor(HOUR); // 00:30 IST on the 6th

      const last = paramsOf(dayRequests().at(-1)!.search);
      expect(last.get('start_date')).toBe('2026-10-06');
      expect(last.get('end_date')).toBe('2026-10-06');
      expect(last.get('page')).toBe('1');
    });

    it('moves to the new day on an ordinary tick too, when the page was awake across midnight', async () => {
      vi.setSystemTime(new Date('2026-10-05T18:29:00Z')); // 23:59 IST
      await mount(<AdminTimeTracking />);
      for (let i = 0; i < 14; i += 1) {
        // 70 s of steady ticking, crossing midnight with no jump anywhere.
        await act(async () => { await vi.advanceTimersByTimeAsync(RESUME_TICK_MS); });
      }
      await settle();
      expect(paramsOf(dayRequests().at(-1)!.search).get('start_date')).toBe('2026-10-06');
    });

    it('re-reads the same range on a wake and shows the corrected, lower total', async () => {
      await mount(<AdminTimeTracking />);
      expect(text()).toContain(formatHMS(11_565));
      const before = dayRequests().length;

      // The desktop woke and ended the session retroactively: the real total is lower.
      reply.days = () => json(daysPayload(7_200));
      await sleepFor(HOUR);

      expect(dayRequests().length).toBeGreaterThan(before);
      expect(text()).toContain(formatHMS(7_200));
      expect(text()).not.toContain(formatHMS(11_565));
    });

    it('does not blur or lock the table for a background re-read, only for a first load', async () => {
      let releaseFirst!: (response: Response) => void;
      reply.days = () => new Promise<Response>((resolve) => { releaseFirst = resolve; });
      await mount(<AdminTimeTracking />);
      expect(container.innerHTML).toContain('blur-[2px]'); // the rows for these filters have not arrived

      await act(async () => { releaseFirst(json(daysPayload(11_565))); });
      await settle();
      expect(container.innerHTML).not.toContain('blur-[2px]');

      let releaseSecond!: (response: Response) => void;
      reply.days = () => new Promise<Response>((resolve) => { releaseSecond = resolve; });
      await sleepFor(HOUR); // a re-read is in flight
      expect(container.innerHTML).not.toContain('blur-[2px]');
      expect(trackingTable()!.className).not.toContain('pointer-events-none');

      await act(async () => { releaseSecond(json(daysPayload(7_200))); });
      await settle();
    });

    it('says when the totals were read', async () => {
      await mount(<AdminTimeTracking />);
      expect(container.querySelector('[data-testid="time-tracking-updated"]')!.textContent).toBe('Updated 3:30 pm');
    });

    it('keeps the rows and says they are old when a re-read fails', async () => {
      await mount(<AdminTimeTracking />);
      reply.days = () => json({ detail: 'down' }, 500);
      await sleepFor(HOUR);

      expect(text()).toContain(formatHMS(11_565));
      expect(text()).not.toContain('Unable to load time tracking data.');
      const note = container.querySelector('[data-testid="time-tracking-refresh-failed"]');
      expect(note).not.toBeNull();
      expect(note!.textContent).toContain('Could not refresh');
    });

    it('still reports a plain failure when there was never anything to show', async () => {
      reply.days = () => json({ detail: 'down' }, 500);
      await mount(<AdminTimeTracking />);
      expect(text()).toContain('Unable to load time tracking data.');
      expect(container.querySelector('[data-testid="time-tracking-refresh-failed"]')).toBeNull();
    });
  });
});
