import type { Store } from '@reduxjs/toolkit';
import { rehydrateApiCache } from './api/baseApi';

const STORAGE_KEY = 'monitra.api.cache.v1';
/** Anything older than this is dropped rather than shown. */
const MAX_AGE_MS = 24 * 60 * 60 * 1000;
/** Trailing write window, so a burst of cache updates costs one serialize. */
const WRITE_THROTTLE_MS = 1500;

/**
 * Endpoints whose data is a *live reading of tracked time* and is therefore
 * never persisted or restored.
 *
 * Restoring a cache paints old rows on the first frame and corrects them a
 * moment later. That is a good trade for a project list. It is a wrong one for
 * a running entry: the server measures it as `now - start_time`, so a figure
 * saved before the machine slept (or even yesterday) reads as hours of work
 * that no longer exist -- the desktop ends a session retroactively once it
 * wakes -- and Active Users would list members as "working now" who stopped
 * long ago. These pages start from a loading state and show only what the
 * server says now.
 */
const LIVE_ENDPOINTS: ReadonlySet<string> = new Set([
  'getActiveTimeTracking',
  'getTimeTracking',
  'getTimeTrackingDetails',
]);

const isLiveEntry = (entry: unknown): boolean =>
  isRecord(entry) && typeof entry.endpointName === 'string' && LIVE_ENDPOINTS.has(entry.endpointName);

type ApiCacheSnapshot = {
  /** Identifies the session the cache belongs to — see `sessionKey`. */
  owner: string;
  savedAt: number;
  api: {
    queries: Record<string, any>;
    /**
     * Always present and always empty. RTK Query's mutation reducer reads
     * `mutations` unconditionally during rehydration, so omitting it throws.
     * In-flight mutations are never worth restoring anyway.
     */
    mutations: Record<string, never>;
    provided: Record<string, any>;
  };
};

const isRecord = (value: unknown): value is Record<string, any> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

/**
 * Cached responses are per-user data, so they are only ever reused by the exact
 * session that wrote them. Signing out (or signing in as somebody else) changes
 * the key and the old snapshot is ignored and overwritten.
 */
const sessionKey = (): string => localStorage.getItem('accessToken') || 'anonymous';

const safeGet = (): ApiCacheSnapshot | null => {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as ApiCacheSnapshot) : null;
  } catch {
    return null;
  }
};

export const clearPersistedApiCache = () => {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable — nothing to clear */
  }
};

/**
 * Reads the previous visit's cache. Returns undefined when there is nothing
 * usable, in which case the app simply starts cold as before.
 */
export const loadPersistedApiCache = (): Record<string, unknown> | undefined => {
  const snapshot = safeGet();
  if (!snapshot || snapshot.owner !== sessionKey()) {
    // Belongs to a different session — never show it.
    if (snapshot) clearPersistedApiCache();
    return undefined;
  }
  if (!snapshot.savedAt || Date.now() - snapshot.savedAt > MAX_AGE_MS) {
    clearPersistedApiCache();
    return undefined;
  }

  // A cache written by an older build or interrupted storage update must not
  // reach RTK Query's reducer, which expects all three sections to be objects.
  if (!isRecord(snapshot.api) || !isRecord(snapshot.api.queries) || !isRecord(snapshot.api.mutations) || !isRecord(snapshot.api.provided)) {
    clearPersistedApiCache();
    return undefined;
  }

  // Restored rows paint immediately, but they came from a previous page load and
  // we have no idea what changed since. Backdating the fetch timestamp makes
  // RTK Query treat every one of them as stale, so each screen the user opens
  // revalidates in the background behind the data it is already showing.
  const queries: Record<string, any> = {};
  for (const [key, entry] of Object.entries(snapshot.api.queries || {})) {
    // A snapshot written by an earlier build may still hold live tracked-time
    // readings; they are dropped here rather than shown.
    if (isLiveEntry(entry)) continue;
    queries[key] = { ...entry, fulfilledTimeStamp: 0 };
  }

  // The tag index must not point at queries that were just dropped.
  return { api: { ...snapshot.api, queries, provided: pickProvidedFor(snapshot.api.provided, new Set(Object.keys(queries))) } };
};

