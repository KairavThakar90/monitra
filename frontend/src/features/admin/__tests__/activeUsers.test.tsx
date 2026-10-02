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
    const first = rows()[0].textContent!;
    expect(first).toContain('Asha Patel');
    expect(first).toContain('Developer');
    expect(first).toContain('Build login screen');
    expect(first).toContain('Alpha Portal');
    expect(first).toContain('10:00 am');
    expect(first).toContain('01:02:05');
    const second = rows()[1].textContent!;
    expect(second).toContain('Ravi Shah');
    expect(second).toContain('Fix crash on launch');
    expect(second).toContain('Beta Mobile');
    expect(second).toContain('02:32:10');
    expect(container.querySelector('[data-testid="active-users-count"]')!.textContent).toContain('2 members active');
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
