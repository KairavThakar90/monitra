// @vitest-environment jsdom
/**
 * What the persisted API cache is allowed to paint on the first frame.
 *
 * Restoring the cache shows old rows immediately and corrects them a moment
 * later -- fine for a project list, wrong for a running timer. The server
 * measures a running entry as `now - start_time`, so a figure saved before the
 * machine slept (or yesterday) reads as hours that no longer exist, and the
 * desktop ends a session retroactively once it wakes. The three live
 * tracked-time endpoints are therefore never written and never restored; every
 * other query behaves exactly as before.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { loadPersistedApiCache, startApiCachePersistence } from '../persist';

const STORAGE_KEY = 'monitra.api.cache.v1';
const TOKEN = 'token-1';

const fulfilled = (endpointName: string, data: unknown, at = Date.now()) => ({
  status: 'fulfilled',
  endpointName,
  data,
  fulfilledTimeStamp: at,
});

const PROJECTS = 'getAllProjects(undefined)';
const ACTIVE = 'getActiveTimeTracking(undefined)';
const DAYS = 'getTimeTracking({"page":1})';
const DETAILS = 'getTimeTrackingDetails({"employeeId":7})';

const queries = () => ({
  [PROJECTS]: fulfilled('getAllProjects', [{ id: 1 }]),
  [ACTIVE]: fulfilled('getActiveTimeTracking', { items: [{ time_entry_id: 5, elapsed_time: '03:12:45' }] }),
  [DAYS]: fulfilled('getTimeTracking', { items: [{ employee_id: 7, total_time: '03:12:45' }] }),
  [DETAILS]: fulfilled('getTimeTrackingDetails', { summary: { total_time: '03:12:45' } }),
});

const provided = () => ({
  tags: {
    Project: { LIST: [PROJECTS] },
    TimeTracking: { ACTIVE: [ACTIVE], LIST: [DAYS], 7: [DETAILS] },
  },
  keys: {
    [PROJECTS]: [{ type: 'Project', id: 'LIST' }],
    [ACTIVE]: [{ type: 'TimeTracking', id: 'ACTIVE' }],
    [DAYS]: [{ type: 'TimeTracking', id: 'LIST' }],
    [DETAILS]: [{ type: 'TimeTracking', id: 7 }],
  },
});

describe('persisted API cache', () => {
  let storage: Map<string, string>;

  beforeEach(() => {
    vi.useFakeTimers();
    storage = new Map([['accessToken', TOKEN]]);
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
      removeItem: (key: string) => storage.delete(key),
      clear: () => storage.clear(),
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const saved = () => JSON.parse(storage.get(STORAGE_KEY)!);

  describe('writing', () => {
    const persistOnce = () => {
      let notify: () => void = () => {};
      const store = {
        getState: () => ({ api: { queries: queries(), provided: provided() } }),
        subscribe: (listener: () => void) => {
          notify = listener;
          return () => {};
        },
      };
      const stop = startApiCachePersistence(store as never);
      notify();
      vi.advanceTimersByTime(2_000); // past the write throttle
      return stop;
    };

    it('keeps an ordinary query and drops the three live tracked-time ones', () => {
      persistOnce();
      expect(Object.keys(saved().api.queries)).toEqual([PROJECTS]);
    });

    it('does not leave tag references pointing at the dropped queries', () => {
      persistOnce();
      const { tags, keys } = saved().api.provided;
      expect(tags).toEqual({ Project: { LIST: [PROJECTS] } });
      expect(Object.keys(keys)).toEqual([PROJECTS]);
    });

    it('writes nothing at all when only live queries are cached', () => {
      let notify: () => void = () => {};
      const only = Object.fromEntries(Object.entries(queries()).filter(([key]) => key !== PROJECTS));
      startApiCachePersistence({
        getState: () => ({ api: { queries: only, provided: provided() } }),
        subscribe: (listener: () => void) => {
          notify = listener;
          return () => {};
        },
      } as never);
      notify();
      vi.advanceTimersByTime(2_000);
      expect(storage.has(STORAGE_KEY)).toBe(false);
    });
  });

  describe('restoring', () => {
    /** A snapshot as an earlier build wrote it: live readings included. */
    const seedOldSnapshot = (savedAt = Date.now()) => {
      storage.set(
        STORAGE_KEY,
        JSON.stringify({ owner: TOKEN, savedAt, api: { queries: queries(), mutations: {}, provided: provided() } }),
      );
    };

    it('drops live tracked-time readings that an earlier build persisted', () => {
      seedOldSnapshot();
      const restored = loadPersistedApiCache() as { api: { queries: Record<string, unknown> } };
      expect(Object.keys(restored.api.queries)).toEqual([PROJECTS]);
    });

    it('prunes the tag index of the dropped queries too', () => {
      seedOldSnapshot();
      const restored = loadPersistedApiCache() as { api: { provided: { tags: object; keys: object } } };
      expect(restored.api.provided.tags).toEqual({ Project: { LIST: [PROJECTS] } });
      expect(Object.keys(restored.api.provided.keys)).toEqual([PROJECTS]);
    });

    it('still marks what it does restore as stale, so it is revalidated behind the first paint', () => {
      seedOldSnapshot();
      const restored = loadPersistedApiCache() as { api: { queries: Record<string, { fulfilledTimeStamp: number }> } };
      expect(restored.api.queries[PROJECTS].fulfilledTimeStamp).toBe(0);
    });

    it('a day-old running total is not shown on the next visit', () => {
      // Written 23 hours ago -- inside the 24 h window that other data survives.
      seedOldSnapshot(Date.now() - 23 * 60 * 60 * 1000);
      const restored = loadPersistedApiCache() as { api: { queries: Record<string, { data?: unknown }> } };
      expect(JSON.stringify(restored.api.queries)).not.toContain('03:12:45');
    });

    it('is unchanged for everything else: another user’s snapshot is still never shown', () => {
      seedOldSnapshot();
      storage.set('accessToken', 'someone-else');
      expect(loadPersistedApiCache()).toBeUndefined();
    });
  });
});
