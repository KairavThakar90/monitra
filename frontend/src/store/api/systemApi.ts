import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

/**
 * Deployment-wide state: the maintenance notice.
 *
 * `getMaintenanceStatus` is what every signed-in session polls. It answers a
 * boolean and nothing else is done with it: the toast is shown when it turns
 * true and removed when it turns false, and no request anywhere in the app
 * reads it to refuse, delay or change anything. Maintenance mode is a notice,
 * not a lock.
 *
 * The three administrator calls read who set it, set it, and read the audit
 * trail. They share one cache tag with the status, so an administrator who
 * flips the switch sees their own status query refetched at once rather than
 * waiting out the next poll. The endpoints refuse a non-administrator with
 * 403 regardless of what the page renders.
 */

export interface MaintenanceStatus {
  maintenance_mode: boolean;
  updated_at: string | null;
  server_time: string;
}

export interface MaintenanceMode extends MaintenanceStatus {
  updated_by_user_id: number | null;
  updated_by_username: string | null;
}

export type MaintenanceAction = 'maintenance_enabled' | 'maintenance_disabled';

export interface MaintenanceAuditEntry {
  id: number;
  action: MaintenanceAction;
  user_id: number;
  description: string | null;
  created_at: string;
}

/** Wire values are snake_case; these are what the history list shows. */
export const MAINTENANCE_ACTION_LABELS: Record<MaintenanceAction, string> = {
  maintenance_enabled: 'Maintenance ENABLED',
  maintenance_disabled: 'Maintenance DISABLED',
};

const SYSTEM_TAG = { type: 'System' as const, id: 'MAINTENANCE' };

export const systemApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getMaintenanceStatus: builder.query<MaintenanceStatus, void>({
      query: () => ENDPOINTS.SYSTEM.MAINTENANCE_STATUS,
      providesTags: [SYSTEM_TAG],
    }),
    getMaintenanceMode: builder.query<MaintenanceMode, void>({
      query: () => ENDPOINTS.SYSTEM.MAINTENANCE_MODE,
      providesTags: [SYSTEM_TAG],
    }),
    setMaintenanceMode: builder.mutation<MaintenanceMode, { enabled: boolean }>({
      query: (body) => ({ url: ENDPOINTS.SYSTEM.MAINTENANCE_MODE, method: 'PUT', body }),
      invalidatesTags: [SYSTEM_TAG],
    }),
    getMaintenanceHistory: builder.query<{ items: MaintenanceAuditEntry[] }, { limit?: number } | void>({
      query: (args) => ENDPOINTS.SYSTEM.MAINTENANCE_HISTORY((args && args.limit) || 20),
      providesTags: [SYSTEM_TAG],
    }),
  }),
});

export const {
  useGetMaintenanceStatusQuery,
  useGetMaintenanceModeQuery,
  useSetMaintenanceModeMutation,
  useGetMaintenanceHistoryQuery,
} = systemApi;
