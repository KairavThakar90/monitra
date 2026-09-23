import { rangeFor } from '../dashboard/v2/filters';
import type { DateRange } from '../dashboard/v2/filters';
import { formatHMS } from '../../utils/duration';

/** The client portal's default filter: today, using the same `DateRange`
 * shape and `DateRangeFilter` component the staff/member dashboards use. */
export const CLIENT_DEFAULT_RANGE: DateRange = rangeFor('today', { preset: 'today', from: '', to: '' });

/** A tracked-seconds figure the backend may have withheld (Timing disabled
 * for this client): `null` renders as "Not shared", never as a fabricated
 * "00:00:00" that would read as a real, measured zero. */
export const formatSharedHMS = (seconds: number | null): string =>
  seconds === null ? 'Not shared' : formatHMS(seconds);

/** Same as `formatSharedHMS`, for an hours figure already in decimal form. */
export const formatSharedHours = (hours: number | null): string =>
  hours === null ? 'Not shared' : `${hours}h`;

export const longDate = (iso: string) => {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, (m || 1) - 1, d || 1).toLocaleDateString('en-GB', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  });
};
