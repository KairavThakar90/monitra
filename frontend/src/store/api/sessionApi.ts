import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';
import { normalizeUserProfile } from '../../api/auth';
import type { UserRead } from '../../api/auth';

/**
 * The signed-in user's own profile, read through the shared base query.
 *
 * Going through `baseQueryWithRefresh` (not a bare fetch) matters: an expired
 * access token is renewed on the way, and the backend's `login_disabled`
 * refusal -- an administrator excluded this account -- ends the session and
 * sends the user to the sign-in screen with the reason. `AuthProvider` polls
 * this every few seconds while the tab is visible, which is what makes an
 * administrator's Exclude reach an open browser tab without a reload.
 */
export const sessionApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getSessionProfile: builder.query<UserRead, void>({
      query: () => ({ url: ENDPOINTS.AUTH.ME }),
      transformResponse: (user: UserRead) => normalizeUserProfile(user),
      keepUnusedDataFor: 0,
    }),
  }),
});
