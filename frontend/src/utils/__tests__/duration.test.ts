/**
 * The frontend's single authority for durations and for "today".
 *
 * Every screen that shows tracked time formats through these helpers, so a
 * wrong answer here is a wrong answer on the dashboard, the reports and the
 * timesheet at once. Two of the cases pin real defects:
 *
 *  - `secondsOf` exists because the dashboard's "Time Worked" card rebuilt
 *    seconds from 2dp hours: a 10-second session rendered as 00:00:00, and a
 *    17m11s task rendered 18 seconds away from the report for the same task.
 *  - `istTodayISO` exists because three screens defined "today" three ways
 *    (UTC, browser-local, IST-frozen-at-import); the backend measures every
 *    date rule in Asia/Kolkata, so those disagreed with it for part of each day.
 */
import { describe, expect, it } from 'vitest';

import { formatHMS, formatHoursAsHMS, istTodayISO, secondsOf } from '../duration';

describe('formatHMS', () => {
  it('renders exact seconds and never wraps at 24 hours', () => {
    expect(formatHMS(4375)).toBe('01:12:55');
    expect(formatHMS(90061)).toBe('25:01:01');
    expect(formatHMS(10)).toBe('00:00:10');
  });

  it('renders nothing, null and negatives as zero', () => {
    expect(formatHMS(0)).toBe('00:00:00');
    expect(formatHMS(null)).toBe('00:00:00');
    expect(formatHMS(undefined)).toBe('00:00:00');
    expect(formatHMS(-5)).toBe('00:00:00');
    expect(formatHMS(Number.NaN)).toBe('00:00:00');
  });
});

describe('secondsOf', () => {
  it('prefers the exact seconds the API carries', () => {
    expect(secondsOf({ total_seconds: 10, total_hours: 0 })).toBe(10);
    expect(secondsOf({ total_seconds: 1031, total_hours: 0.29 })).toBe(1031);
  });

  it('falls back to decimal hours only when no seconds are present', () => {
    expect(secondsOf({ total_hours: 1.5 })).toBe(5400);
    expect(secondsOf({ total_hours: 0.29 })).toBe(1044);
  });

  it('shows the same figure the entry was stored with, where hours could not', () => {
    // A ten-second session: 0.00h on the 2dp grid, 00:00:10 in seconds.
    expect(formatHoursAsHMS(0)).toBe('00:00:00');
    expect(formatHMS(secondsOf({ total_seconds: 10, total_hours: 0 }))).toBe('00:00:10');
  });

  it('is zero for a missing summary', () => {
    expect(secondsOf(null)).toBe(0);
    expect(secondsOf(undefined)).toBe(0);
    expect(secondsOf({})).toBe(0);
  });
});

describe('istTodayISO', () => {
  it('is the Asia/Kolkata calendar day, not the UTC one', () => {
    // 20:00 UTC on the 14th is 01:30 IST on the 15th.
    expect(istTodayISO(new Date('2026-09-14T20:00:00Z'))).toBe('2026-09-15');
    // 18:00 UTC on the 14th is 23:30 IST, still the 14th.
    expect(istTodayISO(new Date('2026-09-14T18:00:00Z'))).toBe('2026-09-14');
  });

  it('is evaluated when asked, not frozen at import', () => {
    const early = istTodayISO(new Date('2026-09-14T18:00:00Z'));
    const later = istTodayISO(new Date('2026-09-14T20:00:00Z'));
    expect(early).not.toBe(later);
  });
});
