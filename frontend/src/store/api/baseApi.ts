import { createApi, fetchBaseQuery } from '@reduxjs/toolkit/query/react';
import type { FetchArgs, FetchBaseQueryError } from '@reduxjs/toolkit/query';
import { createAction } from '@reduxjs/toolkit';
import { refreshSessionAPI } from '../../api/auth';
import { clearSessionStorage, ensureSessionExpiry, storeSessionTokens } from '../../auth/session';

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
const rawBaseQuery = fetchBaseQuery({
  baseUrl: '',
  prepareHeaders: (headers) => {
    const token = localStorage.getItem('accessToken');
    if (token) headers.set('Authorization', `Bearer ${token}`);
    return headers;
  },
});

let refreshPromise: Promise<import('../../api/auth').TokenPair> | null = null;

const baseQueryWithRefresh = async (args: string | FetchArgs, api: any, extraOptions: any) => {
  const expiresAt = ensureSessionExpiry();
  if (expiresAt !== null && expiresAt <= Date.now()) {
    clearSessionStorage();
    window.dispatchEvent(new Event('auth:session-expired'));
    return { error: { status: 401, data: 'Session expired' } as FetchBaseQueryError };
  }

  let result = await rawBaseQuery(args, api, extraOptions);
  if (result.error?.status !== 401) return result;

  const refreshToken = localStorage.getItem('refreshToken');
  if (!refreshToken) return result;

  refreshPromise ??= refreshSessionAPI(refreshToken).finally(() => {
    refreshPromise = null;
  });

  try {
    const session = await refreshPromise;
    storeSessionTokens(session, true);
    result = await rawBaseQuery(args, api, extraOptions);
  } catch {
    clearSessionStorage();
    window.dispatchEvent(new Event('auth:session-expired'));
  }

  return result;
};

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
  baseQuery: baseQueryWithRefresh,
  extractRehydrationInfo(action, { reducerPath }) {
    if (rehydrateApiCache.match(action)) {
      return action.payload?.[reducerPath] as any;
    }
  },
  tagTypes: ['Member', 'Project', 'Task', 'Team', 'TimeTracking', 'ManualTimeEntry', 'Feedback', 'System'],
  endpoints: () => ({}),
});
