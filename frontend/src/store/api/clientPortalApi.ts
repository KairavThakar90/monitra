import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';
import type { ClientPermissions } from './clientsApi';
import type { ScreenshotMemberDays } from './screenshotsApi';

export type { ClientPermissions };

export interface MyProfile {
  name: string;
  email: string;
  permissions: ClientPermissions;
}

/** One assigned-roster entry on a shared project. */
export interface MyProjectRosterMember {
  id: number;
  name: string;
  designation: string | null;
}

export interface MyProjectSummary {
  id: number;
  project_name: string;
  description: string | null;
  status: string;
  deadline: string | null;
  project_start_date: string | null;
  /** When the project record was created. */
  created_date?: string | null;
  /** IST date work first started on the project (earliest tracked session,
   * all-time); null when nothing was ever tracked or Timing is withheld. */
  first_tracked_date?: string | null;
  total_tracked_seconds: number | null;
  total_tracked_hours: number | null;
  member_count: number | null;
  /** The assigned roster — the same people the admin sees on the project.
   * `[]` when Member Details is not shared. Optional so a response from a
   * backend predating the field renders as empty rather than crashing. */
  members?: MyProjectRosterMember[];
}

export interface MyProjectsResponse {
  start_date: string;
  end_date: string;
  permissions: ClientPermissions;
  items: MyProjectSummary[];
}

export interface MyMemberHours {
  id: number;
  name: string;
  designation: string | null;
  total_tracked_seconds: number | null;
  total_tracked_hours: number | null;
  project_count: number;
  /** The shared projects this member worked on in range, by name. Optional
   * so a response from a backend predating the field renders as empty. */
  project_names?: string[];
}

export interface MyMemberHoursResponse {
  start_date: string;
  end_date: string;
  permissions: ClientPermissions;
  items: MyMemberHours[];
}

export interface MyTaskHours {
  id: number;
  task_name: string;
  project_name: string | null;
  status?: string;
  created_date?: string | null;
  /** Assigned member's name; null when unassigned or Member Details is not shared. */
  assignee?: string | null;
  total_tracked_seconds: number | null;
  total_tracked_hours: number | null;
  /** Average timer activity over the range; null when Timing is withheld or
   * the timer recorded no samples (e.g. manual entries only). */
  activity_percentage?: number | null;
}

export interface MyTaskHoursResponse {
  start_date: string;
  end_date: string;
  permissions: ClientPermissions;
  items: MyTaskHours[];
}

/** Budget/usage figures shared by the billing read's project and task rows.
 * `total_hours`/`remaining_hours` are null when no budget is set (a task
 * without an estimate); `remaining_hours` goes negative when overspent. */
export interface MyBillingUsage {
  total_hours: number | null;
  used_seconds: number;
  used_hours: number;
  remaining_hours: number | null;
}

/** One member's share of a task's tracked time. */
export interface MyBillingTaskMember {
  id: number;
  name: string;
  used_seconds: number;
  used_hours: number;
}

export interface MyBillingTask extends MyBillingUsage {
  id: number;
  task_name: string;
  status: string;
  /** Who worked on this task and for how long. `[]` when the client was not
   * granted Member Details — identity is that flag's concern, so Billing
   * withholds it the same way the roster and member-hours reads do.
   * Optional so an older backend's response renders without the rows. */
  members?: MyBillingTaskMember[];
}

export interface MyBillingProject extends MyBillingUsage {
  id: number;
  project_name: string;
  status: string;
  billing_type: string;
  tasks: MyBillingTask[];
}

export interface MyBillingResponse {
  permissions: ClientPermissions;
  items: MyBillingProject[];
}

/** One IST day of a member's activity against shared projects. */
export interface MyMemberDay {
  date: string;
  /** Pre-formatted IST clock times ("09:12 AM"); last is null while a
   * session is still running. */
  first_activity: string;
  last_activity: string | null;
  session_count: number;
  total_tracked_seconds: number;
  total_tracked_hours: number;
}

export interface MyMemberDetail {
  id: number;
  name: string;
  designation: string | null;
  start_date: string;
  end_date: string;
  permissions: ClientPermissions;
  projects: { id: number; project_name: string; assigned: boolean }[];
  /** Date-wise activity, newest day first; `[]` when Timing is not shared. */
  days: MyMemberDay[];
  days_active: number | null;
  total_tracked_seconds: number | null;
  total_tracked_hours: number | null;
}

export interface MyProjectTask {
  id: number;
  task_name: string;
  status: string;
  total_tracked_seconds: number | null;
  total_tracked_hours: number | null;
}

export interface MyProjectMember {
  id: number;
  name: string;
  designation: string | null;
  total_tracked_seconds: number | null;
  total_tracked_hours: number | null;
}

export interface MyProjectDetail {
  id: number;
  project_name: string;
  description: string | null;
  status: string;
  deadline: string | null;
  project_start_date: string | null;
  start_date: string;
  end_date: string;
  permissions: ClientPermissions;
  total_tracked_seconds: number | null;
  total_tracked_hours: number | null;
  total_members: number;
  tasks: MyProjectTask[];
  members: MyProjectMember[];
}