/** Keeps only settled, non-error, non-expired entries. */
const pickReusableQueries = (queries: Record<string, any>) => {
  const kept: Record<string, any> = {};
  const now = Date.now();
  for (const [key, entry] of Object.entries(queries || {})) {
    if (!entry || entry.status !== 'fulfilled' || entry.error) continue;
    if (entry.data === undefined) continue;
    if (isLiveEntry(entry)) continue;
    if (entry.fulfilledTimeStamp && now - entry.fulfilledTimeStamp > MAX_AGE_MS) continue;
    kept[key] = {
      ...entry,
      // A persisted entry is never mid-flight after a reload.
      status: 'fulfilled',
    };
  }
  return kept;
};

/**
 * Drops tag references that point at queries we did not keep.
 *
 * RTK Query stores its tag index as `{ tags: { [type]: { [id]: cacheKey[] } },
 * keys: { [cacheKey]: tags } }`, and its rehydration reducer reads both halves.
 * Anything unexpected is skipped rather than persisted, so a future shape
 * change costs us the tag index, not the whole cache.
 */
const pickProvidedFor = (provided: any, keptKeys: Set<string>) => {
  const tags: Record<string, Record<string, string[]>> = {};
  const keys: Record<string, any> = {};

  for (const [tagType, byId] of Object.entries(provided?.tags || {})) {
    const keptById: Record<string, string[]> = {};
    for (const [id, cacheKeys] of Object.entries((byId || {}) as Record<string, unknown>)) {
      if (!Array.isArray(cacheKeys)) continue;
      const filtered = cacheKeys.filter((key: string) => keptKeys.has(key));
      if (filtered.length) keptById[id] = filtered;
    }
    if (Object.keys(keptById).length) tags[tagType] = keptById;
  }

  for (const [cacheKey, value] of Object.entries(provided?.keys || {})) {
    if (keptKeys.has(cacheKey)) keys[cacheKey] = value;
  }

  return { tags, keys };
};

/**
 * Mirrors the RTK Query cache into localStorage so the next page load can paint
 * real data immediately. Writes are throttled and failures are swallowed —
 * persistence is an optimisation, never a requirement for the app to work.
 */
export const startApiCachePersistence = (store: Store) => {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let disabled = false;
  // The session this cache belongs to, fixed when persistence starts. Reading
  // it fresh at write time would let data fetched by one account be stamped
  // with whichever token happens to be in storage when the write fires.
  let owner = sessionKey();

  const write = () => {
    timer = null;
    if (disabled) return;
    try {
      if (sessionKey() !== owner) {
        // The session changed underneath us. Whatever is in the store belongs
        // to the previous account, so throw it away rather than persist it.
        clearPersistedApiCache();
        owner = sessionKey();
        return;
      }

      const apiState = (store.getState() as any).api;
      if (!apiState) return;
      const queries = pickReusableQueries(apiState.queries);
      const keptKeys = new Set(Object.keys(queries));
      if (!keptKeys.size) return;

      const snapshot: ApiCacheSnapshot = {
        owner,
        savedAt: Date.now(),
        api: { queries, mutations: {}, provided: pickProvidedFor(apiState.provided, keptKeys) },
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot));
    } catch {
      // Most likely the quota was exceeded. Drop what we have and stop trying
      // for this session rather than throwing on every dispatch.
      clearPersistedApiCache();
      disabled = true;
    }
  };

  const unsubscribe = store.subscribe(() => {
    if (disabled || timer) return;
    timer = setTimeout(write, WRITE_THROTTLE_MS);
  });

  // A refresh or tab close can land inside the throttle window.
  window.addEventListener('beforeunload', write);

  return () => {
    unsubscribe();
    window.removeEventListener('beforeunload', write);
    if (timer) clearTimeout(timer);
  };
};

export { rehydrateApiCache };
