import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

/**
 * Screenshot Management -> Multi-screen (administrators only, server-side).
 *
 * A member's desktop reports the screens it can see; this lists them and lets an
 * administrator switch capture off for individual screens. Only members whose
 * desktop has reported at least one screen are listed -- there is nothing to
 * manage for the rest, and nothing is invented for them.
 *
 * A PUT changes only the screens it lists, and the server answers with that
 * member's screens as they now are, which replaces the member in the cache.
 */

export interface MemberScreen {
  /** 1-based position in the operating system's list of screens. */
  number: number;
  width: number;
  height: number;
  /** Negative for a screen to the left of / above the primary. */
  left: number;
  top: number;
  is_primary: boolean;
  /** Whether the member's desktop listed this screen in its latest report. */
  connected: boolean;
  /** The administrator's choice. A screen nobody decided about is captured. */
  capture_enabled: boolean;
}

export interface MemberScreens {
  user_id: number;
  name: string;
  email: string | null;
  role: string | null;
  connected_count: number;
  last_reported_at: string | null;
  screens: MemberScreen[];
}

export interface ScreenOverview {
  members: MemberScreens[];
}

export interface ScreenChoice {
  number: number;
  capture_enabled: boolean;
}

const OVERVIEW_QUERY = 'getScreenshotDisplays' as const;

export const screenshotDisplaysApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getScreenshotDisplays: builder.query<ScreenOverview, void>({
      query: () => ENDPOINTS.SCREENSHOT_DISPLAYS.BASE,
    }),

    setScreenshotDisplays: builder.mutation<MemberScreens, { userId: number; screens: ScreenChoice[] }>({
      query: ({ userId, screens }) => ({
        url: `${ENDPOINTS.SCREENSHOT_DISPLAYS.BASE}/${userId}`,
        method: 'PUT',
        body: { screens },
      }),
      async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
        try {
          const { data } = await queryFulfilled;
          dispatch(
            screenshotDisplaysApi.util.updateQueryData(OVERVIEW_QUERY, undefined, (draft) => {
              const at = draft.members.findIndex((member) => member.user_id === data.user_id);
              if (at >= 0) draft.members[at] = data;
            }) as never,
          );
        } catch {
          /* nothing was applied optimistically */
        }
      },
    }),
  }),
});

export const { useGetScreenshotDisplaysQuery, useSetScreenshotDisplaysMutation } = screenshotDisplaysApi;
