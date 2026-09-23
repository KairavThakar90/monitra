import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

export interface ClientProjectRef {
  id: number;
  project_name: string;
}

/** What a client may see, beyond the shared projects' own name/description
 * (never gated). Mirrors `share_*` on the backend's `Client` model. */
export interface ClientPermissions {
  share_member_details: boolean;
  share_screenshots: boolean;
  share_tasks: boolean;
  share_timing: boolean;
}

export const DEFAULT_CLIENT_PERMISSIONS: ClientPermissions = {
  share_member_details: true,
  share_screenshots: false,
  share_tasks: true,
  share_timing: true,
};

export interface ClientListItem {
  id: number;
  name: string;
  email: string;
  status: 'pending' | 'active' | 'rejected' | 'deactivated';
  projects: ClientProjectRef[];
  permissions: ClientPermissions;
  created_at: string;
}

export interface ClientListResponse {
  items: ClientListItem[];
  pagination: { page: number; limit: number; total: number; total_pages: number };
}

export interface CreateClientInvitationPayload {
  email: string;
  project_ids: number[];
  permissions: ClientPermissions;
}

export const clientsApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getClients: builder.query<ClientListResponse, { page?: number; limit?: number }>({
      query: ({ page = 1, limit = 20 }) => `${ENDPOINTS.CLIENTS.BASE}?page=${page}&limit=${limit}`,
      providesTags: (result) =>
        result
          ? [
              ...result.items.map(({ id }) => ({ type: 'Client' as const, id })),
              { type: 'Client' as const, id: 'LIST' },
            ]
          : [{ type: 'Client' as const, id: 'LIST' }],
    }),

    createClientInvitation: builder.mutation<{ id: number; email: string; status: string }, CreateClientInvitationPayload>({
      query: (body) => ({ url: ENDPOINTS.CLIENTS.INVITATIONS, method: 'POST', body }),
      invalidatesTags: [{ type: 'Client', id: 'LIST' }],
    }),

    updateClientAccess: builder.mutation<
      { id: number; status: string },
      { id: number; project_ids: number[]; permissions: ClientPermissions }
    >({
      query: ({ id, project_ids, permissions }) => ({
        url: ENDPOINTS.CLIENTS.ACCESS(id),
        method: 'PATCH',
        body: { project_ids, permissions },
      }),
      invalidatesTags: (_result, _error, { id }) => [{ type: 'Client', id }, { type: 'Client', id: 'LIST' }],
    }),

    resendClientInvitation: builder.mutation<{ id: number; status: string }, number>({
      query: (id) => ({ url: ENDPOINTS.CLIENTS.RESEND_INVITATION(id), method: 'POST' }),
      invalidatesTags: (_result, _error, id) => [{ type: 'Client', id }, { type: 'Client', id: 'LIST' }],
    }),

    deactivateClient: builder.mutation<{ id: number; status: string }, number>({
      query: (id) => ({ url: ENDPOINTS.CLIENTS.DEACTIVATE(id), method: 'POST' }),
      invalidatesTags: (_result, _error, id) => [{ type: 'Client', id }, { type: 'Client', id: 'LIST' }],
    }),
  }),
});

export const {
  useGetClientsQuery,
  useCreateClientInvitationMutation,
  useUpdateClientAccessMutation,
  useResendClientInvitationMutation,
  useDeactivateClientMutation,
} = clientsApi;
