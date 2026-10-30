// @vitest-environment jsdom
/**
 * Admin -> Active Users.
 *
 * The real page is rendered against a real RTK Query store with only `fetch`
 * replaced, so what is pinned is the request a browser would make and what an
 * administrator sees: each member who has a timer running, with the task and
 * project they are on, the time they started, and the server's elapsed figure.
 * Nobody running is an honest empty state, never filler rows.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) => (
    <div>
      <h1>{title}</h1>
      <p>{subtitle}</p>
      {children}
    </div>
  ),
}));

import { AdminActiveUsers } from '../AdminActiveUsers';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const item = (over: Record<string, unknown> = {}) => ({
  time_entry_id: 501,
  employee_id: 11,
  name: 'Asha Patel',
  email: 'asha@example.com',
  designation: 'Developer',
  project_id: 92,
  project_name: 'Alpha Portal',
  task_id: 185,
  task_name: 'Build login screen',
  start_time: '2026-10-01T04:30:00Z', // 10:00 am IST
  elapsed_seconds: 3725,
  elapsed_time: '01:02:05',
  activity_percentage: 74,
  ...over,
});

describe('AdminActiveUsers', () => {
  let container: HTMLDivElement;
  let root: Root;
  let requests: { method: string; path: string }[];
  let respond: () => Response;

  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const mount = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><AdminActiveUsers /></Provider>); });
    await settle();
  };
  const rows = () => Array.from(container.querySelectorAll('[data-testid="active-user-row"]'));

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    requests = [];
    respond = () => json({ items: [], total: 0, server_time: '2026-10-01T05:00:00Z' });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), 'http://localhost');
      requests.push({ method: (request?.method ?? init?.method ?? 'GET').toUpperCase(), path: url.pathname });
      return respond();
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

  it('asks the backend who is running, with one GET', async () => {
    await mount();
    expect(requests).toEqual([{ method: 'GET', path: '/api/v1/time-tracking/active' }]);
    expect(container.querySelector('h1')!.textContent).toBe('Active Users');
  });

  it('shows each active member with the task and project they are working on', async () => {
    respond = () =>
      json({
        items: [
          item(),
          item({
            time_entry_id: 502, employee_id: 12, name: 'Ravi Shah', designation: null,
            project_name: 'Beta Mobile', task_name: 'Fix crash on launch',
            start_time: '2026-10-01T03:00:00Z', elapsed_time: '02:32:10',
          }),
        ],
        total: 2,
        server_time: '2026-10-01T05:00:00Z',
      });
    await mount();

    expect(rows()).toHaveLength(2);
    // Found by member, not position: the page orders rows by start time.
    const rowOf = (name: string) => rows().find((row) => row.textContent!.includes(name))!.textContent!;
    const first = rowOf('Asha Patel');
    expect(first).toContain('Asha Patel');
    expect(first).toContain('Developer');
    expect(first).toContain('Build login screen');
    expect(first).toContain('Alpha Portal');
    expect(first).toContain('10:00 am');
    expect(first).toContain('01:02:05');
    const second = rowOf('Ravi Shah');
    expect(second).toContain('Ravi Shah');
    expect(second).toContain('Fix crash on launch');
    expect(second).toContain('Beta Mobile');
    expect(second).toContain('02:32:10');
    expect(container.querySelector('[data-testid="active-users-count"]')!.textContent).toContain('2 members active');
  });

  describe('a timer whose desktop has gone quiet', () => {
    // The production row: started Friday 17:23 IST, last heard from at 17:50, and the
    // page read "65:32:33" on Monday morning. The server now freezes the figure at the
    // last evidence and says the desktop has stopped reporting in.
    const friday = () =>
      item({
        time_entry_id: 5958, employee_id: 260, name: 'Kunal Vaghela', designation: 'Flutter Developer',
        project_name: 'Coach Companion - Kyle Projects', task_name: 'Mobile Application Development',
        start_time: '2026-10-09T11:53:21Z', // 17:23 IST on Friday
        elapsed_seconds: 2520, elapsed_time: '00:42:00', activity_percentage: null,
        liveness: 'stale', last_seen_at: '2026-10-09T12:20:21Z', measured_until: '2026-10-09T12:35:21Z',
      });
    const monday = '2026-10-12T05:25:54Z'; // 10:55 am IST

    it('says there is no signal, since when, and shows the frozen figure -- not 65 hours', async () => {
      respond = () => json({ items: [friday()], total: 1, server_time: monday });
      await mount();

      const row = rows()[0] as HTMLElement;
      expect(row.getAttribute('data-liveness')).toBe('stale');
      expect(row.textContent).toContain('00:42:00');
      expect(row.textContent).not.toContain('65:');
      const note = row.querySelector('[data-testid="active-user-no-signal"]')!;
      expect(note.textContent).toBe('No signal since 09 Oct, 5:50 pm');
    });

    it('keeps the member on the page rather than hiding them', async () => {
      respond = () => json({ items: [friday()], total: 1, server_time: monday });
      await mount();
      expect(rows()).toHaveLength(1);
      expect(rows()[0].textContent).toContain('Kunal Vaghela');
    });

    it('counts members who are working apart from those with no signal', async () => {
      respond = () =>
        json({
          items: [item({ liveness: 'live', last_seen_at: '2026-10-12T05:25:00Z' }), friday()],
          total: 2,
          server_time: monday,
        });
      await mount();
      expect(container.querySelector('[data-testid="active-users-count"]')!.textContent).toContain('1 member active');
      expect(container.querySelector('[data-testid="active-users-no-signal-count"]')!.textContent).toBe('1 with no signal');
    });

    it('marks a timer whose desktop never reported as unverified, and says nothing for a live row', async () => {
      respond = () =>
        json({
          items: [
            item({ time_entry_id: 1, name: 'Live Lena', liveness: 'live', last_seen_at: '2026-10-12T05:25:30Z' }),
            item({ time_entry_id: 2, name: 'Unknown Uma', liveness: 'unknown', last_seen_at: null }),
            item({ time_entry_id: 3, name: 'Older Omar' }), // a backend that predates the field
          ],
          total: 3,
          server_time: monday,
        });
      await mount();
      expect(container.querySelectorAll('[data-testid="active-user-no-signal"]')).toHaveLength(0);
      expect(container.querySelector('[data-testid="active-users-no-signal-count"]')).toBeNull();
      expect(container.querySelector('[data-testid="active-users-count"]')!.textContent).toContain('3 members active');
      // Only the row the server could not verify says so: not the live one, and not a backend
      // that predates the field (which has said nothing about any row).
      const unverified = rows().filter((row) => row.querySelector('[data-testid="active-user-unverified"]'));
      expect(unverified.map((row) => row.textContent)).toEqual([expect.stringContaining('Unknown Uma')]);
    });

    it('says "no signal" without a time when the server did not give one', async () => {
      respond = () => json({ items: [friday()].map((i) => ({ ...i, last_seen_at: null })), total: 1, server_time: monday });
      await mount();
      expect(container.querySelector('[data-testid="active-user-no-signal"]')!.textContent).toBe('No signal');
    });
  });

  describe('Started column sorting', () => {
    // Deliberately not in start order, and one spelling uses "+00:00" instead of "Z".
    const threeRunning = () =>
      json({
        items: [
          item({ time_entry_id: 1, name: 'Middle Mia', start_time: '2026-10-01T04:00:00Z' }),
          item({ time_entry_id: 2, name: 'Latest Leo', start_time: '2026-10-01T04:45:00+00:00' }),
          item({ time_entry_id: 3, name: 'Earliest Eve', start_time: '2026-10-01T03:15:00Z' }),
        ],
        total: 3,
        server_time: '2026-10-01T05:00:00Z',
      });
    const names = () => rows().map((row) => ['Earliest Eve', 'Middle Mia', 'Latest Leo'].find((n) => row.textContent!.includes(n)));
    const sortButton = () => container.querySelector<HTMLButtonElement>('[data-testid="sort-started"]')!;
    const sortHeader = () => sortButton().closest('th')!;
    const click = async () => {
      await act(async () => { sortButton().dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    };

    it('opens oldest first, as the backend returns it', async () => {
      respond = threeRunning;
      await mount();
      expect(names()).toEqual(['Earliest Eve', 'Middle Mia', 'Latest Leo']);
      expect(sortHeader().getAttribute('aria-sort')).toBe('ascending');
    });

    it('flips to newest first and back on each click', async () => {
      respond = threeRunning;
      await mount();

      await click();
      expect(names()).toEqual(['Latest Leo', 'Middle Mia', 'Earliest Eve']);
      expect(sortHeader().getAttribute('aria-sort')).toBe('descending');

      await click();
      expect(names()).toEqual(['Earliest Eve', 'Middle Mia', 'Latest Leo']);
      expect(sortHeader().getAttribute('aria-sort')).toBe('ascending');
    });

    it('breaks a tie on the entry id, in the chosen direction', async () => {
      respond = () =>
        json({
          items: [
            item({ time_entry_id: 8, name: 'Latest Leo', start_time: '2026-10-01T04:00:00Z' }),
            item({ time_entry_id: 7, name: 'Middle Mia', start_time: '2026-10-01T04:00:00Z' }),
          ],
          total: 2,
          server_time: '2026-10-01T05:00:00Z',
        });
      await mount();
      expect(names()).toEqual(['Middle Mia', 'Latest Leo']);
      await click();
      expect(names()).toEqual(['Latest Leo', 'Middle Mia']);
    });

    it('does not ask the backend again just to reorder', async () => {
      respond = threeRunning;
      await mount();
      await click();
      expect(requests).toHaveLength(1);
    });
  });

  describe('Activity column', () => {
    const activityOf = (name: string) =>
      rows().find((row) => row.textContent!.includes(name))!.querySelector('[data-testid="active-user-activity"]')!.textContent;

    it("shows each member's activity percentage from the server", async () => {
      respond = () =>
        json({
          items: [
            item({ name: 'Asha Patel', activity_percentage: 74 }),
            item({ time_entry_id: 502, employee_id: 12, name: 'Ravi Shah', activity_percentage: 0 }),
          ],
          total: 2,
          server_time: '2026-10-01T05:00:00Z',
        });
      await mount();
      expect(container.textContent).toContain('Activity');
      expect(activityOf('Asha Patel')).toBe('74%');
      // A measured zero is a real figure and is shown as one.
      expect(activityOf('Ravi Shah')).toBe('0%');
    });

    it('shows a dash, never 0%, when no activity has been measured yet', async () => {
      respond = () =>
        json({ items: [item({ activity_percentage: null })], total: 1, server_time: '2026-10-01T05:00:00Z' });
      await mount();
      expect(activityOf('Asha Patel')).toBe('—');
    });
  });

  describe('Desktop column', () => {
    const desktopCell = (name: string) =>
      rows().find((row) => row.textContent!.includes(name))!.querySelector('[data-testid="active-user-desktop"]') as HTMLElement;

    it("shows the desktop version and system each member's desktop last reported", async () => {
      respond = () =>
        json({
          items: [
            item({ name: 'Asha Patel', desktop_version: '1.3.2', desktop_platform: 'win32', desktop_version_seen_at: '2026-10-01T03:00:00Z' }),
            item({ time_entry_id: 502, employee_id: 12, name: 'Ravi Shah', desktop_version: '1.3.3', desktop_platform: 'darwin', desktop_version_seen_at: '2026-10-01T04:00:00Z' }),
            item({ time_entry_id: 503, employee_id: 13, name: 'Meera Rao', desktop_version: '1.2.0', desktop_platform: 'linux' }),
          ],
          total: 3,
          server_time: '2026-10-01T05:00:00Z',
        });
      await mount();

      const headers = Array.from(container.querySelectorAll('th')).map((th) => th.textContent!.trim());
      expect(headers).toContain('Desktop');
      expect(desktopCell('Asha Patel').textContent).toBe('v1.3.2Windows');
      expect(desktopCell('Ravi Shah').textContent).toBe('v1.3.3macOS');
      expect(desktopCell('Meera Rao').textContent).toBe('v1.2.0Linux');
    });

    it('says when it was reported, and that it can lag an upgrade', async () => {
      respond = () =>
        json({
          items: [item({ desktop_version: '1.3.2', desktop_platform: 'win32', desktop_version_seen_at: '2026-10-01T03:00:00Z' })],
          total: 1,
          server_time: '2026-10-01T05:00:00Z',
        });
      await mount();
      const hint = desktopCell('Asha Patel').querySelector('[title]')!.getAttribute('title')!;
      expect(hint).toContain('Monitra desktop 1.3.2');
      expect(hint).toContain('reported 8:30 am'); // 03:00 UTC
      expect(hint).toContain('about every ten hours');
    });

    it('shows a dash, never a guessed version, for a desktop that has not reported one', async () => {
      respond = () =>
        json({
          items: [
            item({ name: 'Asha Patel', desktop_version: null, desktop_platform: null, desktop_version_seen_at: null }),
            item({ time_entry_id: 502, employee_id: 12, name: 'Ravi Shah' }), // an older backend sends no such fields
          ],
          total: 2,
          server_time: '2026-10-01T05:00:00Z',
        });
      await mount();
      expect(desktopCell('Asha Patel').textContent).toBe('—');
      expect(desktopCell('Ravi Shah').textContent).toBe('—');
    });

    it('shows an unfamiliar system name as the desktop sent it, and no system line when there is none', async () => {
      respond = () =>
        json({
          items: [
            item({ name: 'Asha Patel', desktop_version: '1.3.2', desktop_platform: 'freebsd' }),
            item({ time_entry_id: 502, employee_id: 12, name: 'Ravi Shah', desktop_version: '1.3.2', desktop_platform: null }),
          ],
          total: 2,
          server_time: '2026-10-01T05:00:00Z',
        });
      await mount();
      expect(desktopCell('Asha Patel').textContent).toBe('v1.3.2freebsd');
      expect(desktopCell('Ravi Shah').textContent).toBe('v1.3.2');
    });

    it('does not disturb the other columns', async () => {
      respond = () =>
        json({
          items: [item({ desktop_version: '1.3.2', desktop_platform: 'win32' })],
          total: 1,
          server_time: '2026-10-01T05:00:00Z',
        });
      await mount();
      const row = rows()[0].textContent!;
      expect(row).toContain('Build login screen');
      expect(row).toContain('Alpha Portal');
      expect(row).toContain('74%');
      expect(row).toContain('01:02:05');
    });
  });

  it('says "1 member" in the singular', async () => {
    respond = () => json({ items: [item()], total: 1, server_time: '2026-10-01T05:00:00Z' });
    await mount();
    expect(container.querySelector('[data-testid="active-users-count"]')!.textContent).toContain('1 member active');
    expect(container.querySelector('[data-testid="active-users-count"]')!.textContent).not.toContain('1 members');
  });

  it('adds the date when the timer started on an earlier IST day', async () => {
    respond = () =>
      json({
        items: [item({ start_time: '2026-09-30T18:00:00Z' })], // 11:30 pm IST on 30 Sep
        total: 1,
        server_time: '2026-10-01T05:00:00Z',
      });
    await mount();
    // "Sep" or "Sept": the month abbreviation is the runtime's (en-GB), not this page's.
    expect(rows()[0].textContent).toMatch(/30 Sep\w* 2026, 11:30 pm/);
    // A same-day start carries no date (covered above): only the earlier-day one does.
  });

  it('shows when the list was produced, from the server clock', async () => {
    respond = () => json({ items: [item()], total: 1, server_time: '2026-10-01T05:00:00Z' });
    await mount();
    expect(container.querySelector('[data-testid="active-users-updated"]')!.textContent).toBe('Updated 10:30 am');
  });

  it('is an honest empty state when nobody is tracking', async () => {
    await mount();
    expect(rows()).toHaveLength(0);
    expect(container.textContent).toContain('No one is tracking time right now.');
  });

  it('says so when the list cannot be loaded, and shows no rows', async () => {
    respond = () => json({ detail: 'boom' }, 500);
    await mount();
    expect(rows()).toHaveLength(0);
    expect(container.textContent).toContain('Could not load active users');
  });
});
