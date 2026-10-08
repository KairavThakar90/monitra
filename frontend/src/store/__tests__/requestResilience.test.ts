// @vitest-environment jsdom
/**
 * A transient failure must not become a failed screen, and a failed refresh
 * must not end a session that is not over.
 *
 * Pins the web half of the intermittent "Your dashboard could not be loaded"
 * reports: no request had a timeout, nothing re-sent a read that failed the way
 * a dropped connection fails, refresh tokens (single-use, rotated) were spent
 * twice by tabs and queries racing each other and the loser wiped the winner's
 * tokens, and any failure of the refresh -- a dropped packet included -- signed
 * the user out.
 */
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi, MAX_READ_RETRIES } from '../api/baseApi';
import { describeQueryError } from '../../api/queryError';

const api = baseApi.injectEndpoints({
  overrideExisting: true,
  endpoints: (build) => ({
    probeRead: build.query<{ ok: boolean }, string>({ query: (id) => `https://api.test/probe/${id}` }),
    probeWrite: build.mutation<{ ok: boolean }, string>({
      query: (id) => ({ url: `https://api.test/write/${id}`, method: 'POST', body: {} }),
    }),
  }),
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const SESSION = {
  access_token: 'access-2', refresh_token: 'refresh-2', token_type: 'bearer',
  session_created_at: new Date().toISOString(),
  session_expires_at: new Date(Date.now() + 86_400_000).toISOString(),
  user: { id: 1, email: 'a@b.test', role_name: 'employee' },
};

type Handler = (request: Request) => Response | Promise<Response>;

describe('web request resilience', () => {
  let calls: Request[];
  let handler: Handler;
  let expired: number;
  const onExpired = () => { expired += 1; };

  const makeStore = () => configureStore({
    reducer: { [baseApi.reducerPath]: baseApi.reducer },
    middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
  });

  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('accessToken', 'access-1');
    localStorage.setItem('refreshToken', 'refresh-1');
    localStorage.setItem('monitra.session.expiresAt', String(Date.now() + 86_400_000));
    calls = [];
    expired = 0;
    window.addEventListener('auth:session-expired', onExpired);
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      calls.push(request);
      return handler(request);
    }));
  });

  afterEach(() => {
    window.removeEventListener('auth:session-expired', onExpired);
    vi.unstubAllGlobals();
  });

  const paths = () => calls.map((c) => new URL(c.url).pathname);
  const read = (store: ReturnType<typeof makeStore>, id = '1') =>
    store.dispatch(api.endpoints.probeRead.initiate(id, { forceRefetch: true }));

  // ── identity of a request ──────────────────────────────────────────────────

  it('sends a request id and the platform on every request, and never puts a token in them', async () => {
    handler = () => json({ ok: true });
    const store = makeStore();
    await read(store);
    const headers = calls[0].headers;
    expect(headers.get('x-request-id')).toMatch(/^[A-Za-z0-9._:-]{8,64}$/);
    expect(headers.get('x-client-platform')).toBe('web');
    expect(headers.get('authorization')).toBe('Bearer access-1');
    expect(headers.get('x-request-id')).not.toContain('access-1');
  });

  // ── the retry ──────────────────────────────────────────────────────────────

  it('re-sends a read that fails with a gateway error, and succeeds', async () => {
    let n = 0;
    handler = () => (++n === 1 ? json({}, 503) : json({ ok: true }));
    const result = await read(makeStore());
    expect(result.data).toEqual({ ok: true });
    expect(calls).toHaveLength(2);
  });

  it('re-sends a read that never got an answer (a network failure)', async () => {
    let n = 0;
    handler = () => { if (++n === 1) throw new TypeError('Failed to fetch'); return json({ ok: true }); };
    const result = await read(makeStore());
    expect(result.data).toEqual({ ok: true });
    expect(calls).toHaveLength(2);
  });

  it('gives up after two re-sends and reports the failure', async () => {
    handler = () => json({}, 502);
    const result = await read(makeStore());
    expect(calls).toHaveLength(1 + MAX_READ_RETRIES);
    expect(result.isError).toBe(true);
    expect(describeQueryError(result.error).kind).toBe('server');
  });

  it.each([400, 404, 409, 422, 500])('never re-sends a read that got a definitive %s', async (status) => {
    handler = () => json({ detail: 'no' }, status);
    await read(makeStore());
    expect(calls).toHaveLength(1);
  });

  it('never re-sends a write, even on a gateway error', async () => {
    handler = () => json({}, 503);
    await makeStore().dispatch(api.endpoints.probeWrite.initiate('1'));
    expect(calls).toHaveLength(1);
  });

  // ── refresh ────────────────────────────────────────────────────────────────

  it('renews an expired token once and re-sends the request with the new one', async () => {
    handler = (request) => {
      const path = new URL(request.url).pathname;
      if (path.endsWith('/auth/refresh')) return json(SESSION);
      return request.headers.get('authorization') === 'Bearer access-2' ? json({ ok: true }) : json({}, 401);
    };
    const result = await read(makeStore());
    expect(result.data).toEqual({ ok: true });
    expect(paths().filter((p) => p.endsWith('/auth/refresh'))).toHaveLength(1);
    expect(localStorage.getItem('accessToken')).toBe('access-2');
    expect(localStorage.getItem('refreshToken')).toBe('refresh-2');
    expect(expired).toBe(0);
  });

  it('shares ONE refresh between queries that meet a 401 together', async () => {
    handler = async (request) => {
      const path = new URL(request.url).pathname;
      if (path.endsWith('/auth/refresh')) {
        await new Promise((r) => setTimeout(r, 30));
        return json(SESSION);
      }
      return request.headers.get('authorization') === 'Bearer access-2' ? json({ ok: true }) : json({}, 401);
    };
    const store = makeStore();
    const results = await Promise.all(['a', 'b', 'c', 'd'].map((id) => read(store, id)));
    expect(results.every((r) => r.data?.ok)).toBe(true);
    expect(paths().filter((p) => p.endsWith('/auth/refresh'))).toHaveLength(1);
    expect(expired).toBe(0);
  });

  it('does not refresh at all when another tab already replaced the token', async () => {
    handler = (request) => {
      if (new URL(request.url).pathname.endsWith('/auth/refresh')) return json(SESSION);
      if (request.headers.get('authorization') === 'Bearer access-1') {
        // while this request was failing, another tab renewed the session
        localStorage.setItem('accessToken', 'access-other-tab');
        localStorage.setItem('refreshToken', 'refresh-other-tab');
        return json({}, 401);
      }
      return json({ ok: true });
    };
    const result = await read(makeStore());
    expect(result.data).toEqual({ ok: true });
    expect(paths().some((p) => p.endsWith('/auth/refresh'))).toBe(false);
    expect(localStorage.getItem('refreshToken')).toBe('refresh-other-tab');
    expect(expired).toBe(0);
  });

  it('a refresh refused because another tab spent the token is not the end of the session', async () => {
    handler = (request) => {
      const path = new URL(request.url).pathname;
      if (path.endsWith('/auth/refresh')) {
        // the other tab's refresh won the race and stored its tokens
        localStorage.setItem('accessToken', 'access-other-tab');
        localStorage.setItem('refreshToken', 'refresh-other-tab');
        return json({ detail: 'Session ended' }, 401);
      }
      return request.headers.get('authorization') === 'Bearer access-other-tab' ? json({ ok: true }) : json({}, 401);
    };
    const result = await read(makeStore());
    expect(result.data).toEqual({ ok: true });
    expect(localStorage.getItem('refreshToken')).toBe('refresh-other-tab');
    expect(expired).toBe(0);
  });

  it('ends the session when the server itself says it is over (refresh 401)', async () => {
    handler = (request) =>
      new URL(request.url).pathname.endsWith('/auth/refresh') ? json({ detail: 'Session ended' }, 401) : json({}, 401);
    await read(makeStore());
    expect(expired).toBe(1);
    expect(localStorage.getItem('accessToken')).toBeNull();
    expect(localStorage.getItem('refreshToken')).toBeNull();
  });

  it.each([500, 502, 503, 429])('does NOT end the session when the refresh answers %s', async (status) => {
    handler = (request) =>
      new URL(request.url).pathname.endsWith('/auth/refresh') ? json({}, status) : json({}, 401);
    const result = await read(makeStore());
    expect(expired).toBe(0);
    expect(localStorage.getItem('refreshToken')).toBe('refresh-1');
    expect(localStorage.getItem('accessToken')).toBe('access-1');
    // reported as a connection problem -- not as "your session has ended"
    expect(describeQueryError(result.error).kind).toBe('network');
  });

  it('does NOT end the session when the refresh never reaches the server', async () => {
    handler = (request) => {
      if (new URL(request.url).pathname.endsWith('/auth/refresh')) throw new TypeError('Failed to fetch');
      return json({}, 401);
    };
    await read(makeStore());
    expect(expired).toBe(0);
    expect(localStorage.getItem('refreshToken')).toBe('refresh-1');
  });

  it('a 401 with nothing to renew it from ends the session instead of leaving it half-signed-in', async () => {
    localStorage.removeItem('refreshToken');
    handler = () => json({}, 401);
    await read(makeStore());
    expect(expired).toBe(1);
  });

  it('never refreshes more than once per request: a 401 after a renewed token is reported, not looped', async () => {
    handler = (request) =>
      new URL(request.url).pathname.endsWith('/auth/refresh') ? json(SESSION) : json({}, 401);
    const result = await read(makeStore());
    expect(paths().filter((p) => p.endsWith('/auth/refresh'))).toHaveLength(1);
    expect(describeQueryError(result.error).kind).toBe('auth');
  });
});

describe('describeQueryError', () => {
  it.each([
    [{ status: 401 }, 'auth', false],
    [{ status: 403 }, 'auth', false],
    [{ status: 'FETCH_ERROR' }, 'network', true],
    [{ status: 'TIMEOUT_ERROR' }, 'timeout', true],
    [{ status: 502 }, 'server', true],
    [{ status: 503 }, 'server', true],
    [{ status: 504 }, 'server', true],
    [{ status: 429 }, 'server', true],
    [{ status: 500 }, 'server', true],
    [{ status: 404 }, 'client', false],
    [{ status: 422 }, 'client', false],
    [{ status: 'PARSING_ERROR' }, 'client', false],
    [undefined, 'network', true],
  ])('%j is %s (transient=%s)', (error, kind, transient) => {
    const info = describeQueryError(error);
    expect(info.kind).toBe(kind);
    expect(info.transient).toBe(transient);
  });

  it('never puts a technical string in the message', () => {
    for (const status of [401, 404, 500, 'FETCH_ERROR', 'TIMEOUT_ERROR']) {
      expect(describeQueryError({ status }).message).not.toMatch(/FETCH_ERROR|TIMEOUT_ERROR|HTTP|\b\d{3}\b/);
    }
  });
});
