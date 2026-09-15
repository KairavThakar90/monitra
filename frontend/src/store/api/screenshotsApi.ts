import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

export interface TimeEntryScreenshot {
  id: number;
  organization_id: number;
  time_entry_id: number;
  captured_at: string;
  /**
   * Wherever the capturing client stored the image. It is free text in the
   * schema, so it may be an absolute URL or a path on the machine that took
   * it — a screen showing these must handle both rather than assume a URL.
   */
  file_path: string;
  monitor_number: number;
  /** Displays composited into this one image; 1 for a single-monitor capture. */
  display_count: number;
  created_at: string;
}

export interface GetScreenshotsArgs {
  time_entry_id?: number;
  /**
   * Ignored for callers without `time_entries:view_all` — the service pins
   * them to their own captures regardless of what is sent.
   */
  user_id?: number;
  limit?: number;
}

/** One capture as the timeline renders it. */
export interface ScreenshotView {
  id: number;
  captured_at: string;
  monitor_number: number;
  /**
   * Physical displays composited into this one image. A capture is always a
   * single screenshot; this says how many screens are inside it, and is what
   * the UI labels and lays out by. Rows predating merged capture read 1.
   */
  display_count: number;
  width: number | null;
  height: number | null;
  file_size_bytes: number | null;
  /**
   * Root-relative path the backend suggests for the image bytes. It carries no
   * API version prefix and needs the bearer token, so the UI fetches through
   * `ENDPOINTS.TIME_ENTRY_SCREENSHOTS.VIEW(id)` instead of putting this in a
   * plain `<img src>`.
   */
  view_url: string;
}

/**
 * One fixed-length window of a tracked day.
 *
 * `activity_percentage` describes this window alone. `activity_measured_seconds`
 * of 0 means nothing was measured in it — which is not the same as a measured
 * 0%, and the UI must not present the two identically.
 */
export interface ScreenshotTimelineWindow {
  window_start: string;
  window_end: string;
  activity_percentage: number;
  activity_measured_seconds: number;
  /**
   * Seconds of this window the member had a timer running, read from the time
   * entries themselves. Not the same as `activity_measured_seconds`: a window
   * can be worked in full and sampled for only part of it, so this is what a
   * "time worked" figure is built from and that one is the denominator the
   * activity percentage was measured over.
   */
  tracked_seconds: number;
  screenshots: ScreenshotView[];
  screenshot_count: number;
}

export interface ScreenshotTimelineResponse {
  success: boolean;
  window_minutes: number;
  windows: ScreenshotTimelineWindow[];
}

export interface GetScreenshotTimelineArgs {
  /** Defaults to the caller. Rejected by the backend for anyone out of scope. */
  user_id?: number;
  /** IST calendar date as `YYYY-MM-DD`. Defaults to today in IST. */
  date?: string;
}

/** One IST calendar day of one member's captures. */
export interface ScreenshotDay {
  /** `YYYY-MM-DD`, in IST. */
  date: string;
  windows: ScreenshotTimelineWindow[];
  screenshot_count: number;
  /** Time tracked across the whole IST day, not only the windows shown. */
  tracked_seconds: number;
}

/** One member's captures across the requested span, newest day first. */
export interface ScreenshotMemberDays {
  user_id: number;
  user_name: string;
  days: ScreenshotDay[];
  screenshot_count: number;
  /** Time tracked across every day in this response. */
  tracked_seconds: number;
}

export interface ScreenshotDayResponse {
  success: boolean;
  window_minutes: number;
  /** Only members who captured something in the span; never padded with empties. */
  members: ScreenshotMemberDays[];
}

export interface GetScreenshotDayArgs {
  /** First IST day, `YYYY-MM-DD`. */
  from?: string;
  /** Last IST day, inclusive. Both omitted reads today. */
  to?: string;
  /**
   * Narrows to one member. Only ever narrows — a member the caller may not see
   * is refused rather than silently swapped for their own row.
   */
  user_id?: number;
}

/** What `DELETE /time-entry-screenshots/{id}` answers on success. */
export interface ScreenshotDeleteResponse {
  success: boolean;
  message: string;
  screenshot_id: number;
}

export const screenshotsApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getScreenshots: builder.query<TimeEntryScreenshot[], GetScreenshotsArgs>({
      query: (params) => {
        const query = new URLSearchParams();
        query.set('limit', String(params.limit ?? 200));
        if (params.time_entry_id) query.set('time_entry_id', String(params.time_entry_id));
        if (params.user_id) query.set('user_id', String(params.user_id));
        return `${ENDPOINTS.TIME_ENTRY_SCREENSHOTS.BASE}?${query.toString()}`;
      },
      providesTags: [{ type: 'TimeTracking' as const, id: 'SCREENSHOTS' }],
    }),

    /**
     * Every visible member's span at once — what the admin screen opens on.
     * Scoped server-side to the same set as the single-member timeline, so it
     * can never return someone that endpoint would refuse.
     */
    getScreenshotDay: builder.query<ScreenshotDayResponse, GetScreenshotDayArgs>({
      query: (params) => {
        const query = new URLSearchParams();
        if (params.from) query.set('from', params.from);
        if (params.to) query.set('to', params.to);
        if (params.user_id) query.set('user_id', String(params.user_id));
        const suffix = query.toString();
        return `${ENDPOINTS.TIME_ENTRY_SCREENSHOTS.DAY}${suffix ? `?${suffix}` : ''}`;
      },
      providesTags: [{ type: 'TimeTracking' as const, id: 'SCREENSHOTS' }],
    }),

    getScreenshotTimeline: builder.query<ScreenshotTimelineResponse, GetScreenshotTimelineArgs>({
      query: (params) => {
        const query = new URLSearchParams();
        if (params.user_id) query.set('user_id', String(params.user_id));
        if (params.date) query.set('date', params.date);
        const suffix = query.toString();
        return `${ENDPOINTS.TIME_ENTRY_SCREENSHOTS.TIMELINE}${suffix ? `?${suffix}` : ''}`;
      },
      providesTags: [{ type: 'TimeTracking' as const, id: 'SCREENSHOTS' }],
    }),

    /**
     * Permanently delete one capture — the image and its metadata row.
     *
     * No optimistic patch. This destroys data in Google Drive as well as in
     * the database, and the backend can legitimately refuse (403 for a role
     * without `screenshots:delete`, 404 for an id outside the caller's
     * organization, 502 when Drive is unreachable and the row is deliberately
     * kept). Removing the card first and putting it back on failure would
     * claim a deletion that did not happen; the day is re-read instead, so
     * what is on screen is what the server actually still holds.
     */
    deleteScreenshot: builder.mutation<ScreenshotDeleteResponse, number>({
      query: (screenshotId) => ({
        url: ENDPOINTS.TIME_ENTRY_SCREENSHOTS.DELETE(screenshotId),
        method: 'DELETE',
      }),
      invalidatesTags: [{ type: 'TimeTracking' as const, id: 'SCREENSHOTS' }],
    }),
  }),
});

export const {
  useGetScreenshotsQuery,
  useGetScreenshotTimelineQuery,
  useGetScreenshotDayQuery,
  useDeleteScreenshotMutation,
} = screenshotsApi;
