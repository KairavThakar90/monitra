import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

/**
 * Filters accepted by `/api/v1/react/dashboard`. They are the same set the
 * Reports page sends — `start_date`/`end_date` are IST calendar dates,
 * inclusive at both ends, and `member_id`/`project_id`/`task_id` are repeated
 * once per selected id. FastAPI drops query parameters it does not declare, so
 * the legacy `from`/`to` names must not be used here.
 */
export interface ReactDashboardQueryParams {
  start_date: string;
  end_date: string;
  project_id?: number[];
  task_id?: number[];
  member_id?: number[];
  top_n?: number;
}

export interface ReactDashboardSummary {
  /** Null when nothing in the selected scope was activity-sampled. */
  activity: number | null;
  monthly_activity: number | null;
  /**
   * Exact tracked seconds in scope -- render durations from this. `total_hours`
   * is 2dp, i.e. a 36-second grid: a ten-second session is 0.00h there and
   * disagrees with the entry it came from.
   */
  total_seconds: number;
  total_hours: number;
  active_projects: number;
  team_members: number;
  total_tasks: number;
}

export interface ReactDashboardTimePoint {
  /** IST calendar date, `YYYY-MM-DD`. */
  date: string;
  tracked_hours: number;
  manual_hours: number;
}

export interface ReactDashboardTopProject {
  project_id: number;
  project_name: string;
  total_hours: number;
  avg_activity: number | null;
}

export interface ReactDashboardTopMember {
  member_id: number;
  member_name: string;
  total_hours: number;
  avg_activity: number | null;
}

export interface ReactDashboardTopApp {
  app_id: number;
  app_name: string;
  total_hours: number;
  /** Share of `total_app_hours`, 0-100. Null when there is no app usage in scope. */
  percentage: number | null;
}

export interface ReactDashboardProjectBilling {
  project_id: number;
  project_name: string;
  /** 'fixed' or 'free', from Project.billing_type. */
  billing_type: string;
  /** The project's fixed-hour budget. Null for every 'free'-billing project. */
  fixed_hours: number | null;
  /** All-time tracked seconds -- not scoped to the dashboard's selected date range. */
  /** All-time Used seconds: work tasks only, the four default (internal) tasks excluded. */
  completed_seconds: number;
  completed_hours: number;
  /** All-time seconds on the four default tasks. Does not consume the fixed budget. */
  internal_seconds: number;
  internal_hours: number;
  /** fixed_hours - Used, negative when over budget; null without a fixed budget. */
  remaining_seconds: number | null;
  /** completed_hours / fixed_hours * 100. Null when fixed_hours is null. */
  usage_percentage: number | null;
  /** Tracked seconds within the dashboard's selected date range. */
  tracked_seconds: number;
  tracked_hours: number;
  /** Average activity % within the selected date range. Null when unsampled. */
  avg_activity: number | null;
}

interface Page<T> {
  items: T[];
  page: number;
  limit: number;
  total: number;
  pages: number;
}

export interface ReactDashboardResponse {
  filters: {
    start_date: string;
    end_date: string;
    project_id: number[];
    task_id: number[];
    member_id: number[];
  };
  summary: ReactDashboardSummary;
  time_tracked: {
    /** 'day' for every dashboard preset; 'week' or 'month' for longer spans. */
    interval: string;
    data: ReactDashboardTimePoint[];
  };
  top_projects: Page<ReactDashboardTopProject>;
  top_members: Page<ReactDashboardTopMember>;
  top_apps: Page<ReactDashboardTopApp> & { total_app_hours: number };
  /** Non-archived 'fixed'-billing projects -- the Billable filter tab. */
  billable_projects: ReactDashboardProjectBilling[];
  /** Non-archived 'free'-billing projects -- the Free Time / Internal filter tab. */
  internal_projects: ReactDashboardProjectBilling[];
}

const buildQueryParams = (params: object) => {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      if (Array.isArray(value)) {
        value.forEach((v) => query.append(key, String(v)));
      } else {
        query.append(key, String(value));
      }
    }
  });
  return query.toString();
};

export const dashboardApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getReactDashboard: builder.query<ReactDashboardResponse, ReactDashboardQueryParams>({
      query: (params) => ({
        url: `${ENDPOINTS.REACT_DASHBOARD.BASE}?${buildQueryParams(params)}`,
        method: 'GET',
      }),
      providesTags: ['TimeTracking'],
    }),
  }),
  overrideExisting: true,
});

export const { useGetReactDashboardQuery } = dashboardApi;
