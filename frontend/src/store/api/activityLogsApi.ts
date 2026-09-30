import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

/**
 * The activity trail.
 *
 * One row per thing a person did: signing in and out, opening and closing the
 * desktop application, starting and stopping the timer, requesting manual time
 * and deciding on a request, creating or changing a project or a task, and the
 * member-directory and feedback decisions an administrator makes. The backend
 * writes the rows (`app/services/activity_log.py`); the dashboard only reads.
 *
 * `GET /activity-logs` answers with the rows already grouped by the employee
 * who acted, newest first inside each group, for everyone the caller may see:
 * the whole organization, or a leader's own team. Nothing here decides who is
 * visible -- a filter this page sends can only narrow that answer.
 */

/** The `module` values the backend writes, in the order the filter lists them. */
export type ActivityLogModule =
  | 'auth'
  | 'desktop'
  | 'timer'
  | 'manual_time'
  | 'project'
  | 'task'
  | 'member'
  | 'feedback'
  | 'system';

export interface ActivityLogEntry {
  id: number;
  module: ActivityLogModule | string;
  action: string;
  description: string | null;
  /** Which client performed it, when the request said: desktop, web or api. */
  source: 'desktop' | 'web' | 'api' | string | null;
  client_version: string | null;
  project_id: number | null;
  project_name: string | null;
  task_id: number | null;
  task_name: string | null;
  entity_id: number | null;
  ip_address: string | null;
  /** UTC instant, ISO-8601. */
  created_at: string;
}

export interface ActivityLogMemberGroup {
  user_id: number;
  name: string;
  email: string | null;
  designation: string | null;
  role_name: string | null;
  entry_count: number;
  last_activity_at: string;
  entries: ActivityLogEntry[];
}

export interface ActivityLogListResponse {
  start_date: string;
  end_date: string;
  total: number;
  /**
   * True when the window held more rows than one response returns. The newest
   * are kept, so the page says so rather than presenting a cut list as whole.
   */
  truncated: boolean;
  modules: string[];
  members: ActivityLogMemberGroup[];
}

export interface ActivityLogListArgs {
  /** IST calendar days, `YYYY-MM-DD`. */
  start: string;
  end: string;
  module?: string | null;
  search?: string;
}

export const activityLogsApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getActivityLogs: builder.query<ActivityLogListResponse, ActivityLogListArgs>({
      query: ({ start, end, module, search }) => {
        const params = new URLSearchParams({ start, end });
        if (module) params.set('module', module);
        if (search && search.trim()) params.set('search', search.trim());
        return `${ENDPOINTS.ACTIVITY_LOGS.BASE}?${params.toString()}`;
      },
      providesTags: [{ type: 'ActivityLog' as const, id: 'LIST' }],
      // A trail is only useful if it is current: a cached answer is shown at
      // once and always re-read behind it, rather than trusted for a minute.
      keepUnusedDataFor: 120,
    }),
  }),
});

export const { useGetActivityLogsQuery } = activityLogsApi;
