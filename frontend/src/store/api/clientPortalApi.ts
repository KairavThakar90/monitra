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

export interface DateRangeArg {
  start_date?: string;
  end_date?: string;
}

const withRange = (base: string, range?: DateRangeArg) => {
  if (!range?.start_date || !range?.end_date) return base;
  const params = new URLSearchParams({ start_date: range.start_date, end_date: range.end_date });
  return `${base}?${params.toString()}`;
};

export const clientPortalApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getMyProjects: builder.query<MyProjectsResponse, DateRangeArg | void>({
      query: (arg) => withRange(ENDPOINTS.CLIENTS.MY_PROJECTS, arg ?? undefined),
      providesTags: [{ type: 'ClientProject', id: 'LIST' }],
    }),

    getMyMemberHours: builder.query<MyMemberHoursResponse, DateRangeArg | void>({
      query: (arg) => withRange(ENDPOINTS.CLIENTS.MY_MEMBERS, arg ?? undefined),
      providesTags: [{ type: 'ClientProject', id: 'MEMBERS' }],
    }),

    getMyTaskHours: builder.query<MyTaskHoursResponse, DateRangeArg | void>({
      query: (arg) => withRange(ENDPOINTS.CLIENTS.MY_TASKS, arg ?? undefined),
      providesTags: [{ type: 'ClientProject', id: 'TASKS' }],
    }),

    getMyProjectDetail: builder.query<MyProjectDetail, { projectId: number } & DateRangeArg>({
      query: ({ projectId, ...range }) => withRange(ENDPOINTS.CLIENTS.MY_PROJECT_BY_ID(projectId), range),
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
