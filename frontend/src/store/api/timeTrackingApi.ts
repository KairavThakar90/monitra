import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

export interface TimeTrackingEntry {
  employee_id: number;
  name: string;
  email: string;
  designation: string;
  date: string;
  start_time: string | null;
  end_time: string | null;
  total_seconds: number;
  /** Legacy "13h 22m" label. Prefer total_time for exact durations. */
  total_hours: string;
  /** Exact tracked duration, HH:MM:SS. */
  total_time: string;
}

export interface TimeTrackingListResponse {
  items: TimeTrackingEntry[];
  pagination: { page?: number; limit?: number; total?: number; total_pages?: number; [key: string]: unknown };
}

export interface TimeTrackingStatus {
  id: number;
  name: string;
  color: string;
}

export interface TimeTrackingTask {
  id: number;
  name: string;
  status: TimeTrackingStatus;
  total_seconds: number;
  total_hours: string;
  total_time: string;
}

export interface TimeTrackingProject {
  id: number;
  name: string;
  status: TimeTrackingStatus;
  total_seconds: number;
  total_hours: string;
  total_time: string;
  tasks: TimeTrackingTask[];
}

export interface TimeTrackingDetails {
  employee: { id: number; name: string; email: string; designation: string; role: string };
  start_date: string;
  end_date: string;
  summary: { start_time: string | null; end_time: string | null; total_seconds: number; total_hours: string; total_time: string };
  projects: TimeTrackingProject[];
}

/** A member who has a timer running right now. */
export interface ActiveTimeTrackingItem {
  time_entry_id: number;
  employee_id: number;
  name: string;
  email: string | null;
  designation: string | null;
  project_id: number;
  project_name: string;
  task_id: number;
  task_name: string;
  /** UTC instant the running entry started. */
  start_time: string;
  /** Net elapsed seconds as of `server_time`, measured by the server. */
  elapsed_seconds: number;
  /** `elapsed_seconds` as HH:MM:SS. */
  elapsed_time: string;
  /**
   * The member's duration-weighted activity for today (IST), 0-100. `null` when
   * no activity has been measured yet today: unknown, which is not 0%.
   */
  activity_percentage: number | null;
  /**
   * Whether the member's desktop is still reporting in (docs/TIMING_MODEL.md
   * section 9). `live`: it has within the stale window. `stale`: it has gone
   * quiet, and `elapsed_seconds` has stopped growing at `measured_until` -- this
   * is a timer nobody is vouching for, not live work. `unknown`: it has never
   * reported (a desktop older than the heartbeat), measured to now as before.
   * Absent from a backend that predates the field.
   */
  liveness?: 'live' | 'stale' | 'unknown' | null;
  /** The last instant the member's desktop proved it was alive (UTC). */
  last_seen_at?: string | null;
  /** The instant `elapsed_seconds` is measured to (UTC). */
  measured_until?: string | null;
  /**
   * The Monitra desktop version this member's desktop last reported (`1.3.2`), its OS family
   * (`win32`, `darwin`, `linux`) and when it said so (UTC). Reported by the desktop's own update
   * check, about every ten hours, so just after an upgrade it can still show the previous
   * version. Null when that desktop has never reported one; absent from an older backend.
   */
  desktop_version?: string | null;
  desktop_platform?: string | null;
  desktop_version_seen_at?: string | null;
}

export interface ActiveTimeTrackingResponse {
  items: ActiveTimeTrackingItem[];
  total: number;
  /** When the server produced this answer. */
  server_time: string;
}

const addDateParams = (url: string, params: { range?: string; date?: string; start_date?: string; end_date?: string; employee_id?: number }) => {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value));
  });
  const queryString = query.toString();
  return queryString ? `${url}?${queryString}` : url;
};

export const timeTrackingApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getTimeTracking: builder.query<TimeTrackingListResponse, { range?: string; date?: string; start_date?: string; end_date?: string; employee_id?: number; page?: number; limit?: number }>({
      query: (params) => addDateParams(ENDPOINTS.TIME_TRACKING.GET_ALL, params),
      providesTags: [{ type: 'TimeTracking' as const, id: 'LIST' }],
    }),
    /** Who is tracking right now. Polled by the Active Users page, not pushed. */
    getActiveTimeTracking: builder.query<ActiveTimeTrackingResponse, void>({
      query: () => ENDPOINTS.TIME_TRACKING.GET_ACTIVE,
      providesTags: [{ type: 'TimeTracking' as const, id: 'ACTIVE' }],
    }),
    getTimeTrackingDetails: builder.query<TimeTrackingDetails, { employeeId: number; range?: string; date?: string; start_date?: string; end_date?: string }>({
      query: ({ employeeId, ...params }) => addDateParams(ENDPOINTS.TIME_TRACKING.GET_BY_EMPLOYEE(employeeId), params),
      providesTags: (_result, _error, { employeeId }) => [{ type: 'TimeTracking' as const, id: employeeId }],
    }),
  }),
});

export const {
  useGetTimeTrackingQuery,
  useGetActiveTimeTrackingQuery,
  useGetTimeTrackingDetailsQuery,
} = timeTrackingApi;
