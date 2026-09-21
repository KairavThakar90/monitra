import { baseApi } from './baseApi';
import { patchEveryCachedQuery } from './optimistic';
import { ENDPOINTS } from '../../api/endpoints';

export interface Member {
  id: number;
  name: string;
  email: string;
  role: string;
  status: string;
  date_of_joining: string | null;
  date_of_birth: string | null;
  designation: string;
  idle_enabled?: boolean;
  idle_minutes?: number;
  capture_frequency?: number;
  created_at?: string;
  updated_at?: string;
  organization?: { id: number; name: string };
}

export interface GetMembersResponse {
  items: Member[];
  page: number;
  limit: number;
  total: number;
  pages: number;
}

export type GetMembersArgs = {
  page?: number;
  limit?: number;
  role?: string;
  status?: string;
  search?: string;
};

/** True when a member still belongs in a list fetched with `arg`'s filters. */
const matchesFilters = (member: Member, arg: GetMembersArgs | undefined) => {
  const role = arg?.role;
  const status = arg?.status;
  if (role && role !== 'All' && (member.role || '').toLowerCase() !== role.toLowerCase()) return false;
  if (status && status !== 'All' && (member.status || '').toLowerCase() !== status.toLowerCase()) return false;
  return true;
};

/** Removes a row that no longer matches, otherwise leaves it updated in place. */
const reconcileRow = (draft: GetMembersResponse, index: number, arg: GetMembersArgs | undefined) => {
  if (matchesFilters(draft.items[index], arg)) return;
  draft.items.splice(index, 1);
  if (typeof draft.total === 'number') draft.total = Math.max(0, draft.total - 1);
};


export interface DailyActivity {
  date: string;
  keyboard_strokes: number;
  mouse_clicks: number;
  mouse_movements: number;
  activity_percentage: number;
}

export interface ApplicationUsageItem {
  application_name: string;
  duration_seconds: number;
  duration: string;
  usage_percentage: number;
}

export interface ApplicationUsage {
  date: string;
  applications: ApplicationUsageItem[];
}

export interface UrlUsageItem {
  browser_name: string;
  domain: string;
  url: string;
  page_title: string;
  duration_seconds: number;
  duration: string;
  usage_percentage: number;
}

export interface UrlUsage {
  date: string;
  urls: UrlUsageItem[];
}

export interface GetMemberDetailsResponse {
  member: Member & { organization?: { id: number; name: string } };
  start_date: string;
  end_date: string;
  daily_activity: DailyActivity[];
  application_usage: ApplicationUsage[];
  url_usage: UrlUsage[];
}

export interface GetMemberDetailsArgs {
  id: number;
  start_date?: string;
  end_date?: string;
}

