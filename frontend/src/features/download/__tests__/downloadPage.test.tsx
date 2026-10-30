// @vitest-environment jsdom
/**
 * The public download page.
 *
 * Every 1.3.x installer is unsigned and un-notarized, so the first time one is opened Windows
 * answers "Windows protected your PC" and macOS answers "cannot be opened because Apple cannot
 * check it for malicious software" (or, for a quarantined download, "is damaged"). Neither is a
 * bad file -- the bytes match the CI build -- but a visitor who meets one with no instructions
 * concludes the download is broken. What is pinned here:
 *
 * - the three buttons are real links to the three installers, and work without the release service;
 * - the Windows steps are on the page;
 * - the macOS steps are on the page too (they were missing: only Windows was explained), for both
 *   the "cannot check it" message and the "damaged" one, for new and old macOS versions alike;
 * - the guidance does not depend on which system the visitor is detected as using.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DownloadPage } from '../DownloadPage';
import { DOWNLOAD_VERSION } from '../../../api/desktopRelease';

describe('DownloadPage', () => {
  let container: HTMLDivElement;
  let root: Root;

  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const mount = async (answer: () => Promise<Response>) => {
    vi.stubGlobal('fetch', vi.fn(async () => answer()));
    await act(async () => { root.render(<MemoryRouter><DownloadPage /></MemoryRouter>); });
    await settle();
  };
  const text = (testId: string) => container.querySelector(`[data-testid="${testId}"]`)!.textContent!;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const emptyIndex = () => Promise.resolve(new Response(JSON.stringify({ downloads: {}, latest_version: null }), {
    status: 200, headers: { 'content-type': 'application/json' },
  }));

  it('offers all three installers as real download links', async () => {
    await mount(emptyIndex);
    const links = Array.from(container.querySelectorAll('a[download]')) as HTMLAnchorElement[];

    expect(links.map((a) => a.getAttribute('href'))).toEqual([
      `/download_app_files/Monitra-Setup-${DOWNLOAD_VERSION}.exe`,
      `/download_app_files/Monitra-macOS-arm64-${DOWNLOAD_VERSION}.dmg`,
      `/download_app_files/Monitra-macOS-x86_64-${DOWNLOAD_VERSION}.dmg`,
    ]);
  });

  it('keeps every link working when the release service is down', async () => {
    await mount(() => Promise.reject(new Error('network down')));

    expect(container.querySelectorAll('a[download]')).toHaveLength(3);
    expect(container.textContent).toContain('Version details could not be loaded.');
  });

  it('explains the Windows warning, including the browser\'s own "not commonly downloaded" prompt', async () => {
    await mount(emptyIndex);
    const help = text('windows-security-help');

    expect(help).toContain('Windows protected your PC');
    expect(help).toContain('More info');
    expect(help).toContain('Run anyway');
    expect(help).toContain('isn’t commonly downloaded');
    expect(help).toContain('Keep');
  });

  it('explains the macOS message that appears when the app is first opened', async () => {
    await mount(emptyIndex);
    const help = text('macos-security-help');

    expect(help).toContain('cannot check it for malicious software');
    expect(help).toContain('drag Monitra into Applications');
    // Tapping the default button throws the app away; the page says which to press.
    expect(help).toContain('Done');
    expect(help).toContain('not “Move to Trash”');
  });

  it('gives the right route for new macOS (Open Anyway) and for older macOS (Control-click, Open)', async () => {
    await mount(emptyIndex);
    const help = text('macos-security-help');

    expect(help).toContain('macOS 15 (Sequoia) or later');
    expect(help).toContain('System Settings → Privacy & Security');
    expect(help).toContain('Open Anyway');
    expect(help).toContain('macOS 14 or earlier');
    expect(help).toContain('Control-click Monitra');
  });

  it('explains the "damaged" message and gives the one command that clears it', async () => {
    await mount(emptyIndex);
    const help = text('macos-security-help');

    expect(help).toContain('is damaged and can’t be opened');
    expect(help).toContain('xattr -dr com.apple.quarantine /Applications/Monitra.app');
    // The reassurance is accurate: the served bytes are the CI build (checked by hash).
    expect(help).toContain('the file is not damaged');
  });

  it('shows both sets of instructions whatever system the visitor is on', async () => {
    // jsdom reports neither Windows nor a Mac, so nothing is "recommended" -- the help is not tied to it.
    await mount(emptyIndex);

    expect(container.querySelector('[data-testid="windows-security-help"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="macos-security-help"]')).toBeTruthy();
  });
});
