import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

export interface ClientProjectRef {
  id: number;
  project_name: string;
}

export interface ClientListItem {
  id: number;
  name: string;
  email: string;
  status: 'pending' | 'active' | 'rejected' | 'deactivated';
  projects: ClientProjectRef[];
  created_at: string;
}

export interface ClientListResponse {
  items: ClientListItem[];
  pagination: { page: number; limit: number; total: number; total_pages: number };
}

export interface CreateClientInvitationPayload {
  email: string;
  project_ids: number[];
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

    updateClientProjects: builder.mutation<{ id: number; status: string }, { id: number; project_ids: number[] }>({
      query: ({ id, project_ids }) => ({
        url: ENDPOINTS.CLIENTS.PROJECTS(id),
        method: 'PATCH',
        body: { project_ids },
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
  useUpdateClientProjectsMutation,
  useResendClientInvitationMutation,
  useDeactivateClientMutation,
} = clientsApi;
