import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

export interface MyProjectSummary {
  id: number;
  project_name: string;
  description: string | null;
  status: string;
  total_tracked_seconds: number;
  total_tracked_hours: number;
  member_count: number;
}

export interface MyProjectsResponse {
  start_date: string;
  end_date: string;
  items: MyProjectSummary[];
}

export interface MyMemberHours {
  id: number;
  name: string;
  total_tracked_seconds: number;
  total_tracked_hours: number;
  project_count: number;
}

export interface MyMemberHoursResponse {
  start_date: string;
  end_date: string;
  items: MyMemberHours[];
}

export interface MyTaskHours {
  id: number;
  task_name: string;
  project_name: string | null;
  total_tracked_seconds: number;
  total_tracked_hours: number;
}

export interface MyTaskHoursResponse {
  start_date: string;
  end_date: string;
  items: MyTaskHours[];
}

export interface MyProjectTask {
  id: number;
  task_name: string;
  status: string;
  total_tracked_seconds: number;
  total_tracked_hours: number;
}

export interface MyProjectMember {
  id: number;
  name: string;
  designation: string | null;
  total_tracked_seconds: number;
  total_tracked_hours: number;
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
  total_tracked_seconds: number;
  total_tracked_hours: number;
  total_members: number;
  tasks: MyProjectTask[];
  members: MyProjectMember[];
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

    getMyProjectDetail: builder.query<MyProjectDetail, { projectId: number } & ClientQueryArg>({
      query: ({ projectId, ...rest }) => withQuery(ENDPOINTS.CLIENTS.MY_PROJECT_BY_ID(projectId), rest),
      providesTags: (_result, _error, { projectId }) => [{ type: 'ClientProject', id: projectId }],
    }),
  }),
});

export const {
  useGetMyProjectsQuery,
  useGetMyMemberHoursQuery,
  useGetMyTaskHoursQuery,
  useGetMyProjectDetailQuery,
} = clientPortalApi;
