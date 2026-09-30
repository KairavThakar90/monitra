// @vitest-environment jsdom
/**
 * The Members page's per-member log modal: one member, today (IST), showing
 * the time, what was done, the IP address and the user id.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import { istTodayISO } from '../../../utils/duration';
import { MemberLogModal } from '../MemberLogModal';

describe('MemberLogModal', () => {
  let container: HTMLDivElement;
  let root: Root;
  let requests: string[];
  let members: unknown[];
  const onClose = vi.fn();

  const json = (body: unknown) =>
    new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });

  const render = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(
        <Provider store={store}>
          <MemberLogModal memberId={11} memberName="Alice Example" onClose={onClose} />
        </Provider>,
      );
    });
    for (let i = 0; i < 5; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    requests = [];
    onClose.mockClear();
    members = [{
      user_id: 11, name: 'Alice Example', email: null, designation: null, role_name: 'employee',
      entry_count: 2, last_activity_at: '2026-09-30T06:30:00Z',
      entries: [
        { id: 2, module: 'timer', action: 'timer_started', description: 'Started the timer on Apollo › Guidance',
          source: 'desktop', client_version: '1.3.0', project_id: 1, project_name: 'Apollo', task_id: 2,
          task_name: 'Guidance', entity_id: 9, ip_address: '203.0.113.9', created_at: '2026-09-30T06:30:00Z' },
        { id: 1, module: 'auth', action: 'login', description: 'Signed in', source: 'web', client_version: null,
          project_id: null, project_name: null, task_id: null, task_name: null, entity_id: null,
          ip_address: null, created_at: '2026-09-30T04:00:00Z' },
      ],
    }];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      requests.push(url);
      return json({ start_date: '', end_date: '', total: 2, truncated: false, modules: [], members });
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

  it('asks for exactly this member and today', async () => {
    await render();
    const today = istTodayISO();
    const url = new URL(requests[0], 'http://localhost');
    expect(url.searchParams.get('member_id')).toBe('11');
    expect(url.searchParams.get('start')).toBe(today);
    expect(url.searchParams.get('end')).toBe(today);
  });

  it('shows time, track, IP and user id for each action', async () => {
    await render();
    const headers = Array.from(container.querySelectorAll('th')).map((th) => th.textContent);
    expect(headers).toEqual(['Time', 'Track', 'IP', 'User ID']);
    const rows = Array.from(container.querySelectorAll('tbody tr'));
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('Started timer');
    expect(rows[0].textContent).toContain('Started the timer on Apollo › Guidance');
    expect(rows[0].textContent).toContain('203.0.113.9');
    expect(rows[0].textContent).toContain('11');
    // An action with no recorded address shows a dash, never an invented one.
    expect(rows[1].textContent).toContain('—');
  });

  it('says so when the member did nothing today', async () => {
    members = [];
    await render();
    expect(container.textContent).toContain('No activity recorded for Alice Example today.');
  });

  it('closes from the button and from Escape', async () => {
    await render();
    await act(async () => {
      (container.querySelector('button[aria-label="Close"]') as HTMLButtonElement).click();
    });
    expect(onClose).toHaveBeenCalledTimes(1);
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
