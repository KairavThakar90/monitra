// @vitest-environment jsdom
/**
 * My Time Tracking: what an opened day shows.
 *
 * An opened day lists that day's projects and to-dos with their totals. It used to add a second
 * block beneath them -- "Recorded time entries", one row per session, each with a "Change Project"
 * button -- which was removed at the owner's request. Moving or splitting a session is done from
 * the Time Entries page. Pinned here:
 *
 * - the opened day still shows its project / to-do breakdown and the day's total;
 * - it shows no "Recorded time entries" heading and no "Change Project" button;
 * - it no longer asks the backend for the day's raw sessions (the request existed only for that
 *   block), so opening a day costs one request fewer.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { MemoryRouter } from 'react-router-dom';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

vi.mock('../MemberShell', () => ({
  MemberShell: ({ title, actions, children }: { title: string; actions?: React.ReactNode; children: React.ReactNode }) => (
    <div><h1>{title}</h1>{actions}{children}</div>
  ),
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser: { id: 54, name: 'Asha', role_name: 'employee', permissions: {} } }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { MemberTimeTracking } from '../MemberTimeTracking';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const DAY = '2026-10-30';
const status = { id: 1, name: 'Todo', color: '#94A3B8' };

describe('MemberTimeTracking: an opened day', () => {
  let container: HTMLDivElement;
  let root: Root;
  let paths: string[];

  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    paths = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), 'http://localhost');
      paths.push(url.pathname);
      if (url.pathname.endsWith('/time-tracking')) {
        return json({
          items: [{
            employee_id: 54, name: 'Asha', email: 'a@x.test', designation: '', date: DAY,
            start_time: '2026-10-30T04:40:00Z', end_time: '2026-10-30T07:12:00Z',
            total_seconds: 5452, total_hours: '', total_time: '01:30:52',
          }],
          pagination: { page: 1, limit: 100, total: 1, total_pages: 1 },
        });
      }
      if (/\/time-tracking\/54$/.test(url.pathname)) {
        return json({
          employee: { id: 54, name: 'Asha', email: '', designation: '', role: 'employee' },
          start_date: DAY, end_date: DAY,
          summary: { start_time: null, end_time: null, total_seconds: 5452, total_hours: '', total_time: '01:30:52' },
          projects: [{
            id: 2, name: 'V2', status, total_seconds: 1523, total_hours: '', total_time: '00:25:23',
            tasks: [{ id: 3, name: 'Project Setup / Understanding', status, total_seconds: 1523, total_hours: '', total_time: '00:25:23' }],
          }],
        });
      }
      if (url.pathname.includes('/time-entries')) {
        return json([{ id: 1, project_id: 2, task_id: 3, is_running: false, total_seconds: 1523, elapsed_time: '00:25:23',
          start_time: '2026-10-30T04:40:00Z', end_time: '2026-10-30T05:05:00Z' }]);
      }
      if (url.pathname.includes('/projects')) return json({ items: [], total: 0 });
      return json({ items: [], pagination: { page: 1, limit: 100, total: 0, total_pages: 1 } });
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(<Provider store={store}><MemoryRouter><MemberTimeTracking /></MemoryRouter></Provider>);
    });
    await settle();
    // Open the day.
    const row = Array.from(container.querySelectorAll('tbody tr')).find((tr) => tr.textContent?.includes('30 Oct'));
    expect(row, 'the day row').toBeTruthy();
    await act(async () => { (row as HTMLElement).click(); });
    await settle();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('still shows the day\'s project and to-do breakdown with its total', () => {
    const text = container.textContent!;
    expect(text).toContain('V2');
    expect(text).toContain('Project Setup / Understanding');
    expect(text).toContain('00:25:23');
    expect(text).toContain('01:30:52');
  });

  it('no longer shows the Recorded time entries block or any Change Project button', () => {
    expect(container.textContent).not.toContain('Recorded time entries');
    expect(container.textContent).not.toContain('Change Project');
    expect(Array.from(container.querySelectorAll('button')).some((b) => b.textContent?.trim() === 'Change Project')).toBe(false);
  });

  it('does not ask for the day\'s raw sessions any more', () => {
    expect(paths.filter((p) => p.includes('/time-entries'))).toEqual([]);
  });
});
