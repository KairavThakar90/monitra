import { rangeFor } from '../dashboard/v2/filters';
import type { DateRange } from '../dashboard/v2/filters';

/** The client portal's default filter: today, using the same `DateRange`
 * shape and `DateRangeFilter` component the staff/member dashboards use. */
export const CLIENT_DEFAULT_RANGE: DateRange = rangeFor('today', { preset: 'today', from: '', to: '' });

export const longDate = (iso: string) => {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, (m || 1) - 1, d || 1).toLocaleDateString('en-GB', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  });
};
