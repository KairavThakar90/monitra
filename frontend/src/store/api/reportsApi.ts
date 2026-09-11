import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

/**
 * Filters for the legacy `/api/v1/reports/*` endpoints, which declare the date
 * range as `from`/`to` aliases.
 */
export interface ReportQueryParams {
  from: string;
  to: string;
  member_id?: number[];
  project_id?: number[];
  billing_type?: string;
  usage_type?: 'app' | 'url';
}

/**
 * Filters for the `/api/v1/react/reports/*` endpoints.
 *
 * The date range is `start_date`/`end_date` here — NOT `from`/`to`. FastAPI
 * drops query parameters it does not declare, so sending the legacy names made
 * every report silently answer for the server's default window (the last seven
 * days) no matter what the user picked. `member_id` and `project_id` are
 * repeated once per selected id, matching the multi-select pickers.
 */
export interface ReactReportQueryParams {
  start_date: string;
  end_date: string;
  member_id?: number[];
  project_id?: number[];
  task_id?: number[];
}

export interface DetailedLogsQueryParams extends ReportQueryParams {
  dimension?: 'projects' | 'members' | 'tasks' | 'apps';
  search?: string;
  sort_by?: 'date' | 'member' | 'project' | 'task' | 'hours' | 'activity';
  sort_desc?: boolean;
  page?: number;
  limit?: number;
}


export interface ReactReportsSummaryResponse {
  total_hours: number;
  avg_activity: number | null;
  total_members: number;
  total_tasks: number;
}

export interface ReactReportsItem {
  /**
   * Exact tracked seconds for this row — prefer this over `total_hours`.
   * A share computed from 2dp hours can exceed the exact scope total it is
   * divided by; the Projects ring showed a single project at 100.4%.
   */
  total_seconds: number;
  total_hours: number;
  avg_activity: number | null;
  total_members: number;
  total_tasks: number;
  project_id?: number;
  project_name?: string;
  task_id?: number;
  task_name?: string;
  app_id?: number;
  app_name?: string;
  url_id?: number;
  url_name?: string;
}

export interface ReactReportsListResponse {
  items: ReactReportsItem[];
  page: number;
  limit: number;
  total: number;
  pages: number;
  /**
   * Seconds held by every matching row, not just this page — the denominator
   * for a part-to-whole chart of this tab.
   *
   * This is not interchangeable with the /summary endpoint's total. Summary
   * counts session time; the Apps and URLs tabs count application and browser
   * time, which the desktop measures separately and which legitimately covers
   * less of the day. Dividing one by the other drew every second that
   * application capture had never claimed to measure as one unnamed slice.
   */
  total_seconds: number;
  /** `total_seconds` as hours, 2dp. Prefer the seconds for durations. */
  total_hours: number;
}

export interface ReactReportsTrendPoint {
  /** IST calendar date, `YYYY-MM-DD`. */
  date: string;
  /** Exact tracked seconds for the day — prefer this over `total_hours`. */
  total_seconds: number;
  total_hours: number;
  /** Null when nothing on this day was activity-sampled. */
  avg_activity: number | null;
}

export interface ReactReportsTrendResponse {
  start_date: string;
  end_date: string;
  /** Every day in the range, in order. A day with no tracking is a real zero. */
  points: ReactReportsTrendPoint[];
}

export interface ReportGroupedItem {
  id: number | string;
  name: string;
  tracked_seconds: number;
  tracked_hours: number;
  tracked_hours_formatted: string;
  /** Exact tracked duration, HH:MM:SS. */
  tracked_time: string;
  activity_percentage: number;
  meta_label: string;
}

export interface ReportSummary {
  total_hours: number;
  total_tracked_seconds: number;
  total_hours_formatted: string;
  /** Exact total tracked duration, HH:MM:SS. */
  total_tracked_time: string;
  average_activity_percentage: number;
  total_members: number;
  total_entries: number;
  total_projects?: number | null;
  total_tasks?: number | null;
  total_apps?: number | null;
}

export interface ReportGroupedResponse {
  start_date: string;
  end_date: string;
  summary: ReportSummary;
  grouped_data: ReportGroupedItem[];
}

