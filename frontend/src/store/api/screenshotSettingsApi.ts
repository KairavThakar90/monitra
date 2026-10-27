import { baseApi } from './baseApi';
import { patchEveryCachedQuery } from './optimistic';
import { ENDPOINTS } from '../../api/endpoints';
import type { Member } from './membersApi';

/**
 * Screenshot Management (administrators only, server-side).
 *
 * Two independent things live behind it:
 *
 * - the deployment-wide size and quality every desktop captures at. The GET
 *   carries the limits the server enforces, so the page never hardcodes them,
 *   and a PUT is a *replacement*: all three keys are sent, `image_quality: null`
 *   meaning "Automatic" (never an omitted key);
 * - whether each member is captured at all. A missing `screenshots_enabled` on
 *   a member means on; only an explicit `false` is off.
 *
 * The settings write replaces the cache with the server's own answer. The
 * one-member switch flips the row at once and puts it back if the server
 * refuses, the way the Members switches do.
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

export interface MemberScreenshotResult {
  id: number;
  name: string;
  screenshots_enabled: boolean;
  /** False when the member was already in the position asked for. */
  changed: boolean;
}

export type ScreenshotMemberScope = { scope: 'all' } | { scope: 'members'; user_ids: number[] };

export interface BulkScreenshotResult {
  enabled: boolean;
  matched: number;
  changed: number;
}

/** Roles whose machines are never captured -- the server refuses them too. */
const NOT_CAPTURED_ROLES = new Set(['client', 'release_bot']);

/**
 * The people a desktop can capture: active members, never a client or the
 * release service account. The same people "all members" means on the server.
 */
export const isCapturableMember = (member: Member) =>
  member.status === 'active' && !NOT_CAPTURED_ROLES.has(member.role);

/** Whether a member's screenshots are on. Only an explicit `false` is off. */
export const screenshotsAreOn = (member: Member) => member.screenshots_enabled !== false;

const SETTINGS_QUERY = 'getScreenshotSettings' as const;

/** Sets one member's switch in every cached members list (paged lists and the full one). */
const setMemberSwitch = (
  parts: Parameters<typeof patchEveryCachedQuery>[0],
  memberId: number,
  enabled: boolean,
) =>
  patchEveryCachedQuery(parts, 'getMembers', (draft) => {
    const row = draft.items?.find((member: { id: number }) => member.id === memberId);
    if (row) row.screenshots_enabled = enabled;
  });

const setMemberSwitchEverywhere = (
  parts: Parameters<typeof patchEveryCachedQuery>[0],
  memberId: number,
  enabled: boolean,
) => {
  const paged = setMemberSwitch(parts, memberId, enabled);
  const everyone = patchEveryCachedQuery(parts, 'getAllMembers', (draft) => {
    const row = Array.isArray(draft) ? draft.find((member: { id: number }) => member.id === memberId) : null;
    if (row) row.screenshots_enabled = enabled;
  });
  return {
    undo: () => {
      paged.undo();
      everyone.undo();
    },
  };
};

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

    setMemberScreenshots: builder.mutation<MemberScreenshotResult, { id: number; enabled: boolean }>({
      query: ({ id, enabled }) => ({ url: ENDPOINTS.SCREENSHOT_SETTINGS.MEMBER(id), method: 'PUT', body: { enabled } }),
      async onQueryStarted({ id, enabled }, { dispatch, getState, queryFulfilled }) {
        const parts = { dispatch, getState } as Parameters<typeof patchEveryCachedQuery>[0];
        const optimistic = setMemberSwitchEverywhere(parts, id, enabled);
        try {
          const { data } = await queryFulfilled;
          // The server's own answer replaces the guess.
          setMemberSwitchEverywhere(parts, id, data.screenshots_enabled);
        } catch {
          optimistic.undo();
        }
      },
    }),

    // "All" and "selected" can change many rows, some of which are not on any
    // cached page, so nothing is guessed: the lists are read again.
    setScreenshotsForMembers: builder.mutation<
      BulkScreenshotResult,
      { enabled: boolean; apply_to: ScreenshotMemberScope }
    >({
      query: (body) => ({ url: ENDPOINTS.SCREENSHOT_SETTINGS.MEMBERS, method: 'POST', body }),
      invalidatesTags: [{ type: 'Member', id: 'LIST' }],
    }),
  }),
});

export const {
  useGetScreenshotSettingsQuery,
  useUpdateScreenshotSettingsMutation,
  useSetMemberScreenshotsMutation,
  useSetScreenshotsForMembersMutation,
} = screenshotSettingsApi;
