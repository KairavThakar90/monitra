/**
 * Tests for the public download logic.
 *
 * Two properties matter here and neither is visual:
 *
 * 1. **Each platform's button points at exactly the artifact it should.**
 *    These tests pin the three links themselves: a typo in one of them is a
 *    download that hands a Mac user the Windows installer, and nothing else
 *    would catch it. Since 2026-09-30 the links point at installers the site
 *    serves itself from `public/download_app_files/`, so publishing a new
 *    release means placing the files there, bumping `DOWNLOAD_VERSION`, and
 *    updating the pinned paths here together.
 * 2. **Detection only ever picks a default.** It must never be the thing that
 *    decides what a user is allowed to download — browsers cannot report the
 *    CPU, so a Mac visitor has to be able to reach the other architecture.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  DOWNLOAD_TARGETS,
  DOWNLOAD_VERSION,
  describesLinkedBuild,
  detectDownloadKey,
  downloadUrlFor,
  fetchDesktopDownloadsAPI,
  formatFileSize,
} from '../desktopRelease';
import type { DesktopRelease, DownloadKey } from '../desktopRelease';

/** Point `navigator` at a fixed user agent for one assertion. */
function withNavigator(userAgent: string, platform: string) {
  vi.stubGlobal('navigator', { userAgent, platform });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('downloadUrlFor', () => {
  const keys = Object.keys(DOWNLOAD_TARGETS) as DownloadKey[];

  it('is a root-relative path on this site', () => {
    // The installer is served by the website itself. Root-relative (a leading
    // slash) is what makes that safe: a bare relative path would resolve
    // against whatever route the page is on, and an absolute URL to another
    // host would lose the `download` attribute, which browsers only honour
    // for the same origin.
    for (const key of keys) {
      expect(downloadUrlFor(key)).toMatch(/^\/[^/]/);
    }
  });

  it('gives every advertised platform a distinct link', () => {
    // The three builds are not interchangeable — an Intel Mac cannot run the
    // Apple Silicon one — so two cards sharing a link is a shipped defect.
    const urls = new Set(keys.map(downloadUrlFor));
    expect(urls.size).toBe(keys.length);
  });

  it('serves each platform the build it asks for', () => {
    expect(downloadUrlFor('windows')).toBe('/download_app_files/Monitra-Setup-1.3.0.exe');
    expect(downloadUrlFor('macos-arm64')).toBe('/download_app_files/Monitra-macOS-arm64-1.3.0.dmg');
    expect(downloadUrlFor('macos-x86_64')).toBe('/download_app_files/Monitra-macOS-x86_64-1.3.0.dmg');
  });

  it('answers without the release service having been called', () => {
    // The download is the page's whole purpose: it must not become
    // unavailable because a metadata request failed.
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    expect(downloadUrlFor('windows')).toMatch(/^\//);
  });

  it('serves the version the page prints beside every button', () => {
    // DOWNLOAD_VERSION is what the card shows as "Version"; a link that
    // serves any other build would make that label a lie.
    for (const key of keys) {
      expect(downloadUrlFor(key)).toContain(`-${DOWNLOAD_VERSION}.`);
    }
  });

  it('hands each platform the installer format it can open', () => {
    // Windows runs an installer executable; both Macs mount a disk image.
    expect(downloadUrlFor('windows')).toMatch(/\.exe$/);
    expect(downloadUrlFor('macos-arm64')).toMatch(/\.dmg$/);
    expect(downloadUrlFor('macos-x86_64')).toMatch(/\.dmg$/);
  });
});

describe('describesLinkedBuild', () => {
  const record = (overrides: Partial<DesktopRelease>): DesktopRelease => ({
    version: DOWNLOAD_VERSION,
    platform: 'win32',
    architecture: null,
    download_url: null,
    file_size: 31_681_154,
    sha256: 'abc',
    release_notes: null,
    release_notes_url: null,
    published_at: null,
    available: true,
    ...overrides,
  });

  it('accepts the backend record for the build the button serves', () => {
    expect(describesLinkedBuild(record({}))).toBe(true);
  });

  it('rejects a record for a different version', () => {
    // The backend describes the newest *registered* release. Before the
    // linked build is registered that is the previous one, and printing its
    // size and checksum under a button that serves a different file would
    // send a person verifying their download to the wrong answer.
    expect(describesLinkedBuild(record({ version: '1.1.1' }))).toBe(false);
  });

  it('rejects a record the backend marked unavailable', () => {
    expect(describesLinkedBuild(record({ available: false }))).toBe(false);
  });

  it('rejects the absence of a record', () => {
    expect(describesLinkedBuild(undefined)).toBe(false);
  });
});

describe('detectDownloadKey', () => {
  it('recommends the Windows build to a Windows visitor', () => {
    withNavigator(
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      'Win32',
    );
    expect(detectDownloadKey()).toBe('windows');
  });

  it('recommends Apple Silicon to a Mac visitor', () => {
    // Apple Silicon still reports "MacIntel", which is exactly why detection
    // may only choose the highlighted card and not restrict the list.
    withNavigator(
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
      'MacIntel',
    );
    expect(detectDownloadKey()).toBe('macos-arm64');
  });

  it('recommends nothing on a platform we do not build for', () => {
    // Linux: the page must fall back to listing everything with an
    // explanation, not highlight an arbitrary build.
    withNavigator('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36', 'Linux x86_64');
    expect(detectDownloadKey()).toBeNull();
  });

  it('returns a key that always names a real download target', () => {
    withNavigator('Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Win32');
    const key = detectDownloadKey();
    expect(key).not.toBeNull();
    expect(DOWNLOAD_TARGETS[key as DownloadKey]).toBeDefined();
  });
});

describe('fetchDesktopDownloadsAPI', () => {
  it('reports a failed request as an error rather than an empty catalogue', async () => {
    // "The service is down" and "nothing is published" are different facts.
    // Collapsing them would render a temporary outage as a permanent absence.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        json: () => Promise.resolve({ detail: 'boom' }),
      }),
    );
    await expect(fetchDesktopDownloadsAPI()).rejects.toThrow();
  });

  it('passes an unavailable platform straight through', async () => {
    // The backend says available:false when it has published nothing. That has
    // to survive to the page, which renders it as "Not available yet".
    const payload = {
      latest_version: null,
      downloads: {
        windows: { available: false, version: null, download_url: null },
      },
    };
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(payload) }),
    );
    const result = await fetchDesktopDownloadsAPI();
    expect(result.downloads.windows.available).toBe(false);
    expect(result.latest_version).toBeNull();
  });
});

describe('formatFileSize', () => {
  it('renders a real size in the unit a person reads', () => {
    expect(formatFileSize(87_000_000)).toBe('83.0 MB');
    expect(formatFileSize(2048)).toBe('2.0 KB');
  });

  it('renders nothing when the size is unknown', () => {
    // The card omits the row entirely rather than claiming "0 B".
    expect(formatFileSize(null)).toBe('');
    expect(formatFileSize(0)).toBe('');
  });
});
