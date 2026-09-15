/**
 * The frontend's single authoritative time helpers.
 *
 * Mirrors `backend/app/core/time_format.py` and `desktop/core/time_format.py`
 * so that no layer can disagree about what a duration of N seconds looks like.
 *
 *  - A **duration** is exact integer seconds rendered as `HH:MM:SS`. It is
 *    never passed through a timezone conversion, and it never wraps at 24
 *    hours: 90061 seconds is `25:01:01`, not `01:01:01`. That rules out
 *    `Date`-based formatting, which always wraps.
 *  - A **timestamp** is a point in time. It arrives as UTC and is displayed in
 *    Asia/Kolkata, via a real timezone identifier rather than a hard-coded
 *    +05:30 offset.
 */

export const IST_TIME_ZONE = 'Asia/Kolkata';

/**
 * Format an exact duration in seconds as `HH:MM:SS`.
 *
 * Integer arithmetic only. Null/undefined/negative render as `00:00:00`:
 * tracked time is never negative.
 */
export function formatHMS(totalSeconds: number | null | undefined): string {
  if (totalSeconds == null || Number.isNaN(totalSeconds)) return '00:00:00';
  const seconds = Math.max(0, Math.trunc(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  return [hours, minutes, secs].map((n) => String(n).padStart(2, '0')).join(':');
}

/**
 * Format a decimal-hours value as `HH:MM:SS`.
 *
 * Only for endpoints that still expose hours as their sole representation.
 * Prefer `formatHMS` on an exact `*_seconds` field wherever one exists —
 * decimal hours have already lost precision by the time they reach us.
 */
export function formatHoursAsHMS(hours: number | null | undefined): string {
  if (hours == null || Number.isNaN(hours)) return '00:00:00';
  return formatHMS(Math.round(hours * 3600));
}

/**
 * Exact seconds from an API object that carries them, or the nearest second
 * from its decimal hours when it does not.
 *
 * Every tracked-time response now carries `total_seconds` beside the 2dp
 * `total_hours`. Reading the seconds is the only way two screens can agree
 * about one duration: hours rounded to 2dp are a 36-second grid, so a
 * ten-second session is 0.00h on one screen and 00:00:10 on another.
 */
export function secondsOf(
  value: { total_seconds?: number | null; total_hours?: number | null } | null | undefined,
): number {
  if (!value) return 0;
  if (typeof value.total_seconds === 'number' && !Number.isNaN(value.total_seconds)) {
    return Math.max(0, Math.trunc(value.total_seconds));
  }
  return Math.max(0, Math.round((value.total_hours ?? 0) * 3600));
}

/**
 * Today's date in IST as `YYYY-MM-DD` -- the calendar every date filter and
 * every "cannot be in the future" rule in the system is measured on.
 *
 * Neither the browser's local day nor the UTC day is right: the backend
 * reports against Asia/Kolkata, so both of those disagree with it for part of
 * every day. Evaluated on each call rather than once at import, so a page
 * left open across midnight does not keep yesterday's "today".
 */
export function istTodayISO(now: Date = new Date()): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: IST_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(now);
}

/** Render a UTC timestamp as IST clock time, e.g. "14:35". */
export function formatISTTime(value: string | null | undefined): string {
  if (!value) return '-';
  return new Date(value).toLocaleTimeString('en-GB', {
    timeZone: IST_TIME_ZONE,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

/**
 * Render a UTC timestamp as IST clock time on a 12-hour clock, e.g. "2:35 pm".
 *
 * The same instant `formatISTTime` renders, in the form the screenshot
 * timeline reads its hour rows in. Lower-cased deliberately: "10:00 am" beside
 * "11:00 am" is a span a person reads at a glance, where "10:00 AM" shouts.
 */
export function formatISTTime12(value: string | null | undefined): string {
  if (!value) return '-';
  return new Date(value)
    .toLocaleTimeString('en-US', {
      timeZone: IST_TIME_ZONE,
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    })
    // Chrome separates the time from "am"/"pm" with a narrow no-break space,
    // Node with an ordinary one. Normalised so the two render identically and
    // so a caller can match on the string.
    .replace(/[  ]/g, ' ')
    .toLowerCase();
}

/** Render a UTC timestamp as an IST date, e.g. "12 Jun 2026". */
export function formatISTDate(value: string | null | undefined): string {
  if (!value) return '';
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: IST_TIME_ZONE,
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  }).format(new Date(value));
}

/**
 * Convert an IST wall-clock date + `HH:mm` into a UTC ISO timestamp.
 *
 * Users type the time they actually worked, in their own clock. Sending that
 * string with a `Z` suffix would claim it was UTC and shift every entry by
 * 5h30m, so the offset is resolved through the real Asia/Kolkata zone.
 */
export function istWallClockToUtcISO(dateStr: string, timeStr: string): string {
  // Interpret "<date>T<time>" as if it were UTC, then measure how far that
  // instant is from the same wall clock in IST, and correct by the difference.
  const asUtc = new Date(`${dateStr}T${timeStr}:00Z`);
  const istWallClock = new Date(
    asUtc.toLocaleString('en-US', { timeZone: IST_TIME_ZONE }),
  );
  const utcWallClock = new Date(asUtc.toLocaleString('en-US', { timeZone: 'UTC' }));
  const offsetMs = istWallClock.getTime() - utcWallClock.getTime();
  return new Date(asUtc.getTime() - offsetMs).toISOString();
}
