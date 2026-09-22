import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

/**
 * The raw, individual `time_entries` rows -- distinct from `timeTrackingApi`,
 * which only ever returns pre-aggregated project/task totals. Transferring a
 * single recorded session to a different project needs the one row it
 * actually is, not a total several sessions were folded into.
 */
export interface TimeEntry {
  id: number;
  organization_id: number;
  user_id: number;
  project_id: number;
  task_id: number;
  start_time: string;
  end_time: string | null;
  total_seconds: number;
  status: string;
  is_manual: boolean;
  is_billable: boolean;
  description: string | null;
  client_op: string | null;
  created_at: string;
  updated_at: string;
  adjustment_seconds: number;
  net_seconds: number;
  is_running: boolean;
  elapsed_seconds: number;
  elapsed_time: string;
}

export interface TimeEntryTransfer {
  id: number;
  time_entry_id: number;
  from_project_id: number;
  from_task_id: number;
  to_project_id: number;
  to_task_id: number;
  transferred_by_user_id: number;
  reason: string | null;
  transferred_at: string;
}

export interface GetTimeEntriesArgs {
  start_date: string;
  end_date: string;
  user_id?: number;
}

export interface TransferTimeEntryArgs {
  id: number;
  to_project_id: number;
  to_task_id: number;
  reason?: string;
}

/**
 * Reaches every screen that reports tracked time, the same tag
 * `manualTimeEntryApi` invalidates on a mutation -- a transfer moves seconds
 * between a project's/task's totals exactly as a manual entry does, so the
 * dashboard, the reports and the day list must all go stale together.
 */
const TRACKED_TIME_CHANGED = ['TimeTracking' as const];

export const timeEntryApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getTimeEntries: builder.query<TimeEntry[], GetTimeEntriesArgs>({
      query: ({ start_date, end_date, user_id }) => {
        const params = new URLSearchParams({ start_date, end_date });
        if (user_id !== undefined) params.set('user_id', String(user_id));
        return `${ENDPOINTS.TIME_ENTRIES.BASE}?${params.toString()}`;
      },
      providesTags: (result) =>
        result
          ? [
              ...result.map((entry) => ({ type: 'TimeEntry' as const, id: entry.id })),
              { type: 'TimeEntry' as const, id: 'LIST' },
            ]
          : [{ type: 'TimeEntry' as const, id: 'LIST' }],
    }),

    getTimeEntryTransfers: builder.query<TimeEntryTransfer[], number>({
      query: (id) => ENDPOINTS.TIME_ENTRIES.TRANSFERS(id),
      providesTags: (_result, _error, id) => [{ type: 'TimeEntry' as const, id: `${id}-transfers` }],
    }),

    /** Reassigns an already-recorded entry's project/task. The payload has
     * no start/end/duration field to send -- the backend schema does not
     * accept one -- so this can only ever move which project a session
     * counts against, never create or resize tracked time. */
    transferTimeEntry: builder.mutation<TimeEntry, TransferTimeEntryArgs>({
      query: ({ id, ...body }) => ({
        url: ENDPOINTS.TIME_ENTRIES.TRANSFER(id),
        method: 'POST',
        body,
      }),
      invalidatesTags: (_result, _error, { id }) => [
        { type: 'TimeEntry' as const, id },
        { type: 'TimeEntry' as const, id: 'LIST' },
        { type: 'TimeEntry' as const, id: `${id}-transfers` },
        ...TRACKED_TIME_CHANGED,
      ],
    }),
  }),
});

export const {
  useGetTimeEntriesQuery,
  useGetTimeEntryTransfersQuery,
  useTransferTimeEntryMutation,
} = timeEntryApi;
