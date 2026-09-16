/**
 * Tests for the public download logic.
 *
 * Two properties matter here and neither is visual:
 *
 * 1. **A download link is never a versioned filename.** The whole point of the
 *    page is that publishing a release does not require editing the frontend,
 *    and the way that regresses is someone "simplifying" a link to point
 *    straight at an artifact. These tests fail if that happens. They also pin
 *    the three links themselves: a typo in one of them is a download that
 *    hands a Mac user the Windows installer, and nothing else would catch it.
 * 2. **Detection only ever picks a default.** It must never be the thing that
 *    decides what a user is allowed to download — browsers cannot report the
 *    CPU, so a Mac visitor has to be able to reach the other architecture.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  DOWNLOAD_TARGETS,
  detectDownloadKey,
  downloadUrlFor,
  fetchDesktopDownloadsAPI,
  formatFileSize,
} from '../desktopRelease';
import type { DownloadKey } from '../desktopRelease';

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

  it('names a platform rather than a version', () => {
    for (const key of keys) {
      const url = downloadUrlFor(key);
      // No artifact filename, and no version number, anywhere in the link.
      expect(url).not.toMatch(/\.(exe|dmg|zip)/i);
      expect(url).not.toMatch(/\d+\.\d+\.\d+/);
    }
  });

  it('is an absolute https link the browser can follow on its own', () => {
    // The installer is hosted away from our backend, so these have to be
    // complete URLs: a relative path would resolve against the dashboard.
    for (const key of keys) {
      expect(downloadUrlFor(key)).toMatch(/^https:\/\//);
    }
  });

  it('gives every advertised platform a distinct link', () => {
    // The three builds are not interchangeable — an Intel Mac cannot run the
    // Apple Silicon one — so two cards sharing a link is a shipped defect.
    const urls = new Set(keys.map(downloadUrlFor));
    expect(urls.size).toBe(keys.length);
  });

  it('serves each platform the build it asks for', () => {
    expect(downloadUrlFor('windows')).toBe(
      'https://storetransform.com/?window_download_monitra',
    );
    expect(downloadUrlFor('macos-arm64')).toBe(
      'https://storetransform.com/?macARM64_download_monitra',
    );
    expect(downloadUrlFor('macos-x86_64')).toBe(
      'https://storetransform.com/?macX8664_download_monitra',
    );
  });

  it('answers without the release service having been called', () => {
    // The download is the page's whole purpose: it must not become
    // unavailable because a metadata request failed.
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    expect(downloadUrlFor('windows')).toMatch(/^https:\/\//);
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
