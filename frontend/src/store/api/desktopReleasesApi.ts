import { baseApi } from './baseApi';
import { ENDPOINTS } from '../../api/endpoints';

/**
 * Desktop release management (administrators only).
 *
 * A *version record* is what an administrator decides about one desktop
 * version: its title and notes, which platforms it is for, whether it is
 * mandatory, how long the grace period is, which older versions must take it
 * (the floor), when it takes effect, and its lifecycle. The artifact rows
 * (Windows, macOS arm64, macOS x86_64) are registered by the release pipeline
 * or entered by hand with the version; publishing offers the version to its
 * target platforms and, if mandatory, starts each user's 24-hour grace period
 * the first time their desktop meets it. Every write returns the server's own
 * view of the version, so the cache is replaced, never patched from a guess.
 */

export type ReleasePlatform = 'win32' | 'darwin';

export type ReleaseVersionStatus = 'draft' | 'validated' | 'published' | 'withdrawn' | 'superseded';

export interface ReleaseArtifact {
  id: number;
  version: string;
  platform: string;
  architecture: string | null;
  download_url: string;
  file_size: number | null;
  sha256: string;
  release_notes: string | null;
  release_notes_url: string | null;
  status: string;
  force_update: boolean;
  min_supported_version: string | null;
  signed: boolean;
  signer: string | null;
  created_at: string;
  published_at: string | null;
}

export interface ReleaseProblem {
  code: string;
  message: string;
  artifact: string | null;
}

export interface ReleaseVersion {
  id: number;
  version: string;
  title: string | null;
  release_notes: string | null;
  target_platforms: ReleasePlatform[];
  mandatory: boolean;
  grace_hours: number;
  required_min_version: string | null;
  /** The floor actually enforced, or null when the release blocks nobody. */
  enforced_floor: string | null;
  effective_at: string | null;
  status: ReleaseVersionStatus;
  created_by_user_id: number | null;
  published_by_user_id: number | null;
  withdrawn_by_user_id: number | null;
  created_at: string;
  updated_at: string;
  validated_at: string | null;
  published_at: string | null;
  withdrawn_at: string | null;
  artifacts: ReleaseArtifact[];
  ready: boolean;
  problems: ReleaseProblem[];
}

export interface ReleaseVersionStats {
  targeted_users: number;
  requirements: { pending: number; expired: number; satisfied: number };
  installs: Record<string, number>;
  adoption: { on_or_above: number; below: number };
  /** Outbox statuses; "sent" is provider acceptance, not confirmed delivery. */
  email: Record<string, number>;
}

export interface ReleaseAuditEntry {
  id: number;
  action: string;
  user_id: number;
  description: string | null;
  created_at: string;
}

export interface ReleaseVersionDetail extends ReleaseVersion {
  stats: ReleaseVersionStats;
  history: ReleaseAuditEntry[];
}

export interface ReleaseArtifactInput {
  platform: ReleasePlatform;
  architecture?: string | null;
  download_url: string;
  sha256: string;
  file_size?: number | null;
  signed?: boolean;
  signer?: string | null;
}

export interface ReleaseVersionCreate {
  version: string;
  title?: string | null;
  release_notes?: string | null;
  release_notes_url?: string | null;
  target_platforms: ReleasePlatform[];
  mandatory: boolean;
  grace_hours: number;
  required_min_version?: string | null;
  effective_at?: string | null;
  artifacts: ReleaseArtifactInput[];
}

export interface ReleaseVersionUpdate {
  title?: string | null;
  release_notes?: string | null;
  release_notes_url?: string | null;
  target_platforms?: ReleasePlatform[];
  mandatory?: boolean;
  grace_hours?: number;
  required_min_version?: string | null;
  clear_required_min_version?: boolean;
  effective_at?: string | null;
  clear_effective_at?: boolean;
}

export interface PublishVersionResponse {
  version: string;
  published: ReleaseArtifact[];
  announcement_queued: boolean;
}

export interface WithdrawVersionResponse {
  version: ReleaseVersion;
  /** Grace periods kept, waiting for a corrective release with the same floor. */
  suspended_requirements: number;
}

export const PLATFORM_LABELS: Record<ReleasePlatform, string> = {
  win32: 'Windows',
  darwin: 'macOS',
};

export const STATUS_LABELS: Record<ReleaseVersionStatus, string> = {
  draft: 'Draft',
  validated: 'Validated',
  published: 'Published',
  withdrawn: 'Withdrawn',
  superseded: 'Superseded',
};

export const AUDIT_ACTION_LABELS: Record<string, string> = {
  release_created: 'Created',
  release_updated: 'Updated',
  release_validated: 'Validated',
  release_published: 'Published',
  release_withdrawn: 'Withdrawn',
};

const LIST_TAG = { type: 'System' as const, id: 'DESKTOP_RELEASES' };
const versionTag = (version: string) => ({ type: 'System' as const, id: `DESKTOP_RELEASE_${version}` });

export const desktopReleasesApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getReleaseVersions: builder.query<{ versions: ReleaseVersion[] }, void>({
      query: () => ENDPOINTS.DESKTOP.VERSIONS,
      providesTags: (result) => [
        LIST_TAG,
        ...(result?.versions ?? []).map((v) => versionTag(v.version)),
      ],
    }),
    getReleaseVersion: builder.query<ReleaseVersionDetail, string>({
      query: (version) => ENDPOINTS.DESKTOP.VERSION(version),
      providesTags: (_result, _error, version) => [versionTag(version)],
    }),
    createReleaseVersion: builder.mutation<ReleaseVersionDetail, ReleaseVersionCreate>({
      query: (body) => ({ url: ENDPOINTS.DESKTOP.VERSIONS, method: 'POST', body }),
      invalidatesTags: [LIST_TAG],
    }),
    updateReleaseVersion: builder.mutation<ReleaseVersionDetail, { version: string; body: ReleaseVersionUpdate }>({
      query: ({ version, body }) => ({ url: ENDPOINTS.DESKTOP.VERSION(version), method: 'PATCH', body }),
      invalidatesTags: (_result, _error, { version }) => [LIST_TAG, versionTag(version)],
    }),
    validateReleaseVersion: builder.mutation<ReleaseVersionDetail, string>({
      query: (version) => ({ url: ENDPOINTS.DESKTOP.VERSION_VALIDATE(version), method: 'POST' }),
      invalidatesTags: (_result, _error, version) => [LIST_TAG, versionTag(version)],
    }),
    publishReleaseVersion: builder.mutation<PublishVersionResponse, string>({
      query: (version) => ({ url: ENDPOINTS.DESKTOP.VERSION_PUBLISH(version), method: 'POST' }),
      invalidatesTags: (_result, _error, version) => [LIST_TAG, versionTag(version)],
    }),
    withdrawReleaseVersion: builder.mutation<WithdrawVersionResponse, { version: string; reason?: string }>({
      query: ({ version, reason }) => ({ url: ENDPOINTS.DESKTOP.VERSION_WITHDRAW(version, reason), method: 'POST' }),
      invalidatesTags: (_result, _error, { version }) => [LIST_TAG, versionTag(version)],
    }),
  }),
});

export const {
  useGetReleaseVersionsQuery,
  useGetReleaseVersionQuery,
  useCreateReleaseVersionMutation,
  useUpdateReleaseVersionMutation,
  useValidateReleaseVersionMutation,
  usePublishReleaseVersionMutation,
  useWithdrawReleaseVersionMutation,
} = desktopReleasesApi;
