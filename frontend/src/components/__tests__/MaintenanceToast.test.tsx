// @vitest-environment jsdom
/**
 * The maintenance toast, rendered.
 *
 * Against a real Redux store and the real RTK Query slice, with `fetch`
 * answered by the test: the card appears when the polled flag turns true,
 * carries the logo, the agreed words and the OFFLINE pill, sits in a corner
 * without a backdrop or a dialog role, leaves a control underneath it fully
 * clickable, is not duplicated by a repeated `true`, disappears on `false`,
 * and is re-asked for on the polling interval and on the browser's `online`
 * event -- so an administrator's switch reaches an open tab without a reload.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { setupListeners } from '@reduxjs/toolkit/query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../store/api/baseApi';
import { systemApi, type MaintenanceStatus } from '../../store/api/systemApi';
import { MAINTENANCE_COPY, MAINTENANCE_POLL_INTERVAL_MS } from '../../features/system/maintenance';
import { MaintenanceNotice } from '../MaintenanceToast';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const status = (maintenance_mode: boolean): MaintenanceStatus => ({
  maintenance_mode,
  updated_at: null,
  server_time: '2026-09-16T10:00:00+00:00',
});

const jsonResponse = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });

const makeStore = () =>
  configureStore({
    reducer: { [baseApi.reducerPath]: baseApi.reducer },
    middleware: (gdm) => gdm().concat(baseApi.middleware),
  });

/** Let RTK Query's thunks and React's effects settle. Fake-timer aware. */
const flush = async () => {
  for (let i = 0; i < 3; i += 1) {
    await act(async () => {
      if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(1);
      else await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
};

describe('MaintenanceNotice', () => {
  let container: HTMLDivElement;
  let root: Root;
  let store: ReturnType<typeof makeStore>;
  let answer: boolean;
  let fetchMock: ReturnType<typeof vi.fn>;
  let clicks = 0;

  const toasts = () => container.querySelectorAll('[data-testid="maintenance-toast"]');

  const mount = async (enabled: boolean) => {
    await act(async () => {
      root.render(
        <Provider store={store}>
          <div>
            <button type="button" id="under" onClick={() => { clicks += 1; }}>Start timer</button>
            <MaintenanceNotice enabled={enabled} />
          </div>
        </Provider>,
      );
    });
    await flush();
  };

  const flip = async (value: boolean) => {
    // `upsertQueryData` is an async thunk: await it, as a real fulfilment
    // would be awaited, before looking at what rendered.
    await act(async () => {
      await store.dispatch(systemApi.util.upsertQueryData('getMaintenanceStatus', undefined, status(value)));
    });
    await flush();
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'token');
    localStorage.setItem('refreshToken', 'refresh');
    answer = false;
    clicks = 0;
    fetchMock = vi.fn(async () => jsonResponse(status(answer)));
    vi.stubGlobal('fetch', fetchMock);
    store = makeStore();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => { root.unmount(); });
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    localStorage.clear();
  });

  it('shows nothing while maintenance is off', async () => {
    await mount(true);
    expect(fetchMock).toHaveBeenCalled();
    expect(toasts().length).toBe(0);
  });

  it('false -> true shows one toast with the logo, the words and the OFFLINE pill', async () => {
    await mount(true);
    await flip(true);

    expect(toasts().length).toBe(1);
    const toast = toasts()[0];
    expect(toast.getAttribute('role')).toBe('status');
    const logo = toast.querySelector('img');
    expect(logo?.getAttribute('alt')).toBe('Monitra');
    expect(logo?.getAttribute('src')).toContain('logo');
    expect(toast.textContent).toContain(MAINTENANCE_COPY.title);
    expect(toast.textContent).toContain(MAINTENANCE_COPY.body);
    const pill = toast.querySelector('[data-testid="maintenance-status"]');
    expect(pill?.textContent?.trim()).toBe('OFFLINE');
  });

  it('never blocks the application underneath it', async () => {
    await mount(true);
    await flip(true);

    // No overlay, no dialog, no acknowledgement.
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(container.querySelector('[aria-modal="true"]')).toBeNull();
    expect(toasts()[0].querySelector('button')).toBeNull();
    const shell = toasts()[0] as HTMLElement;
    expect(shell.className).toContain('fixed');
    expect(shell.className).toContain('pointer-events-none');
    expect(shell.className).not.toContain('inset-0');

    // The control underneath still works.
    const under = container.querySelector<HTMLButtonElement>('#under')!;
    expect(under.disabled).toBe(false);
    await act(async () => { under.click(); });
    expect(clicks).toBe(1);
  });

  it('true -> true adds nothing; true -> false removes it', async () => {
    await mount(true);
    await flip(true);
    await flip(true);
    await flip(true);
    expect(toasts().length).toBe(1);

    await flip(false);
    expect(toasts().length).toBe(0);

    await flip(false);
    expect(toasts().length).toBe(0);
  });

  it('a second maintenance window shows the toast again', async () => {
    await mount(true);
    await flip(true);
    await flip(false);
    await flip(true);
    expect(toasts().length).toBe(1);
  });

  it('signing out clears it and the next session starts from unknown', async () => {
    await mount(true);
    await flip(true);
    expect(toasts().length).toBe(1);

    await mount(false);
    expect(toasts().length).toBe(0);
  });

  it('polls on the interval, so a change reaches an open tab without a reload', async () => {
    vi.useFakeTimers();
    await mount(true);
    const before = fetchMock.mock.calls.length;
    expect(before).toBeGreaterThan(0);
    expect(toasts().length).toBe(0);

    answer = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(MAINTENANCE_POLL_INTERVAL_MS + 100); });
    await flush();

    expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
    expect(toasts().length).toBe(1);
  });

  it('re-asks when the browser comes back online', async () => {
    // The app binds RTK's window listeners to its own store once, in
    // `store/index.ts`, and that module-level registration is already taken
    // by the time this file's imports resolve. Bind the same `online`
    // handler to this test's store, so the event reaches the slice under
    // test with the slice's own `refetchOnReconnect` configuration.
    const unsubscribe = setupListeners(store.dispatch, (dispatch, actions) => {
      const handleOnline = () => dispatch(actions.onOnline());
      window.addEventListener('online', handleOnline);
      return () => window.removeEventListener('online', handleOnline);
    });
    try {
      await mount(true);
      const before = fetchMock.mock.calls.length;
      expect(before).toBeGreaterThan(0);

      answer = true;
      await act(async () => { window.dispatchEvent(new Event('online')); });
      await flush();

      expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
      expect(toasts().length).toBe(1);
    } finally {
      unsubscribe();
    }
  });
});
