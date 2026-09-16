/**
 * Desktop downloads for the website.
 *
 * The rule this module exists to enforce: **the download link is never a
 * versioned filename baked into the site.** Each platform has one stable link
 * (`DOWNLOAD_LINKS`) that always serves the current build, so a release
 * published six months from now is served without the frontend being touched —
 * which is the whole difference between "Download Monitra" and "Download
 * Monitra 1.0.0 forever". The release metadata shown beside a button (version,
 * size, notes, checksum) still comes from the backend, and is omitted rather
 * than guessed when it is not there.
 *
 * Unauthenticated by design: someone installing Monitra for the first time has
 * no account yet. The backend only ever exposes *published* releases here, so
 * a draft cannot leak through this path.
 */
import { ENDPOINTS } from './endpoints';
import { formatApiError } from './utils';

/** One downloadable artifact, as the public endpoint describes it. */
export interface DesktopRelease {
  version: string | null;
  platform: string | null;
  architecture: string | null;
  download_url: string | null;
  file_size: number | null;
  sha256: string | null;
  release_notes: string | null;
  release_notes_url: string | null;
  published_at: string | null;
  /**
   * False when this deployment has published nothing for the platform.
   * The page must render that as "no download yet" — never as a broken link.
   */
  available: boolean;
}

export interface DesktopDownloadIndex {
  downloads: Record<string, DesktopRelease>;
  latest_version: string | null;
}

/** The platform keys the backend's index answers with. */
export type DownloadKey = 'windows' | 'macos-arm64' | 'macos-x86_64';

/** Platform/architecture pairs, for building a direct download link. */
export const DOWNLOAD_TARGETS: Record<DownloadKey, { platform: string; arch?: string }> = {
  windows: { platform: 'win32' },
  'macos-arm64': { platform: 'darwin', arch: 'arm64' },
  'macos-x86_64': { platform: 'darwin', arch: 'x86_64' },
};

/**
 * Where each platform's installer is served from.
 *
 * These are distribution links, not artifact filenames: each one names a
 * platform and nothing else, so the file behind it can be replaced for a new
 * release without this file being edited. That is the property the download
 * page has always depended on — it used to come from the backend's
 * "latest release" redirect, and now it comes from the host these point at.
 *
 * Because the file is no longer served by our backend, the page can no longer
 * learn a download's size or checksum from the link. Version and size are
 * still shown when `GET /desktop/releases/downloads` knows them, and simply
 * omitted when it does not — the download itself never depends on that call.
 */
export const DOWNLOAD_LINKS: Record<DownloadKey, string> = {
  windows: 'https://storetransform.com/?window_download_monitra',
  'macos-arm64': 'https://storetransform.com/?macARM64_download_monitra',
  'macos-x86_64': 'https://storetransform.com/?macX8664_download_monitra',
};

/**
 * The link one platform's download button points at.
 *
 * Safe to put straight in an `href`, and it needs no fetch first: a visitor
 * can download Monitra even when the release service is unreachable.
 */
export function downloadUrlFor(key: DownloadKey): string {
  return DOWNLOAD_LINKS[key];
}

/** Every platform's current download, for a page that lists them all. */
export async function fetchDesktopDownloadsAPI(): Promise<DesktopDownloadIndex> {
  const response = await fetch(ENDPOINTS.DESKTOP.DOWNLOADS, {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(formatApiError(errorData, 'Could not load the available downloads.'));
  }
  return response.json();
}

/** The current download for one platform. */
export async function fetchLatestReleaseAPI(
  platform: string,
  arch?: string,
): Promise<DesktopRelease> {
  const response = await fetch(ENDPOINTS.DESKTOP.LATEST(platform, arch), {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(formatApiError(errorData, 'Could not load the latest release.'));
  }
  return response.json();
}

/**
 * Guess which download this visitor most likely wants.
 *
 * A guess, and treated as one: it only decides which card is highlighted, and
 * every download stays visible and clickable. Browsers deliberately do not
 * report the CPU — `navigator.platform` says "MacIntel" on Apple Silicon too —
 * so a page that *only* offered the detected build would hand half of all Mac
 * users an artifact that cannot run on their machine. Detection picks the
 * default; the user picks the download.
 */
export function detectDownloadKey(): DownloadKey | null {
  if (typeof navigator === 'undefined') return null;
  const haystack = `${navigator.userAgent} ${navigator.platform ?? ''}`.toLowerCase();
  if (haystack.includes('win')) return 'windows';
  if (haystack.includes('mac')) {
    // Apple Silicon is not distinguishable from Intel here with any
    // reliability, so the arm64 build is offered as the default only because
    // it is the one every Mac sold since 2020 needs. The Intel card sits
    // beside it, labelled, for everyone else.
    return 'macos-arm64';
  }
  return null;
}

/** A file size a person can read. */
export function formatFileSize(bytes: number | null): string {
  if (!bytes || bytes <= 0) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${units[unit]}`;
}
