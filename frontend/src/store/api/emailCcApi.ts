import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

/**
 * Email Management (administrators only, server-side): the fifteen emails Monitra
 * sends, and who is copied on each.
 *
 * The GET carries the emails themselves (name, who it goes to, what triggers it,
 * and whether it may be copied at all), so the page never hardcodes the list and
 * cannot drift from what the server sends. A PUT is a *replacement*: every address
 * with the email keys it is copied on; an address left out is removed. The server's
 * answer replaces the cache.
 */

export interface EmailTrigger {
  /** The server's stable key for the email. */
  key: string;
  number: number;
  name: string;
  goes_to: string;
  trigger: string;
  /** False for the two emails that carry a private link: they are never copied. */
  cc_allowed: boolean;
  note: string | null;
  /** False while the whole email is switched off in the deployment's configuration. */
  active: boolean;
}

export interface EmailCcAddress {
  email: string;
  /** The keys of the emails this address is copied on. */
  triggers: string[];
}

export interface EmailCcSettings {
  triggers: EmailTrigger[];
  addresses: EmailCcAddress[];
  max_addresses: number;
  /** True until an administrator first saves: the configured starting list, on every copyable email. */
  is_default: boolean;
  version: number;
  updated_at: string | null;
  updated_by_username: string | null;
}

export interface EmailCcUpdate {
  addresses: EmailCcAddress[];
}

const QUERY = 'getEmailCc' as const;

export const emailCcApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getEmailCc: builder.query<EmailCcSettings, void>({
      query: () => ENDPOINTS.EMAIL_CC.BASE,
    }),

    updateEmailCc: builder.mutation<EmailCcSettings, EmailCcUpdate>({
      query: (body) => ({ url: ENDPOINTS.EMAIL_CC.BASE, method: 'PUT', body }),
      async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
        try {
          const { data } = await queryFulfilled;
          dispatch(
            emailCcApi.util.updateQueryData(QUERY, undefined, (draft) => {
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

export const { useGetEmailCcQuery, useUpdateEmailCcMutation } = emailCcApi;