export const membersApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getMemberDetails: builder.query<GetMemberDetailsResponse, GetMemberDetailsArgs>({
      query: ({ id, start_date, end_date }) => {
        const params = new URLSearchParams();
        if (start_date) params.append('start_date', start_date);
        if (end_date) params.append('end_date', end_date);
        return {
          url: `${ENDPOINTS.MEMBERS.GET_BY_ID(id)}/details?${params.toString()}`,
          method: 'GET',
        };
      },
      providesTags: (_result, _error, arg) => [{ type: 'Member', id: arg.id }],
    }),

    getMembers: builder.query<GetMembersResponse, GetMembersArgs>({
      query: (params) => {
        const queryParams = new URLSearchParams();
        if (params.page) queryParams.append('page', params.page.toString());
        if (params.limit) queryParams.append('limit', params.limit.toString());
        if (params.role && params.role !== 'All') queryParams.append('role', params.role.toLowerCase());
        if (params.status && params.status !== 'All') queryParams.append('status', params.status.toLowerCase());
        if (params.search) queryParams.append('search', params.search);

        return { url: `${ENDPOINTS.MEMBERS.GET_ALL}?${queryParams.toString()}` };
      },
      providesTags: (result) =>
        result
          ? [...result.items.map(({ id }) => ({ type: 'Member' as const, id })), { type: 'Member' as const, id: 'LIST' }]
          : [{ type: 'Member' as const, id: 'LIST' }],
    }),

    
    getAllMembers: builder.query<Member[], void>({
      async queryFn(_arg, _api, _extraOptions, baseQuery) {
        const firstResult = await baseQuery(`${ENDPOINTS.MEMBERS.GET_ALL}?page=1&limit=100`);
        if (firstResult.error) return { error: firstResult.error };

        const firstResponse = firstResult.data as GetMembersResponse;
        const totalPages = firstResponse.pages || 1;
        const remainingResults = await Promise.all(
          Array.from({ length: Math.max(0, totalPages - 1) }, (_, index) =>
            baseQuery(`${ENDPOINTS.MEMBERS.GET_ALL}?page=${index + 2}&limit=100`),
          ),
        );
        const failedResult = remainingResults.find((result) => result.error);
        if (failedResult?.error) return { error: failedResult.error };

        const members = [
          ...(firstResponse.items || []),
          ...remainingResults.flatMap((result) => ((result.data as GetMembersResponse).items || [])),
        ];

        return { data: members };
      },
    }),

    createMember: builder.mutation<Member, Partial<Member>>({
      query: (body) => ({ url: ENDPOINTS.MEMBERS.CREATE, method: 'POST', body }),
      // The members list has no explicit ordering server-side, so we cannot know
      // which page a new row lands on. This is the one mutation that still needs
      // the list refetched; the screen stays interactive while it happens.
      invalidatesTags: [{ type: 'Member', id: 'LIST' }, { type: 'Team', id: 'LIST' }],
    }),

    updateMember: builder.mutation<Member, { id: number; body: Partial<Member> }>({
      query: ({ id, body }) => ({ url: ENDPOINTS.MEMBERS.UPDATE(id), method: 'PATCH', body }),
      // Team summaries count members by role/status, so they are marked stale
      // and refresh the next time a Teams screen is opened — no request now.
      invalidatesTags: [{ type: 'Team', id: 'LIST' }],
      async onQueryStarted({ id, body }, { dispatch, getState, queryFulfilled }) {
        const optimistic = patchEveryCachedQuery({ dispatch, getState }, 'getMembers', (draft, arg) => {
          const index = draft.items?.findIndex((m: Member) => m.id === id) ?? -1;
          if (index < 0) return;
          Object.assign(draft.items[index], body);
          reconcileRow(draft, index, arg);
        });

        try {
          const { data } = await queryFulfilled;
          // Replace the guess with what the server actually stored.
          patchEveryCachedQuery({ dispatch, getState }, 'getMembers', (draft, arg) => {
            const index = draft.items?.findIndex((m: Member) => m.id === id) ?? -1;
            if (index < 0) return;
            draft.items[index] = data;
            reconcileRow(draft, index, arg);
          });
        } catch {
          optimistic.undo();
        }
      },
    }),

    // NOTE: the backend deactivates rather than hard-deletes (it returns the
    // member with status "inactive"), so that is what we reflect locally.
    deleteMember: builder.mutation<Member, number>({
      query: (id) => ({ url: ENDPOINTS.MEMBERS.DELETE(id), method: 'DELETE' }),
      invalidatesTags: [{ type: 'Team', id: 'LIST' }],
      async onQueryStarted(id, { dispatch, getState, queryFulfilled }) {
        const optimistic = patchEveryCachedQuery({ dispatch, getState }, 'getMembers', (draft, arg) => {
          const index = draft.items?.findIndex((m: Member) => m.id === id) ?? -1;
          if (index < 0) return;
          draft.items[index].status = 'inactive';
          reconcileRow(draft, index, arg);
        });

        try {
          await queryFulfilled;
        } catch {
          optimistic.undo();
        }
      },
    }),
  }),
});

export const {
  useGetMemberDetailsQuery,
  useGetMembersQuery,
  useGetAllMembersQuery,
  useCreateMemberMutation,
  useUpdateMemberMutation,
  useDeleteMemberMutation,
} = membersApi;
