import { isLoginDisabledBody, isLoginDisabledError, markLoginDisabled } from '../../auth/loginAccess';
import { createApi, fetchBaseQuery, retry } from '@reduxjs/toolkit/query/react';
import type { FetchArgs, FetchBaseQueryError } from '@reduxjs/toolkit/query';
import { createAction } from '@reduxjs/toolkit';
import { SessionEndedError } from '../../api/auth';
import { renewSession } from '../../auth/renewSession';
import { clearSessionStorage, ensureSessionExpiry } from '../../auth/session';

/**
 * Dispatched once at start-up with the cache we persisted during the previous
 * visit. RTK Query picks it up through `extractRehydrationInfo` below, which is
 * what lets a page paint real rows on the very first frame after a refresh
 * instead of a spinner.
 */
export const rehydrateApiCache = createAction<Record<string, unknown> | undefined>('api/rehydrate');

/**
 * One API slice for the whole client. Every domain file (`membersApi`,
 * `projectsApi`, …) injects its endpoints into this, so there is a single
 * cache, a single middleware and a single place that attaches the auth header.
 * A single cache also means a tag invalidated by one domain is seen by all the
 * others — updating a project can refresh the Teams screens.
 */
/**
 * Longest a data request may take before it is abandoned (`TIMEOUT_ERROR`).
 * There was none: a request the network swallowed left its page loading for
 * ever, and -- because every 401 waits on the same refresh -- one hung refresh
 * held every query that had met one. Generous on purpose: this is the ceiling
 * for the heaviest report, not a target.
 */
export const REQUEST_TIMEOUT_MS = 45_000;

const newRequestId = (): string =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

const rawBaseQuery = fetchBaseQuery({
  baseUrl: '',
  timeout: REQUEST_TIMEOUT_MS,
  prepareHeaders: (headers) => {
    const token = localStorage.getItem('accessToken');
    if (token) headers.set('Authorization', `Bearer ${token}`);
    // The backend returns and logs this id, so "it failed at 10:32" can be
    // answered with the request itself. No token, no body, no user content.
    headers.set('X-Request-ID', newRequestId());
    headers.set('X-Client-Platform', 'web');
    return headers;
  },
});

const endSession = () => {
  clearSessionStorage();
  window.dispatchEvent(new Event('auth:session-expired'));
};

const baseQueryWithRefresh = async (args: string | FetchArgs, api: any, extraOptions: any) => {
  const expiresAt = ensureSessionExpiry();
  if (expiresAt !== null && expiresAt <= Date.now()) {
    endSession();
    return { error: { status: 401, data: 'Session expired' } as FetchBaseQueryError };
  }

  const tokenUsed = localStorage.getItem('accessToken');
  let result = await rawBaseQuery(args, api, extraOptions);
  if (result.error?.status !== 401) return result;

  // An administrator excluded this account: a refresh would be refused the
  // same way. End the session now and let the sign-in screen say why.
  if (isLoginDisabledBody(result.error.data)) {
    markLoginDisabled();
    clearSessionStorage();
    window.dispatchEvent(new Event('auth:session-expired'));
    return result;
  }

  // 401 with nothing to renew it from: the session is over. It used to be
  // returned as a plain error, leaving the app "signed in" while every request
  // failed until the user signed out by hand.
  if (!localStorage.getItem('refreshToken')) {
    endSession();
    return result;
  }

  try {
    await renewSession(tokenUsed);
  } catch (err) {
    if (isLoginDisabledError(err)) markLoginDisabled();
    if (isLoginDisabledError(err) || err instanceof SessionEndedError) {
      // The server's own verdict: this session is over.
      endSession();
      return result;
    }
    // Anything else -- the network, a timeout, a 5xx/429, something unexpected --
    // means the session could not be *asked*. That says nothing about whether
    // it is over, so it is not ended: the request fails as a network error,
    // which is retried and which the screen offers to retry, with the tokens
    // still there for it.
    return { error: { status: 'FETCH_ERROR', error: 'The session could not be renewed right now.' } as FetchBaseQueryError };
  }

  // One re-send with the renewed (or already-renewed-by-someone-else) token.
  return rawBaseQuery(args, api, extraOptions);
};

const isRead = (args: string | FetchArgs): boolean =>
  typeof args === 'string' || (args.method ?? 'GET').toUpperCase() === 'GET';

/** Failures that say "nothing happened, ask again" -- never a timeout (a slow
 * server is made slower by being asked again), a 4xx, or a refusal. */
const isTransient = (error: FetchBaseQueryError | undefined): boolean =>
  !!error && (error.status === 'FETCH_ERROR' || error.status === 502 || error.status === 503 || error.status === 504);

/** Two re-sends at most, reads only, after RTK's jittered exponential backoff
 * (~0.1-0.4 s, then ~0.2-0.8 s). Writes are never re-sent here. */
export const MAX_READ_RETRIES = 2;

// (RTK types `maxRetries` and `retryCondition` as alternatives, so the bound is
// part of the condition.)
const baseQueryWithRetry = retry(baseQueryWithRefresh, {
  retryCondition: (error, args, { attempt }) =>
    attempt <= MAX_READ_RETRIES && isTransient(error as FetchBaseQueryError) && isRead(args as string | FetchArgs),
});

export const baseApi = createApi({
  reducerPath: 'api',
  // Keep an unused endpoint's data for 10 minutes so moving between pages and
  // coming back is instant rather than a fresh round trip.
  keepUnusedDataFor: 600,
  // Data already in cache renders immediately; anything older than 60s is
  // revalidated in the background while the stale rows stay on screen.
  refetchOnMountOrArgChange: 60,
  // Both need `setupListeners(store.dispatch)` (store/index.ts) to take
  // effect. Focus is the moment a person comes back from the desktop app to
  // look at the dashboard: anything older than the staleness window above
  // is re-read then, so the day's total is current without a page reload.
  refetchOnFocus: true,
  refetchOnReconnect: true,
  baseQuery: baseQueryWithRetry,
  extractRehydrationInfo(action, { reducerPath }) {
    if (rehydrateApiCache.match(action)) {
      return action.payload?.[reducerPath] as any;
    }
  },
  tagTypes: ['Member', 'Project', 'Task', 'Team', 'TimeTracking', 'TimeEntry', 'ManualTimeEntry', 'Feedback', 'System', 'Client', 'ClientProject', 'ActivityLog'],
  endpoints: () => ({}),
});
