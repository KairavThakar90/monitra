// @vitest-environment jsdom
/**
 * A failed read is announced and retryable on every screen, and the quiet
 * retries behind it are bounded.
 *
 * Many screens never looked at `isError`: a failed load drew an empty table or a
 * row of zeros. The notice watches the one place every failure lands, counts
 * only what a mounted screen is showing, never calls a 401 a connection
 * problem, retries at 5 s / 15 s / 45 s and then stops, and does not mistake the
 * moment a retry is in flight for the end of the episode.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { store } from '../../store';
import { baseApi } from '../../store/api/baseApi';
import { retryFailedQueries } from '../../store/retryFailed';
import { LoadFailureNotice, AUTO_RETRY_DELAYS_MS, summariseFailures } from '../LoadFailureNotice';

const api = baseApi.injectEndpoints({
  overrideExisting: true,
  endpoints: (build) => ({
    noticeProbe: build.query<{ ok: boolean }, string>({ query: (id) => `https://api.test/notice/${id}` }),
  }),
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const entry = (status: string, error?: unknown) => ({ status, error });
const stateOf = (queries: Record<string, unknown>, subscribed: string[]) => ({
  api: {
    queries: queries as Record<string, { status?: string; error?: unknown }>,
    subscriptions: Object.fromEntries(subscribed.map((k) => [k, { s1: {} }])),
  },
});

describe('summariseFailures', () => {
  it('counts only failures a mounted screen is showing', () => {
    const state = stateOf(
      { a: entry('rejected', { status: 503 }), b: entry('rejected', { status: 503 }) },
      ['a'],
    );
    expect(summariseFailures(state)).toBe('1|network|0');
  });

  it('is silent when nothing failed', () => {
    expect(summariseFailures(stateOf({ a: entry('fulfilled') }, ['a']))).toBe('0|none|0');
  });

  it('leaves a session that has ended to the sign-in flow', () => {
    const state = stateOf({ a: entry('rejected', { status: 401 }) }, ['a']);
    expect(summariseFailures(state)).toBe('0|auth|0');
  });

  it('treats a 404 as a problem to report but not one to retry', () => {
    expect(summariseFailures(stateOf({ a: entry('rejected', { status: 404 }) }, ['a']))).toBe('1|client|0');
  });

  it('counts a retry in flight as pending, not as the end of the failure', () => {
    const state = stateOf({ a: entry('pending'), b: entry('rejected', { status: 'FETCH_ERROR' }) }, ['a', 'b']);
    expect(summariseFailures(state)).toBe('1|network|1');
  });
});

describe('retryFailedQueries (against a real store)', () => {
  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => vi.unstubAllGlobals());

  it('re-sends exactly the failed, mounted queries, once each', async () => {
    let healthy = false;
    const seen: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input));
      seen.push(url.pathname);
      if (url.pathname.endsWith('/good')) return json({ ok: true });
      return healthy ? json({ ok: true }) : json({}, 400);   // 400: not retried by the transport
    }));
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    const bad = store.dispatch(api.endpoints.noticeProbe.initiate('bad'));
    const good = store.dispatch(api.endpoints.noticeProbe.initiate('good'));
    await Promise.all([bad, good]);
    // RTK Query mirrors subscriptions into state on a short debounce.
    await new Promise((r) => setTimeout(r, 700));
    const before = seen.length;

    healthy = true;
    const retried = retryFailedQueries(store.dispatch, store.getState as never);
    await new Promise((r) => setTimeout(r, 50));

    expect(retried).toBe(1);
    expect(seen.slice(before)).toEqual(['/notice/bad']);
    expect(api.endpoints.noticeProbe.select('bad')(store.getState()).data).toEqual({ ok: true });
    bad.unsubscribe();
    good.unsubscribe();
  });

  it('does not retry a failure nobody is looking at any more', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({}, 400)));
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    const sub = store.dispatch(api.endpoints.noticeProbe.initiate('gone'));
    await sub;
    sub.unsubscribe();
    await new Promise((r) => setTimeout(r, 10));
    expect(retryFailedQueries(store.dispatch, store.getState as never)).toBe(0);
  });
});

describe('<LoadFailureNotice />', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.useFakeTimers();
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.clear();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    store.dispatch(baseApi.util.resetApiState());   // the app store is a singleton; leave it clean
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  let probeId = 0;
  const mount = async (fetchImpl: () => Promise<Response>) => {
    vi.stubGlobal('fetch', vi.fn(fetchImpl));
    // The notice reads the app's own store; give it one that has the probe query failing.
    store.dispatch(baseApi.util.resetApiState());
    probeId += 1;
    const sub = store.dispatch(api.endpoints.noticeProbe.initiate(`x${probeId}`, { forceRefetch: true }));
    // RTK's own re-sends back off for up to ~2.5 s, then the subscription is mirrored
    // into state on a 0.5 s debounce: the failure is settled by about 3 s.
    await act(async () => { await vi.advanceTimersByTimeAsync(3_500); });
    await act(async () => { root.render(<Provider store={store}><LoadFailureNotice /></Provider>); });
    return { store, sub };
  };

  it('appears for a connection failure with a Retry, and says so in plain words', async () => {
    const { sub } = await mount(async () => { throw new TypeError('Failed to fetch'); });
    const notice = container.querySelector('[data-testid="load-failure-notice"]')!;
    expect(notice).toBeTruthy();
    expect(notice.textContent).toMatch(/Connection temporarily unavailable/);
    expect(notice.querySelector('button')!.textContent).toBe('Retry');
    sub.unsubscribe();
  });

  it('retries quietly at 5 s, 15 s and 45 s and then stops for good', async () => {
    let requests = 0;
    const { sub } = await mount(async () => { requests += 1; throw new TypeError('Failed to fetch'); });
    const afterMount = requests;

    for (const delay of AUTO_RETRY_DELAYS_MS) {
      const before = requests;
      await act(async () => { await vi.advanceTimersByTimeAsync(delay + 2_500); });
      expect(requests, `a retry after ${delay} ms`).toBeGreaterThan(before);
    }
    const settled = requests;
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60_000); });
    expect(requests, 'it kept retrying after its three attempts').toBe(settled);
    expect(afterMount).toBeGreaterThan(0);
    // The notice stays, with the manual Retry.
    expect(container.querySelector('[data-testid="load-failure-notice"]')).toBeTruthy();
    sub.unsubscribe();
  });

  it('goes away by itself when the connection returns', async () => {
    let up = false;
    const { sub } = await mount(async () => { if (!up) throw new TypeError('Failed to fetch'); return json({ ok: true }); });
    expect(container.querySelector('[data-testid="load-failure-notice"]')).toBeTruthy();
    up = true;
    // The first quiet retry is due 5 s after the failure was seen; step the
    // clock in small increments (RTK's own backoff and its subscription
    // bookkeeping also run on timers) until it has been and gone.
    for (let waited = 0; waited < 20_000
      && container.querySelector('[data-testid="load-failure-notice"]'); waited += 500) {
      await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    }
    expect(container.querySelector('[data-testid="load-failure-notice"]')).toBeNull();
    sub.unsubscribe();
  });

  it('is not shown for a 401 (the sign-in flow owns that)', async () => {
    localStorage.removeItem('refreshToken');
    const { sub } = await mount(async () => json({}, 401));
    expect(container.querySelector('[data-testid="load-failure-notice"]')).toBeNull();
    sub.unsubscribe();
  });
});
