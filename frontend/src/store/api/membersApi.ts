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
  /** The Members directory's Allow / Not allow switch for adding tasks. Absent on an older backend means allowed. */
  can_add_tasks?: boolean;
  /** The Members directory's Allow / Exclude switch for signing in. Absent on an older backend means allowed. */
  can_login?: boolean;
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

/**
 * Narrows the directory by one of the two access switches: `allowed` keeps the
 * members whose switch is on, `not_allowed` the ones who are excluded, `All`
 * does not filter. Only an explicit `false` excludes, as everywhere else.
 */
export type AccessFilter = 'All' | 'allowed' | 'not_allowed';

export type GetMembersArgs = {
  page?: number;
  limit?: number;
  role?: string;
  status?: string;
  search?: string;
  can_login?: AccessFilter;
  can_add_tasks?: AccessFilter;
};

export interface MemberAccessArgs {
  member_ids: number[];
  can_login?: boolean;
  can_add_tasks?: boolean;
}

/** Headcounts of active members, for the numbers beside the Add Task and Login columns. */
export interface MemberAccessSummary {
  add_task_allowed: number;
  login_allowed: number;
  active_members: number;
}

/**
 * The summary's own cache tag. Anything that can change who is allowed, or who
 * is active, invalidates it; the member lists never provide it, so refreshing
 * the counts does not refetch a single page of the directory.
 */
const ACCESS_SUMMARY_TAG = { type: 'Member' as const, id: 'ACCESS_SUMMARY' };

export interface MemberAccessResult {
  updated: Member[];
  failed: { id: number; detail: string }[];
}

/** True when a switch (`undefined` counts as on) is on the side `filter` asks for. */
const matchesAccess = (switchValue: boolean | undefined, filter: AccessFilter | undefined) => {
  if (!filter || filter === 'All') return true;
  const allowed = switchValue !== false;
  return filter === 'allowed' ? allowed : !allowed;
};

/** True when a member still belongs in a list fetched with `arg`'s filters. */
const matchesFilters = (member: Member, arg: GetMembersArgs | undefined) => {
  const role = arg?.role;
  const status = arg?.status;
  if (role && role !== 'All' && (member.role || '').toLowerCase() !== role.toLowerCase()) return false;
  if (status && status !== 'All' && (member.status || '').toLowerCase() !== status.toLowerCase()) return false;
  if (!matchesAccess(member.can_login, arg?.can_login)) return false;
  if (!matchesAccess(member.can_add_tasks, arg?.can_add_tasks)) return false;
  return true;
};

/**
 * Removes a row that no longer matches, otherwise leaves it updated in place.
 * Returns true when it removed the row.
 */
