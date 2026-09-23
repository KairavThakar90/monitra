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
  start_date: string | null;
  total_tracked_seconds: number;
  total_tracked_hours: number;
  total_members: number;
  tasks: MyProjectTask[];
  members: MyProjectMember[];
}

export const clientPortalApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getMyProjects: builder.query<{ items: MyProjectSummary[] }, void>({
      query: () => ENDPOINTS.CLIENTS.MY_PROJECTS,
      providesTags: [{ type: 'ClientProject', id: 'LIST' }],
    }),

    getMyProjectDetail: builder.query<MyProjectDetail, number>({
      query: (projectId) => ENDPOINTS.CLIENTS.MY_PROJECT_BY_ID(projectId),
      providesTags: (_result, _error, projectId) => [{ type: 'ClientProject', id: projectId }],
    }),
  }),
});

export const { useGetMyProjectsQuery, useGetMyProjectDetailQuery } = clientPortalApi;
