// @vitest-environment jsdom
/**
 * "Today" in the date presets is read when a range is resolved.
 *
 * It used to be a module constant (`TODAY`), captured when the bundle loaded.
 * A tab that stayed open -- or a laptop that slept overnight and woke with the
 * page still loaded -- therefore kept calling yesterday "Today", and Admin Time
 * Tracking asked the server about the wrong day (so the day's total looked
 * wrong) until somebody reloaded the page.
 *
 * Only the clock is faked; the module is imported once, exactly as a long-lived
 * tab holds it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DEFAULT_RANGE, rangeFor, rangeForSpan } from '../filters';

describe('date presets follow the clock', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const at = (iso: string) => vi.setSystemTime(new Date(iso));

  it('"Today" is the IST day it is now, and moves when the day does -- in the same module instance', () => {
    at('2026-10-05T18:00:00Z'); // 23:30 IST, 5 Oct
    expect(rangeFor('today', DEFAULT_RANGE)).toEqual({ preset: 'today', from: '2026-10-05', to: '2026-10-05' });

    at('2026-10-05T19:00:00Z'); // 00:30 IST, 6 Oct -- no reload in between
    expect(rangeFor('today', DEFAULT_RANGE)).toEqual({ preset: 'today', from: '2026-10-06', to: '2026-10-06' });
  });

  it('is the IST calendar, not UTC: 19:00 UTC is already the next day in India', () => {
    at('2026-10-05T19:00:00Z');
    expect(rangeFor('today', DEFAULT_RANGE).from).toBe('2026-10-06');
    at('2026-10-05T18:29:59Z');
    expect(rangeFor('today', DEFAULT_RANGE).from).toBe('2026-10-05');
  });

  it('every relative preset is measured from the live day', () => {
    at('2026-10-14T05:00:00Z'); // Wednesday
    expect(rangeFor('yesterday', DEFAULT_RANGE)).toMatchObject({ from: '2026-10-13', to: '2026-10-13' });
    expect(rangeFor('7d', DEFAULT_RANGE)).toMatchObject({ from: '2026-10-08', to: '2026-10-14' });
    expect(rangeFor('2w', DEFAULT_RANGE)).toMatchObject({ from: '2026-10-01', to: '2026-10-14' });
    expect(rangeFor('30d', DEFAULT_RANGE)).toMatchObject({ from: '2026-09-15', to: '2026-10-14' });
    expect(rangeFor('month', DEFAULT_RANGE)).toMatchObject({ from: '2026-10-01', to: '2026-10-14' });
    expect(rangeFor('lastMonth', DEFAULT_RANGE)).toMatchObject({ from: '2026-09-01', to: '2026-09-30' });
    expect(rangeFor('lastWeek', DEFAULT_RANGE)).toMatchObject({ from: '2026-10-05', to: '2026-10-11' });

    at('2026-10-15T05:00:00Z'); // the next morning
    expect(rangeFor('7d', DEFAULT_RANGE)).toMatchObject({ from: '2026-10-09', to: '2026-10-15' });
    expect(rangeFor('month', DEFAULT_RANGE)).toMatchObject({ from: '2026-10-01', to: '2026-10-15' });
  });

  it('a month boundary is crossed without a reload too', () => {
    at('2026-10-31T10:00:00Z');
    expect(rangeFor('month', DEFAULT_RANGE)).toMatchObject({ from: '2026-10-01', to: '2026-10-31' });
    at('2026-11-01T10:00:00Z');
    expect(rangeFor('month', DEFAULT_RANGE)).toMatchObject({ from: '2026-11-01', to: '2026-11-01' });
    expect(rangeFor('lastMonth', DEFAULT_RANGE)).toMatchObject({ from: '2026-10-01', to: '2026-10-31' });
  });

  it('a span handed in from outside is clamped to the live today, not to the day the page loaded', () => {
    at('2026-10-05T10:00:00Z');
    expect(rangeForSpan('2026-10-01', '2026-10-06')).toMatchObject({ from: '2026-10-01', to: '2026-10-05' });
    at('2026-10-06T10:00:00Z');
    expect(rangeForSpan('2026-10-01', '2026-10-06')).toMatchObject({ from: '2026-10-01', to: '2026-10-06' });
  });

  it('a range whose ends are on one day never straddles midnight mid-call', () => {
    at('2026-10-05T18:29:59.999Z'); // one millisecond before IST midnight
    const range = rangeFor('7d', DEFAULT_RANGE);
    expect(range.to).toBe('2026-10-05');
    expect(range.from).toBe('2026-09-29'); // six days before the SAME day, not before a day that just rolled over
  });
});