export interface DetailedLogItem {
  id: string;
  date: string;
  member_id: number;
  member_name: string;
  role: string;
  project_id: number | null;
  project_name: string | null;
  task_id: number | null;
  task_name: string | null;
  app: string | null;
  url: string | null;
  tracked_seconds: number;
  tracked_hours: number;
  tracked_time: string;
  activity_percentage: number | null;
}

export interface DetailedLogsResponse {
  start_date: string;
  end_date: string;
  items: DetailedLogItem[];
  pagination: {
    page: number;
    limit: number;
    total: number;
    total_pages: number;
  };
}

export interface ProjectTaskSummaryTask {
  id: number;
  task_name: string;
  task_created_date: string;
  total_tracked_seconds: number;
  total_tracked_hours: number;
  total_tracked_time: string;
}

export interface ProjectTaskSummaryStatus {
  id: number;
  name: string;
  color: string;
}

export interface ProjectTaskSummaryProject {
  id: number;
  project_name: string;
  created_date: string;
  status: ProjectTaskSummaryStatus | null;
  total_task_count: number;
  total_task_seconds: number;
  total_task_hours: number;
  total_task_time: string;
  tasks: ProjectTaskSummaryTask[];
}

export interface ProjectTaskSummaryResponse {
  projects: ProjectTaskSummaryProject[];
  pagination: {
    page: number;
    limit: number;
    total_projects: number;
    total_pages: number;
  };
}

export interface ProjectTaskSummaryQueryParams {
  page?: number;
  limit?: number;
  start_date?: string;
  end_date?: string;
  project_id?: number[];
}

const buildQueryParams = (params: any) => {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      if (Array.isArray(value)) {
        value.forEach(v => query.append(key, String(v)));
      } else {
        query.append(key, String(value));
      }
    }
  });
  return query.toString();
};

export const reportsApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getReactReportsSummary: builder.query<ReactReportsSummaryResponse, ReactReportQueryParams>({
      query: (params) => ({
        url: `${ENDPOINTS.REPORTS.BASE.replace('/reports', '/react/reports')}/summary?${buildQueryParams(params)}`,
        method: 'GET',
      }),
      providesTags: ['TimeTracking'],
    }),
    getReactReportsList: builder.query<ReactReportsListResponse, { dimension: string; search?: string; sort_by?: string; sort_order?: string; page?: number; limit?: number; } & ReactReportQueryParams>({
      query: ({ dimension, ...params }) => ({
        url: `${ENDPOINTS.REPORTS.BASE.replace('/reports', '/react/reports')}/${dimension}?${buildQueryParams(params)}`,
        method: 'GET',
      }),
      providesTags: ['TimeTracking'],
    }),
    getReactReportsTrend: builder.query<ReactReportsTrendResponse, ReactReportQueryParams>({
      query: (params) => ({
        url: `${ENDPOINTS.REPORTS.BASE.replace('/reports', '/react/reports')}/trend?${buildQueryParams(params)}`,
        method: 'GET',
      }),
      providesTags: ['TimeTracking'],
    }),

    getGroupedReport: builder.query<ReportGroupedResponse, { dimension: string } & ReportQueryParams>({
      query: ({ dimension, ...params }) => ({
        url: `${ENDPOINTS.REPORTS.BASE}/${dimension}?${buildQueryParams(params)}`,
        method: 'GET',
      }),
      providesTags: ['TimeTracking'],
    }),
    getDetailedLogs: builder.query<DetailedLogsResponse, DetailedLogsQueryParams>({
      query: (params) => ({
        url: `${ENDPOINTS.REPORTS.DETAILED_LOGS}?${buildQueryParams(params)}`,
        method: 'GET',
      }),
      providesTags: ['TimeTracking'],
    }),
    getProjectTaskSummary: builder.query<ProjectTaskSummaryResponse, ProjectTaskSummaryQueryParams>({
      query: (params) => ({
        url: `${ENDPOINTS.REPORTS.BASE}/project-task-summary?${buildQueryParams(params)}`,
        method: 'GET',
      }),
      providesTags: ['TimeTracking', 'Project', 'Task'],
    }),
  }),
  overrideExisting: true,
});

export const { useGetGroupedReportQuery, useGetDetailedLogsQuery, useLazyGetDetailedLogsQuery, useGetProjectTaskSummaryQuery, useGetReactReportsSummaryQuery, useGetReactReportsListQuery, useLazyGetReactReportsListQuery, useGetReactReportsTrendQuery } = reportsApi;
