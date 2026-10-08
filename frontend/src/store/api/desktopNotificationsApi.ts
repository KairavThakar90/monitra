import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

/**
 * Administrator-managed desktop notifications (docs/DESKTOP_NOTIFICATIONS.md).
 *
 * Every write returns the full admin view, so the cache is replaced with the
 * server's own answer rather than patched from what was sent. The two cheap,
 * reversible edits -- the Send checkbox and a delete -- are also applied at
 * once and rolled back if the server refuses, the way the Members switches are.
 *
 * Times are `HH:MM` in IST and weekdays are `0` (Monday) to `6` (Sunday).
 */

export type NotificationKind = 'interval' | 'daily';

export interface BuiltinNotification {
  key: string;
  label: string;
  description: string;
  kind: NotificationKind;
  every_minutes: number | null;
  default_time: string | null;
  enabled: boolean;
  /**
   * `HH:MM` IST for a daily reminder. For an interval reminder, null while it
   * repeats, or the time an administrator fixed it to (shown once a day then).
   */
  time: string | null;
  weekdays: number[];
}

export interface CustomNotification {
  id: string;
  title: string;
  message: string;
  time: string;
  weekdays: number[];
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
  created_by: string | null;
}

export interface DesktopNotifications {
  version: number;
  updated_at: string | null;
  updated_by_username: string | null;
  /** How many notifications a desktop may show in a rolling hour, and what the page may offer. */
  max_per_hour: number;
  default_max_per_hour: number;
  min_max_per_hour: number;
  max_max_per_hour: number;
  builtin: BuiltinNotification[];
  custom: CustomNotification[];
}

export interface BuiltinNotificationChange {
  enabled?: boolean;
  /** Moves a daily reminder, or fixes an interval one to a time of day. */
  time?: string;
  weekdays?: number[];
  /** Puts an interval reminder back on its cadence (removes its time). Never with `time`. */
  repeat?: true;
}

export interface CustomNotificationDraft {
  title: string;
  message: string;
  time: string;
  weekdays: number[];
  enabled: boolean;
}

export const ALL_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6];
export const WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/** "Every day", "Mon–Fri", or the days by name. */
export const describeWeekdays = (days: number[]): string => {
  const sorted = Array.from(new Set(days)).sort((a, b) => a - b);
  if (sorted.length === 7) return 'Every day';
  if (sorted.join(',') === '0,1,2,3,4') return 'Mon–Fri';
  if (sorted.join(',') === '5,6') return 'Sat, Sun';
  return sorted.map((day) => WEEKDAY_SHORT[day]).join(', ');
};

const QUERY = 'getDesktopNotifications' as const;

export const desktopNotificationsApi = baseApi.injectEndpoints({
  endpoints: (builder) => {
    const replaceCache = (dispatch: (action: unknown) => unknown, data: DesktopNotifications) =>
      dispatch(
        desktopNotificationsApi.util.updateQueryData(QUERY, undefined, (draft) => {
          Object.assign(draft, data);
        }) as never,
      );

    return {
      getDesktopNotifications: builder.query<DesktopNotifications, void>({
        query: () => ENDPOINTS.DESKTOP_NOTIFICATIONS.BASE,
      }),

      updateBuiltinNotification: builder.mutation<
        DesktopNotifications,
        { key: string; body: BuiltinNotificationChange }
      >({
        query: ({ key, body }) => ({ url: ENDPOINTS.DESKTOP_NOTIFICATIONS.BUILTIN(key), method: 'PUT', body }),
        async onQueryStarted({ key, body }, { dispatch, queryFulfilled }) {
          const optimistic = dispatch(
            desktopNotificationsApi.util.updateQueryData(QUERY, undefined, (draft) => {
              const row = draft.builtin.find((item) => item.key === key);
              if (row) Object.assign(row, body);
            }),
          );
          try {
            const { data } = await queryFulfilled;
            replaceCache(dispatch as never, data);
          } catch {
            optimistic.undo();
          }
        },
      }),

      updateDesktopNotificationLimit: builder.mutation<DesktopNotifications, { max_per_hour: number }>({
        query: (body) => ({ url: ENDPOINTS.DESKTOP_NOTIFICATIONS.LIMIT, method: 'PUT', body }),
        async onQueryStarted({ max_per_hour }, { dispatch, queryFulfilled }) {
          const optimistic = dispatch(
            desktopNotificationsApi.util.updateQueryData(QUERY, undefined, (draft) => {
              draft.max_per_hour = max_per_hour;
            }),
          );
          try {
            const { data } = await queryFulfilled;
            replaceCache(dispatch as never, data);
          } catch {
            optimistic.undo();
          }
        },
      }),

      createCustomNotification: builder.mutation<DesktopNotifications, CustomNotificationDraft>({
        query: (body) => ({ url: ENDPOINTS.DESKTOP_NOTIFICATIONS.CUSTOM, method: 'POST', body }),
        async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
          try {
            const { data } = await queryFulfilled;
            replaceCache(dispatch as never, data);
          } catch {
            /* nothing was applied optimistically */
          }
        },
      }),

      updateCustomNotification: builder.mutation<
        DesktopNotifications,
        { id: string; body: Partial<CustomNotificationDraft> }
      >({
        query: ({ id, body }) => ({ url: ENDPOINTS.DESKTOP_NOTIFICATIONS.CUSTOM_BY_ID(id), method: 'PATCH', body }),
        async onQueryStarted({ id, body }, { dispatch, queryFulfilled }) {
          const optimistic = dispatch(
            desktopNotificationsApi.util.updateQueryData(QUERY, undefined, (draft) => {
              const row = draft.custom.find((item) => item.id === id);
              if (row) Object.assign(row, body);
            }),
          );
          try {
            const { data } = await queryFulfilled;
            replaceCache(dispatch as never, data);
          } catch {
            optimistic.undo();
          }
        },
      }),

      deleteCustomNotification: builder.mutation<DesktopNotifications, string>({
        query: (id) => ({ url: ENDPOINTS.DESKTOP_NOTIFICATIONS.CUSTOM_BY_ID(id), method: 'DELETE' }),
        async onQueryStarted(id, { dispatch, queryFulfilled }) {
          const optimistic = dispatch(
            desktopNotificationsApi.util.updateQueryData(QUERY, undefined, (draft) => {
              draft.custom = draft.custom.filter((item) => item.id !== id);
            }),
          );
          try {
            const { data } = await queryFulfilled;
            replaceCache(dispatch as never, data);
          } catch {
            optimistic.undo();
          }
        },
      }),
    };
  },
});

export const {
  useGetDesktopNotificationsQuery,
  useUpdateBuiltinNotificationMutation,
  useUpdateDesktopNotificationLimitMutation,
  useCreateCustomNotificationMutation,
  useUpdateCustomNotificationMutation,
  useDeleteCustomNotificationMutation,
} = desktopNotificationsApi;