const reconcileRow = (draft: GetMembersResponse, index: number, arg: GetMembersArgs | undefined) => {
  if (matchesFilters(draft.items[index], arg)) return false;
  draft.items.splice(index, 1);
  if (typeof draft.total === 'number') draft.total = Math.max(0, draft.total - 1);
  return true;
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
        if (params.can_login === 'allowed') queryParams.append('can_login', 'true');
        else if (params.can_login === 'not_allowed') queryParams.append('can_login', 'false');
        if (params.can_add_tasks === 'allowed') queryParams.append('can_add_tasks', 'true');
        else if (params.can_add_tasks === 'not_allowed') queryParams.append('can_add_tasks', 'false');

        return { url: `${ENDPOINTS.MEMBERS.GET_ALL}?${queryParams.toString()}` };
      },
      providesTags: (result) =>
        result
          ? [...result.items.map(({ id }) => ({ type: 'Member' as const, id })), { type: 'Member' as const, id: 'LIST' }]
          : [{ type: 'Member' as const, id: 'LIST' }],
    }),

    
    getMemberAccessSummary: builder.query<MemberAccessSummary, void>({
      query: () => ({ url: ENDPOINTS.MEMBERS.ACCESS_SUMMARY }),
      providesTags: [ACCESS_SUMMARY_TAG],
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
      // So a member who was just created or deleted appears in, or leaves, every
      // picker built from this list instead of lingering until the cache expires.
      providesTags: [{ type: 'Member' as const, id: 'LIST' }],
    }),

    createMember: builder.mutation<Member, Partial<Member>>({
      query: (body) => ({ url: ENDPOINTS.MEMBERS.CREATE, method: 'POST', body }),
      // The members list has no explicit ordering server-side, so we cannot know
      // which page a new row lands on. This is the one mutation that still needs
      // the list refetched; the screen stays interactive while it happens.
      invalidatesTags: [{ type: 'Member', id: 'LIST' }, { type: 'Team', id: 'LIST' }, ACCESS_SUMMARY_TAG],
    }),

    updateMember: builder.mutation<Member, { id: number; body: Partial<Member> }>({
      query: ({ id, body }) => ({ url: ENDPOINTS.MEMBERS.UPDATE(id), method: 'PATCH', body }),
      // Team summaries count members by role/status, so they are marked stale
      // and refresh the next time a Teams screen is opened — no request now.
      invalidatesTags: [{ type: 'Team', id: 'LIST' }, ACCESS_SUMMARY_TAG],
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

    // Turns the sign-in and/or Add Task switch on or off for one or more
    // members. Rows flip at once; the server's answer then replaces the guess
    // for each member it saved, and any it refused are put back.
    updateMemberAccess: builder.mutation<MemberAccessResult, MemberAccessArgs>({
      query: (body) => ({ url: ENDPOINTS.MEMBERS.UPDATE_ACCESS, method: 'PATCH', body }),
      // Re-read the counts from the server rather than guessing them: a bulk
      // change can be partly refused, and the server is what counts.
      invalidatesTags: [ACCESS_SUMMARY_TAG],
      async onQueryStarted({ member_ids, ...switches }, { dispatch, getState, queryFulfilled }) {
        const ids = new Set(member_ids);
        // A list filtered by a switch (Login: Allowed) no longer holds a member
        // whose switch just moved, so that row leaves it. Walked backwards so a
        // removal does not shift the rows still to visit.
        let leftAFilteredList = false;
        const optimistic = patchEveryCachedQuery({ dispatch, getState }, 'getMembers', (draft, arg) => {
          for (let index = (draft.items?.length ?? 0) - 1; index >= 0; index -= 1) {
            if (!ids.has(draft.items[index].id)) continue;
            Object.assign(draft.items[index], switches);
            if (reconcileRow(draft, index, arg)) leftAFilteredList = true;
          }
        });

        try {
          const { data } = await queryFulfilled;
          const saved = new Map(data.updated.map((m) => [m.id, m]));
          // Refused rows go back to what the server holds; saved rows take its copy.
          optimistic.undo();
          leftAFilteredList = false;
          patchEveryCachedQuery({ dispatch, getState }, 'getMembers', (draft, arg) => {
            for (let index = (draft.items?.length ?? 0) - 1; index >= 0; index -= 1) {
              const fresh = saved.get(draft.items[index].id);
              if (!fresh) continue;
              draft.items[index] = fresh;
              if (reconcileRow(draft, index, arg)) leftAFilteredList = true;
            }
          });
          // Rows that left a filtered list moved every later row up a place, so
          // the other cached pages no longer line up with it: read them again.
          if (leftAFilteredList) dispatch(baseApi.util.invalidateTags([{ type: 'Member', id: 'LIST' }]));
        } catch {
          optimistic.undo();
        }
      },
    }),

    // The backend deletes the member outright (204, no body) -- "Inactive" is an
    // edit, not a delete. The row leaves every cached list at once and comes
    // back if the server refuses (a project they lead, a running timer, ...).
    // The lists are then refetched: removing a row shifts every later row up a
    // place, so the cached pages after this one no longer line up.
    deleteMember: builder.mutation<void, number>({
      query: (id) => ({ url: ENDPOINTS.MEMBERS.DELETE(id), method: 'DELETE' }),
      invalidatesTags: [{ type: 'Member', id: 'LIST' }, { type: 'Team', id: 'LIST' }, ACCESS_SUMMARY_TAG],
      async onQueryStarted(id, { dispatch, getState, queryFulfilled }) {
        const optimistic = patchEveryCachedQuery({ dispatch, getState }, 'getMembers', (draft) => {
          const index = draft.items?.findIndex((m: Member) => m.id === id) ?? -1;
          if (index < 0) return;
          draft.items.splice(index, 1);
          if (typeof draft.total === 'number') draft.total = Math.max(0, draft.total - 1);
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
  useGetMemberAccessSummaryQuery,
  useLazyGetMembersQuery,
  useGetAllMembersQuery,
  useCreateMemberMutation,
  useUpdateMemberMutation,
  useUpdateMemberAccessMutation,
  useDeleteMemberMutation,
} = membersApi;
