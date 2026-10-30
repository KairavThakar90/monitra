/**
 * The folder the website serves the installers from: `public/download_app_files/`.
 *
 * Two ways this folder has already broken the Download page in production, and
 * each is pinned here because nothing else would notice before a user did:
 *
 * 1. **A `web.config` IIS cannot read answers 500 for every file in the folder.**
 *    On 2026-10-27 a folder `web.config` setting a `.dmg` MIME type was deployed;
 *    from then on the `.exe`, both `.dmg` and the folder itself returned "500
 *    Internal server error" and Chrome reported "Site wasn't available". Deleting
 *    the file from the repository does not delete it from the server -- copying a
 *    build over a deployed folder only adds and replaces -- so the folder now
 *    ships an *empty* `web.config` whose job is to overwrite the broken one. If it
 *    ever gains a setting, it can break the folder again.
 * 2. **A link to an installer that is not in the folder.** A missing file under
 *    this folder does not 404: the site's single-page rewrite answers it with the
 *    index page, so the "download" is a web page. The three links must name files
 *    that exist.
 *
 * The folder is read through Vite's own globbing rather than `node:fs`: this project
 * carries no Node type definitions, and the installers are tens of megabytes, so they
 * are only *listed* here, never loaded.
 */
import { describe, expect, it } from 'vitest';

import { downloadUrlFor } from '../desktopRelease';
import type { DownloadKey } from '../desktopRelease';

const PREFIX = '/download_app_files/';
const keys: DownloadKey[] = ['windows', 'macos-arm64', 'macos-x86_64'];

// Keys only: `eager: false` means no file is read until called, and none is called.
const present = Object.keys(import.meta.glob('../../../public/download_app_files/*'));
const configs = import.meta.glob('../../../public/download_app_files/web.config', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

describe('the installer folder', () => {
  it('ships a web.config that carries no settings at all', () => {
    const files = Object.values(configs);
    expect(files, 'public/download_app_files/web.config is missing').toHaveLength(1);

    // Whatever the comment says, only the XML itself counts.
    const xml = files[0].replace(/<!--[\s\S]*?-->/g, '');
    const elements = Array.from(xml.matchAll(/<\s*([A-Za-z][\w.:-]*)/g)).map((match) => match[1]);

    expect(elements).toEqual(['configuration']);
  });

  it.each(keys)('has the installer the %s button downloads', (key) => {
    const name = downloadUrlFor(key).replace(PREFIX, '');

    expect(
      present.some((path) => path.endsWith(`/${name}`)),
      `${name} is missing from public/download_app_files/`,
    ).toBe(true);
  });
});
