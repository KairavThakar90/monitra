import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

/**
 * Feedback & Help.
 *
 * Feedback is *submitted* from the Monitra desktop client; the dashboard reads
 * it and — for an administrator only — moves it through the support workflow.
 *
 * The queries differ only in who they are allowed to read. `getMyFeedback`
 * takes no user id: the backend derives the owner from the access token, so
 * there is nothing a caller could change to see somebody else's messages.
 * `getAllFeedback` is answered only for Admin, HR and Leader and is scoped to
 * the caller's own organization.
 *
 * `updateFeedbackStatus` is the one mutation, and its argument list is the
 * point: an id and a status. It carries no recipient, because the employee who
 * gets the email is resolved from the feedback row server-side — the dashboard
 * does not know the address and has no way to supply one. The endpoint refuses
 * any caller who is not an administrator, so hiding the buttons from HR is a
 * courtesy to HR rather than the thing that stops them.
 */

export type FeedbackCategory =
  | 'suggestion'
  | 'report_a_problem'
  | 'general_feedback'
  | 'need_help'
  | 'account_login_issue'
  | 'other';

/**
 * The support workflow's states. `new` is where every submission starts;
 * `in_progress` and `resolved` are what the two Admin buttons produce.
 * `reviewing` and `closed` exist in the backend enum and nothing sets them —
 * they are listed so an unexpected value is a typed case rather than a crash.
 */
export type FeedbackStatus = 'new' | 'reviewing' | 'in_progress' | 'resolved' | 'closed';

export interface Feedback {
  id: number;
  employee_id: number;
  employee_name: string;
  category: FeedbackCategory;
  message: string;
  status: FeedbackStatus;
  created_at: string;
  updated_at: string | null;
}

/** The status update's response: the refreshed row, plus what it caused. */
export interface FeedbackStatusUpdateResponse extends Feedback {
  /**
   * False when the row was already in the requested state and no second email
   * was queued — which is what a double-click or a replayed request produces.
   * The UI words its toast from this rather than assuming a send.
   */
  notification_queued: boolean;
}

export interface FeedbackStatusUpdateArgs {
  id: number;
  /** `in_progress` is the Working button; `resolved` is the other one. */
  status: 'in_progress' | 'resolved';
}

export interface FeedbackListResponse {
  items: Feedback[];
  page: number;
  limit: number;
  total: number;
  pages: number;
}

export interface FeedbackListArgs {
  page?: number;
  limit?: number;
  category?: FeedbackCategory | null;
}

/** Wire values are snake_case; these are what the table shows. */
export const FEEDBACK_CATEGORY_LABELS: Record<FeedbackCategory, string> = {
  suggestion: 'Suggestion',
  report_a_problem: 'Report a problem',
  general_feedback: 'General feedback',
  need_help: 'Need help',
  account_login_issue: 'Account / login issue',
  other: 'Other',
};

const listQuery = (base: string, params: FeedbackListArgs) => {
  const query = new URLSearchParams();
  query.set('page', String(params.page ?? 1));
  query.set('limit', String(params.limit ?? 20));
  if (params.category) query.set('category', params.category);
  return `${base}?${query.toString()}`;
};

/** The largest page the backend will serve; see the `limit` bound on both routes. */
const MAX_PAGE_SIZE = 100;

/**
 * Read every page of a feedback list and return the rows as one array.
 *
 * The Feedback screens filter by search term, date range and submitter, and the
 * API offers none of those -- `page`, `limit` and `category` are the only
 * parameters either route takes. Filtering one server page client-side would
 * silently answer "no results" for a match sitting on page two, so the whole
 * set is loaded once and filtered here instead. This mirrors `getAllProjects`
 * in `projectsApi`, which pages the same way for the same reason.
 *
 * Page one is fetched first because only it can say how many pages there are;
 * the rest go out together rather than in series.
 */
const fetchEveryPage = async (
  base: string,
  baseQuery: (arg: string) => Promise<{ data?: unknown; error?: unknown }>,
) => {
  const first = await baseQuery(`${base}?page=1&limit=${MAX_PAGE_SIZE}`);
  if (first.error) return { error: first.error as never };

  const firstPage = first.data as FeedbackListResponse;
  const totalPages = firstPage.pages || 1;
  const rest = await Promise.all(
    Array.from({ length: Math.max(0, totalPages - 1) }, (_, index) =>
      baseQuery(`${base}?page=${index + 2}&limit=${MAX_PAGE_SIZE}`),
    ),
  );

  const items = [...firstPage.items];
  for (const page of rest) {
    // One failed page must not be reported as a short list -- a filter would
    // then be applied to rows the user cannot see are missing.
    if (page.error) return { error: page.error as never };
    items.push(...(page.data as FeedbackListResponse).items);
  }
  return { data: items };
};

export const feedbackApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getMyFeedback: builder.query<FeedbackListResponse, FeedbackListArgs>({
      query: (params) => listQuery(ENDPOINTS.FEEDBACK.MY, params),
      providesTags: [{ type: 'Feedback' as const, id: 'MINE' }],
    }),
    getAllFeedback: builder.query<FeedbackListResponse, FeedbackListArgs>({
      query: (params) => listQuery(ENDPOINTS.FEEDBACK.BASE, params),
      providesTags: [{ type: 'Feedback' as const, id: 'ALL' }],
    }),

    /** Every feedback the caller may read, unpaginated, for client-side filtering. */
    getAllFeedbackItems: builder.query<Feedback[], void>({
      queryFn: (_arg, _api, _extraOptions, baseQuery) =>
        fetchEveryPage(ENDPOINTS.FEEDBACK.BASE, baseQuery as never) as never,
      providesTags: [{ type: 'Feedback' as const, id: 'ALL' }],
    }),

    /** Everything the caller submitted, unpaginated, for client-side filtering. */
    getMyFeedbackItems: builder.query<Feedback[], void>({
      queryFn: (_arg, _api, _extraOptions, baseQuery) =>
        fetchEveryPage(ENDPOINTS.FEEDBACK.MY, baseQuery as never) as never,
      providesTags: [{ type: 'Feedback' as const, id: 'MINE' }],
    }),

    /**
     * Mark one feedback Working or Resolved. Admin only, enforced server-side.
     *
     * Both list tags are invalidated rather than the row being patched in
     * place. The server owns the workflow — it can legitimately answer with a
     * status the browser did not ask for, and it decides whether an email was
     * actually queued — so the list is re-read from it instead of the UI
     * writing what it assumed. `MINE` goes too: an administrator's own
     * feedback appears in both lists, and leaving one stale would show the
     * same row in two different states on two screens.
     */
    updateFeedbackStatus: builder.mutation<FeedbackStatusUpdateResponse, FeedbackStatusUpdateArgs>({
      query: ({ id, status }) => ({
        url: ENDPOINTS.FEEDBACK.STATUS(id),
        method: 'PATCH',
        body: { status },
      }),
      invalidatesTags: [
        { type: 'Feedback' as const, id: 'ALL' },
        { type: 'Feedback' as const, id: 'MINE' },
      ],
    }),
  }),
});

export const {
  useGetMyFeedbackQuery,
  useGetAllFeedbackQuery,
  useGetAllFeedbackItemsQuery,
  useGetMyFeedbackItemsQuery,
  useUpdateFeedbackStatusMutation,
} = feedbackApi;
