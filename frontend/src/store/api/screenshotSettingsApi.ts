import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';
import type { Member } from './membersApi';

/**
 * Screenshot Management (administrators only, server-side): the deployment-wide
 * size and quality every desktop captures at. The GET carries the limits the
 * server enforces, so the page never hardcodes them, and a PUT is a
 * *replacement*: all three keys are sent, `image_quality: null` meaning
 * "Automatic" (never an omitted key).
 *
 * The write replaces the cache with the server's own answer. Whether a member
 * is captured at all is not decided here: it is `screenshots_enabled` on the
 * member, saved through the ordinary member update (User Management).
 */

export interface ScreenshotSettings {
  image_width: number;
  image_height: number;
  /** null is "Automatic": the server picks, starting from `automatic_image_quality`. */
  image_quality: number | null;
  default_image_width: number;
  default_image_height: number;
  /** What Automatic starts from. It is lowered on busy screens, so it is not a fixed value. */
  automatic_image_quality: number;
  min_image_edge: number;
  max_image_edge: number;
  min_image_quality: number;
  max_image_quality: number;
  version: number;
  updated_at: string | null;
  updated_by_username: string | null;
}

export interface ScreenshotSettingsUpdate {
  image_width: number;
  image_height: number;
  image_quality: number | null;
}

/** Roles whose machines are never tracked -- the server refuses them too. */
const NOT_CAPTURED_ROLES = new Set(['client', 'release_bot']);

/**
 * The people a desktop runs for: active members, never a client or the release
 * service account. The Idle detection section lists exactly these.
 */
export const isCapturableMember = (member: Member) =>
  member.status === 'active' && !NOT_CAPTURED_ROLES.has(member.role);

/** Whether idle detection is on for a member. Only an explicit `false` is off. */
export const idleIsOn = (member: Member) => member.idle_enabled !== false;

const SETTINGS_QUERY = 'getScreenshotSettings' as const;

export const screenshotSettingsApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getScreenshotSettings: builder.query<ScreenshotSettings, void>({
      query: () => ENDPOINTS.SCREENSHOT_SETTINGS.BASE,
    }),

    updateScreenshotSettings: builder.mutation<ScreenshotSettings, ScreenshotSettingsUpdate>({
      query: (body) => ({ url: ENDPOINTS.SCREENSHOT_SETTINGS.BASE, method: 'PUT', body }),
      async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
        try {
          const { data } = await queryFulfilled;
          dispatch(
            screenshotSettingsApi.util.updateQueryData(SETTINGS_QUERY, undefined, (draft) => {
              Object.assign(draft, data);
            }) as never,
          );
        } catch {
          /* nothing was applied optimistically */
        }
      },
    }),
  }),
});

export const {
  useGetScreenshotSettingsQuery,
  useUpdateScreenshotSettingsMutation,
} = screenshotSettingsApi;
