import { baseApi } from '../store/api/baseApi';
import { API_BASE_URL } from './utils';

// Types
export interface ScreenshotApplication {
  id: number;
  name: string;
  process_name: string;
  category: string;
  description?: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface ScreenshotUrl {
  id: number;
  name: string;
  domain: string;
  url_pattern: string;
  category: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface ScreenshotExclusion {
  id: number;
  user_id: number;
  application_id?: number;
  url_id?: number;
  exclusion_type: 'application' | 'url';
  is_excluded: boolean;
  created_at: string;
  updated_at: string;
}

export interface PrivacyConfig {
  applications: ScreenshotApplication[];
  urls: ScreenshotUrl[];
  excluded_applications: ScreenshotExclusion[];
  excluded_urls: ScreenshotExclusion[];
}

export const screenshotPrivacyApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getScreenshotApplications: builder.query<ScreenshotApplication[], void>({
      query: () => `${API_BASE_URL}/screenshot/applications`,
      providesTags: ['ScreenshotApplication'] as any,
    }),
    createScreenshotApplication: builder.mutation<ScreenshotApplication, Partial<ScreenshotApplication>>({
      query: (body) => ({
        url: `${API_BASE_URL}/screenshot/applications`,
        method: 'POST',
        body,
      }),
      invalidatesTags: ['ScreenshotApplication'] as any,
    }),
    updateScreenshotApplication: builder.mutation<ScreenshotApplication, Partial<ScreenshotApplication> & { id: number }>({
      query: ({ id, ...body }) => ({
        url: `${API_BASE_URL}/screenshot/applications/${id}`,
        method: 'PUT',
        body,
      }),
      invalidatesTags: ['ScreenshotApplication'] as any,
    }),
    deleteScreenshotApplication: builder.mutation<void, number>({
      query: (id) => ({
        url: `${API_BASE_URL}/screenshot/applications/${id}`,
        method: 'DELETE',
      }),
      invalidatesTags: ['ScreenshotApplication'] as any,
    }),

    getScreenshotUrls: builder.query<ScreenshotUrl[], void>({
      query: () => `${API_BASE_URL}/screenshot/urls`,
      providesTags: ['ScreenshotUrl'] as any,
    }),
    createScreenshotUrl: builder.mutation<ScreenshotUrl, Partial<ScreenshotUrl>>({
      query: (body) => ({
        url: `${API_BASE_URL}/screenshot/urls`,
        method: 'POST',
        body,
      }),
      invalidatesTags: ['ScreenshotUrl'] as any,
    }),
    updateScreenshotUrl: builder.mutation<ScreenshotUrl, Partial<ScreenshotUrl> & { id: number }>({
      query: ({ id, ...body }) => ({
        url: `${API_BASE_URL}/screenshot/urls/${id}`,
        method: 'PUT',
        body,
      }),
      invalidatesTags: ['ScreenshotUrl'] as any,
    }),
    deleteScreenshotUrl: builder.mutation<void, number>({
      query: (id) => ({
        url: `${API_BASE_URL}/screenshot/urls/${id}`,
        method: 'DELETE',
      }),
      invalidatesTags: ['ScreenshotUrl'] as any,
    }),

    getUserExclusions: builder.query<ScreenshotExclusion[], number>({
      query: (userId) => `${API_BASE_URL}/screenshot/users/${userId}/screenshot-exclusions`,
      providesTags: (result, error, arg) => [{ type: 'ScreenshotExclusion', id: arg }] as any,
    }),
    createUserExclusion: builder.mutation<ScreenshotExclusion, Partial<ScreenshotExclusion>>({
      query: ({ user_id, ...body }) => ({
        url: `${API_BASE_URL}/screenshot/users/${user_id}/screenshot-exclusions`,
        method: 'POST',
        body: { user_id, ...body },
      }),
      invalidatesTags: (result, error, arg) => [{ type: 'ScreenshotExclusion', id: arg.user_id }] as any,
    }),
    updateUserExclusion: builder.mutation<ScreenshotExclusion, Partial<ScreenshotExclusion> & { id: number, user_id: number }>({
      query: ({ id, user_id, ...body }) => ({
        url: `${API_BASE_URL}/screenshot/users/${user_id}/screenshot-exclusions/${id}`,
        method: 'PUT',
        body,
      }),
      invalidatesTags: (result, error, arg) => [{ type: 'ScreenshotExclusion', id: arg.user_id }] as any,
    }),
    deleteUserExclusion: builder.mutation<void, { id: number, user_id: number }>({
      query: ({ id, user_id }) => ({
        url: `${API_BASE_URL}/screenshot/users/${user_id}/screenshot-exclusions/${id}`,
        method: 'DELETE',
      }),
      invalidatesTags: (result, error, arg) => [{ type: 'ScreenshotExclusion', id: arg.user_id }] as any,
    }),

    getPrivacyConfig: builder.query<PrivacyConfig, void>({
      query: () => `${API_BASE_URL}/screenshot/privacy-config`,
    }),
  }),
  overrideExisting: false,
});

export const {
  useGetScreenshotApplicationsQuery,
  useCreateScreenshotApplicationMutation,
  useUpdateScreenshotApplicationMutation,
  useDeleteScreenshotApplicationMutation,
  useGetScreenshotUrlsQuery,
  useCreateScreenshotUrlMutation,
  useUpdateScreenshotUrlMutation,
  useDeleteScreenshotUrlMutation,
  useGetUserExclusionsQuery,
  useCreateUserExclusionMutation,
  useUpdateUserExclusionMutation,
  useDeleteUserExclusionMutation,
  useGetPrivacyConfigQuery,
} = screenshotPrivacyApi;
