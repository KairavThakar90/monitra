import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

export interface ManualTimeEntryCreate {
  project_id: number;
  task_id: number;
  work_date: string;
  total_seconds: number;
  is_billable?: boolean;
  description?: string;
}

export interface ManualTimeEntryRead {
  id: number;
  organization_id: number;
  user_id: number;
  project_id: number;
  task_id: number;
  work_date: string;
  start_time: string;
  end_time: string;
  total_seconds: number;
  description: string | null;
  is_billable: boolean;
  approval_status: string;
  approved_by: number | null;
  approved_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ManualTimeEntryRequestCreate {
  project_id: number;
  task_id: number;
  work_date: string;
  total_seconds: number;
  user_id?: number;
  start_time?: string;
  end_time?: string;
  description?: string;
  is_billable?: boolean;
}

export interface ManualTimeEntryRequest {
  id: number;
  user_id: number;
  project_id: number;
  task_id: number;
  work_date: string;
  start_time: string;
  end_time: string;
  total_seconds: number;
  description: string;
  is_billable: boolean;
  approval_status: 'pending' | 'approved' | 'rejected';
  approved_by: number | null;
  approved_at: string | null;
  mirrored_time_entry_id: number | null;
  member_name: string;
  member_email: string;
  project_name: string;
  task_name: string;
  has_conflict: boolean;
}

export interface GetManualTimeEntryRequestsArgs {
  approval_status?: 'pending' | 'approved' | 'rejected';
  project_id?: number;
  task_id?: number;
  user_id?: number;
  start_date?: string;
  end_date?: string;
  search?: string;
  page?: number;
  limit?: number;
}

export interface GetManualTimeEntryRequestsResponse {
  items: ManualTimeEntryRequest[];
  pagination: {
    page: number;
    limit: number;
    total: number;
    total_pages: number;
  };
}

/**
 * Everything that reports tracked time -- the day list, the per-employee
 * detail, the dashboard, every reports query -- provides some `TimeTracking`
 * tag. Invalidating the bare type reaches all of them; invalidating only
 * `{ TimeTracking, LIST }` refreshed the day list and left the KPI cards and
 * report totals beside it showing the old figure until they went stale.
 */
const TRACKED_TIME_CHANGED = ['TimeTracking' as const];

export const manualTimeEntryApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    createManualTimeEntry: builder.mutation<ManualTimeEntryRead, ManualTimeEntryCreate>({
      query: (body) => ({ url: ENDPOINTS.MANUAL_TIME_ENTRIES.BASE, method: 'POST', body }),
      invalidatesTags: TRACKED_TIME_CHANGED,
    }),
    createManualTimeEntryRequest: builder.mutation<ManualTimeEntryRead, ManualTimeEntryRequestCreate>({
      query: (body) => ({ url: ENDPOINTS.MANUAL_TIME_ENTRY_REQUESTS.BASE, method: 'POST', body }),
      invalidatesTags: [...TRACKED_TIME_CHANGED, { type: 'ManualTimeEntry', id: 'LIST' }],
    }),
    getManualTimeEntryRequests: builder.query<GetManualTimeEntryRequestsResponse, GetManualTimeEntryRequestsArgs>({
      query: (params) => {
        const queryParams = new URLSearchParams();
        Object.entries(params).forEach(([key, value]) => {
          if (value !== undefined && value !== null && value !== '') {
            queryParams.append(key, String(value));
          }
        });
        return { url: `${ENDPOINTS.MANUAL_TIME_ENTRY_REQUESTS.BASE}?${queryParams.toString()}` };
      },
      providesTags: [{ type: 'ManualTimeEntry', id: 'LIST' }],
    }),
    approveManualTimeEntryRequest: builder.mutation<void, number>({
      query: (id) => ({ url: ENDPOINTS.MANUAL_TIME_ENTRY_REQUESTS.APPROVE(id), method: 'PATCH' }),
      invalidatesTags: [...TRACKED_TIME_CHANGED, { type: 'ManualTimeEntry', id: 'LIST' }],
    }),
    rejectManualTimeEntryRequest: builder.mutation<void, number>({
      query: (id) => ({ url: ENDPOINTS.MANUAL_TIME_ENTRY_REQUESTS.REJECT(id), method: 'PATCH' }),
      invalidatesTags: [{ type: 'ManualTimeEntry', id: 'LIST' }],
    }),
    deleteManualTimeEntryRequest: builder.mutation<void, number>({
      query: (id) => ({ url: ENDPOINTS.MANUAL_TIME_ENTRY_REQUESTS.DELETE(id), method: 'DELETE' }),
      invalidatesTags: [{ type: 'ManualTimeEntry', id: 'LIST' }],
    }),
  }),
});

export const { 
  useCreateManualTimeEntryMutation,
  useCreateManualTimeEntryRequestMutation,
  useGetManualTimeEntryRequestsQuery,
  useApproveManualTimeEntryRequestMutation,
  useRejectManualTimeEntryRequestMutation,
  useDeleteManualTimeEntryRequestMutation,
} = manualTimeEntryApi;