export interface MyScreenshot {
  id: number;
  captured_at: string;
  width: number | null;
  height: number | null;
}

export interface MyScreenshotsResponse {
  start_date: string;
  end_date: string;
  permissions: ClientPermissions;
  items: MyScreenshot[];
}

/** The Screenshots page's own read: every shared project's captures across a
 * span, grouped member-then-day-then-window -- the same shape
 * `TIME_ENTRY_SCREENSHOTS.DAY` returns for staff, scoped to this client's
 * projects instead of visible members. */
export interface MyScreenshotsGridResponse {
  start_date: string;
  end_date: string;
  permissions: ClientPermissions;
  window_minutes: number;
  members: ScreenshotMemberDays[];
}

/** The date range plus the Project/Member filters every list-shaped
 * client-portal read accepts. `project_ids`/`member_ids` narrow the result;
 * omitted (or empty) means "no filter" — every shared project / every
 * member, the same as the staff `ProjectMultiSelect`/`MemberMultiSelect`. */
export interface ClientQueryArg {
  start_date?: string;
  end_date?: string;
  project_ids?: number[];
  member_ids?: number[];
}

const withQuery = (base: string, arg?: ClientQueryArg) => {
  if (!arg) return base;
  const params = new URLSearchParams();
  if (arg.start_date && arg.end_date) {
    params.set('start_date', arg.start_date);
    params.set('end_date', arg.end_date);
  }
  for (const id of arg.project_ids ?? []) params.append('project_ids', String(id));
  for (const id of arg.member_ids ?? []) params.append('member_ids', String(id));
  const query = params.toString();
  return query ? `${base}?${query}` : base;
};

export const clientPortalApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getMyProfile: builder.query<MyProfile, void>({
      query: () => ENDPOINTS.CLIENTS.MY_PROFILE,
      providesTags: [{ type: 'ClientProject', id: 'PROFILE' }],
    }),

    getMyProjects: builder.query<MyProjectsResponse, ClientQueryArg | void>({
      query: (arg) => withQuery(ENDPOINTS.CLIENTS.MY_PROJECTS, arg ?? undefined),
      providesTags: [{ type: 'ClientProject', id: 'LIST' }],
    }),

    getMyMemberHours: builder.query<MyMemberHoursResponse, ClientQueryArg | void>({
      query: (arg) => withQuery(ENDPOINTS.CLIENTS.MY_MEMBERS, arg ?? undefined),
      providesTags: [{ type: 'ClientProject', id: 'MEMBERS' }],
    }),

    getMyTaskHours: builder.query<MyTaskHoursResponse, ClientQueryArg | void>({
      query: (arg) => withQuery(ENDPOINTS.CLIENTS.MY_TASKS, arg ?? undefined),
      providesTags: [{ type: 'ClientProject', id: 'TASKS' }],
    }),

    getMyMemberDetail: builder.query<MyMemberDetail, { memberId: number } & ClientQueryArg>({
      query: ({ memberId, ...rest }) => withQuery(ENDPOINTS.CLIENTS.MY_MEMBER_BY_ID(memberId), rest),
      providesTags: (_result, _error, { memberId }) => [{ type: 'ClientProject', id: `member-${memberId}` }],
    }),

    getMyBilling: builder.query<MyBillingResponse, { project_ids?: number[] } | void>({
      query: (arg) => withQuery(ENDPOINTS.CLIENTS.MY_BILLING, arg ?? undefined),
      providesTags: [{ type: 'ClientProject', id: 'BILLING' }],
    }),

    getMyProjectDetail: builder.query<MyProjectDetail, { projectId: number } & ClientQueryArg>({
      query: ({ projectId, ...rest }) => withQuery(ENDPOINTS.CLIENTS.MY_PROJECT_BY_ID(projectId), rest),
      providesTags: (_result, _error, { projectId }) => [{ type: 'ClientProject', id: projectId }],
    }),

    getMyProjectScreenshots: builder.query<MyScreenshotsResponse, { projectId: number } & ClientQueryArg>({
      query: ({ projectId, ...rest }) => withQuery(ENDPOINTS.CLIENTS.MY_PROJECT_SCREENSHOTS(projectId), rest),
      providesTags: (_result, _error, { projectId }) => [{ type: 'ClientProject', id: `screenshots-${projectId}` }],
    }),

    getMyScreenshotsGrid: builder.query<MyScreenshotsGridResponse, ClientQueryArg | void>({
      query: (arg) => withQuery(ENDPOINTS.CLIENTS.MY_SCREENSHOTS, arg ?? undefined),
      providesTags: [{ type: 'ClientProject', id: 'SCREENSHOTS_GRID' }],
    }),
  }),
});

export const {
  useGetMyProfileQuery,
  useGetMyProjectsQuery,
  useGetMyMemberHoursQuery,
  useGetMyMemberDetailQuery,
  useGetMyTaskHoursQuery,
  useGetMyBillingQuery,
  useGetMyProjectDetailQuery,
  useGetMyProjectScreenshotsQuery,
  useGetMyScreenshotsGridQuery,
} = clientPortalApi;
