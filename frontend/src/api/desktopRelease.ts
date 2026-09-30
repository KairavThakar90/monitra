/**
 * Desktop downloads for the website.
 *
 * Each platform has exactly one link (`DOWNLOAD_LINKS`) and every download
 * button goes through it. As of 2026-09-30 the installers are served by the
 * website itself, from `public/download_app_files/`, and the links are built
 * from `DOWNLOAD_VERSION`, so shipping a new build means dropping the three
 * files in that folder and bumping that one constant (see the note on
 * `DOWNLOAD_LINKS`). The version a button serves is therefore known here; the
 * rest of the metadata shown beside it (size, notes, checksum) comes from the
 * backend, and is only shown when the backend's record is about that same
 * build — see `describesLinkedBuild` — and omitted rather than guessed
 * otherwise.
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
 * The version of Monitra every link in `DOWNLOAD_LINKS` serves.
 *
 * Kept in step with `desktop/version.py` by hand: this is the number the
 * download page prints beside each button, so it must be the version of the
 * file the button actually hands over, and nothing else.
 */
export const DOWNLOAD_VERSION = '1.3.1';

/**
 * The folder the installers are served from, at the root of this website.
 *
 * It is `frontend/public/download_app_files/` in the source tree: Vite copies
 * `public/` verbatim into the build, so the same path works on the dev server
 * and on the deployed site. The files are deliberately not tracked in git —
 * they are build output, and far too large for it — so the machine that builds
 * a release must have them in place, or the buttons lead to a 404.
 */
const DOWNLOAD_DIRECTORY = '/download_app_files';

/**
 * Where each platform's installer is served from.
 *
 * Root-relative on purpose: the file sits on the same origin as this page, so
 * the link resolves against the site wherever the page is mounted, and the
 * `download` attribute on the button (which browsers honour only for
 * same-origin links) makes a click save the file instead of navigating to it.
 * **Publishing a new release means placing the three files in
 * `public/download_app_files/` and bumping `DOWNLOAD_VERSION`** — and the
 * pinned paths in `__tests__/desktopRelease.test.ts` — or the download page
 * keeps serving the old build forever.
 *
 * Because the file is not served by our backend, the page cannot learn a
 * download's size or checksum from the link. Those are still shown when
 * `GET /desktop/releases/downloads` describes this very version, and simply
 * omitted when it does not — the download itself never depends on that call.
 */
export const DOWNLOAD_LINKS: Record<DownloadKey, string> = {
  windows: `${DOWNLOAD_DIRECTORY}/Monitra-Setup-${DOWNLOAD_VERSION}.exe`,
  'macos-arm64': `${DOWNLOAD_DIRECTORY}/Monitra-macOS-arm64-${DOWNLOAD_VERSION}.dmg`,
  'macos-x86_64': `${DOWNLOAD_DIRECTORY}/Monitra-macOS-x86_64-${DOWNLOAD_VERSION}.dmg`,
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

/**
 * Whether the backend's record is about the build the button serves.
 *
 * Only then may its size, release notes and checksum be printed beside the
 * button. The backend answers with the newest *published* row, which lags
 * behind `DOWNLOAD_VERSION` until that version is registered — and a size or
 * SHA-256 that belongs to a different file is not "slightly stale", it is
 * wrong: a person verifying their download against it would conclude the
 * file is corrupt.
 */
export function describesLinkedBuild(release: DesktopRelease | undefined): release is DesktopRelease {
  return Boolean(release?.available) && release?.version === DOWNLOAD_VERSION;
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
