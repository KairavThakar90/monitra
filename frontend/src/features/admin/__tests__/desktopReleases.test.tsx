// @vitest-environment jsdom
/**
 * Admin Settings -> Desktop Releases.
 *
 * The real page against a real RTK Query store with only `fetch` replaced by a
 * small stateful fake of the backend, so what is pinned is the request a
 * browser would make and what the administrator sees:
 *
 * - the history lists every version with its platforms, policy and status;
 * - a new release is refused before any request when the version, the grace
 *   period or an installer is malformed, and POSTed with exactly the policy
 *   fields and installers the administrator entered when it is not;
 * - a draft is validated, published (behind a confirmation that states the
 *   24-hour rule) and withdrawn, each through its own endpoint;
 * - a server refusal is shown with the problems it listed;
 * - the detail shows the statistics and the audit history, and never a
 *   credential.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import { validateSha256, validateVersion } from '../../../validation';

const showToast = vi.fn();
let confirmAnswer = true;
const confirmAction = vi.fn(async (_title: string, _body: string) => confirmAnswer);

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction }),
}));

import { AdminDesktopReleases } from '../AdminDesktopReleases';

type Body = Record<string, unknown>;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const HOST = 'https://staff.peakworkos.com/download_app_files';

const artifact = (version: string, platform: string, architecture: string | null, status = 'draft') => ({
  id: Math.floor(Math.random() * 100000), version, platform, architecture,
  download_url: `${HOST}/${platform === 'win32' ? `Monitra-Setup-${version}.exe` : `Monitra-macOS-${architecture}-${version}.dmg`}`,
  file_size: 31000000, sha256: 'a'.repeat(64), release_notes: null, release_notes_url: null,
  status, force_update: false, min_supported_version: null, signed: true, signer: 'Monitra',
  created_at: '2026-11-02T10:00:00Z', published_at: null,
});

const version = (v: string, extra: Body = {}) => ({
  id: Number(v.replace(/\./g, '')), version: v, title: `Monitra ${v}`, release_notes: '- Faster startup',
  target_platforms: ['win32', 'darwin'], mandatory: false, grace_hours: 24, required_min_version: null,
  enforced_floor: null, effective_at: null, status: 'draft', created_by_user_id: 5,
  published_by_user_id: null, withdrawn_by_user_id: null, created_at: '2026-11-02T10:00:00Z',
  updated_at: '2026-11-02T10:00:00Z', validated_at: null, published_at: null, withdrawn_at: null,
  artifacts: [artifact(v, 'win32', null), artifact(v, 'darwin', 'arm64'), artifact(v, 'darwin', 'x86_64')],
  ready: true, problems: [],
  ...extra,
});

const detailOf = (v: Body) => ({
  ...v,
  stats: {
    targeted_users: 12, requirements: { pending: 3, expired: 1, satisfied: 2 },
    installs: { success: 2, failed: 1 }, adoption: { on_or_above: 4, below: 8 },
    email: { pending: 2, sent: 9, failed: 1 },
  },
  history: [
    { id: 2, action: 'release_published', user_id: 5, description: 'Published Monitra 1.4.0 for Windows, macOS (mandatory, 24 h grace, floor 1.4.0) [grace]', created_at: '2026-11-02T11:00:00Z' },
    { id: 1, action: 'release_created', user_id: 5, description: 'Created Monitra 1.4.0 [grace]', created_at: '2026-11-02T10:00:00Z' },
  ],
});

describe('AdminDesktopReleases', () => {
  let container: HTMLDivElement;
  let root: Root;
  let versions: Body[];
  let requests: { method: string; url: string; body: Body | null }[];
  let refuseNext: { status: number; body: Body } | null;

  const flush = async () => {
    for (let i = 0; i < 6; i += 1) {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    }
  };

  beforeEach(async () => {
    versions = [
      version('1.4.0', { mandatory: true, enforced_floor: '1.4.0', status: 'published', published_at: '2026-11-02T11:00:00Z' }),
      version('1.3.3'),
    ];
    requests = [];
    refuseNext = null;
    confirmAnswer = true;
    showToast.mockClear();
    confirmAction.mockClear();

    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // RTK Query hands fetch one `Request`; read the method and body from it.
      const request = input instanceof Request ? input : new Request(input, init);
      const url = request.url;
      const method = request.method.toUpperCase();
      const text = await request.clone().text();
      const body = text ? (JSON.parse(text) as Body) : null;
      requests.push({ method, url, body });
      if (refuseNext && method !== 'GET') {
        const refusal = refuseNext;
        refuseNext = null;
        return json(refusal.body, refusal.status);
      }
      const path = new URL(url, 'http://localhost').pathname;
      const match = path.match(/\/desktop\/releases\/versions(?:\/([^/]+))?(?:\/(validate|publish|withdraw))?$/);
      if (!match) return json({ detail: `unexpected ${method} ${path}` }, 404);
      const [, v, action] = match;
      if (!v) {
        if (method === 'GET') return json({ versions });
        const created = version(String(body?.version), {
          title: body?.title ?? null, target_platforms: body?.target_platforms, mandatory: body?.mandatory,
          grace_hours: body?.grace_hours, artifacts: [],
          ready: false, problems: [{ code: 'missing', message: 'The win32 artifact has not been registered.', artifact: 'win32' }],
        });
        versions = [created, ...versions];
        return json(detailOf(created), 201);
      }
      const current = versions.find((entry) => entry.version === v);
      if (!current) return json({ detail: 'Release not found.' }, 404);
      if (action === 'validate') { current.status = 'validated'; current.validated_at = '2026-11-02T10:30:00Z'; return json(detailOf(current)); }
      if (action === 'publish') { current.status = 'published'; current.published_at = '2026-11-02T11:00:00Z'; return json({ version: v, published: current.artifacts, announcement_queued: true }); }
      if (action === 'withdraw') { current.status = 'withdrawn'; return json({ version: current, suspended_requirements: 3 }); }
      if (method === 'PATCH') { Object.assign(current, body); return json(detailOf(current)); }
      return json(detailOf(current));
    }));

    container = document.createElement('div');
    document.body.appendChild(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault().concat(baseApi.middleware),
    });
    root = createRoot(container);
    await act(async () => {
      root.render(<Provider store={store}><AdminDesktopReleases /></Provider>);
    });
    await flush();
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const click = async (element: Element | null) => {
    if (!element) throw new Error('nothing to click');
    await act(async () => {
      (element as HTMLElement).click();
    });
    await flush();
  };

  const type = async (id: string, value: string) => {
    const input = container.querySelector<HTMLInputElement | HTMLTextAreaElement>(`#${id}`);
    if (!input) throw new Error(`no field ${id}`);
    const setter = Object.getOwnPropertyDescriptor(
      input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value',
    )!.set!;
    await act(async () => {
      setter.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
  };

  /** The rows of the releases table (the detail renders its own installers table). */
  const releaseRows = () => Array.from(container.querySelector('table')!.querySelectorAll('tbody tr'));

  const buttonNamed = (name: string) =>
    Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.trim() === name) ?? null;

  it('lists every version with its platforms, policy and status, and opens the first', () => {
    const rows = releaseRows();
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining('1.4.0'),
      expect.stringContaining('1.3.3'),
    ]);
    expect(rows[0].textContent).toContain('Windows + macOS');
    expect(rows[0].textContent).toContain('Mandatory · 24 h grace · floor 1.4.0');
    expect(rows[0].textContent).toContain('Published');
    expect(rows[1].textContent).toContain('Optional');
    expect(rows[1].textContent).toContain('Draft');
    // The detail: statistics, installers and history, no secrets.
    expect(container.textContent).toContain('12');
    expect(container.textContent).toContain('3 pending · 1 expired · 2 satisfied');
    expect(container.textContent).toContain('4 on or above · 8 below');
    expect(container.textContent).toContain('2 success · 1 failed');
    expect(container.textContent).toContain('2 pending · 9 sent · 1 failed');
    expect(container.textContent).toContain('Monitra-Setup-1.4.0.exe');
    expect(container.textContent).toContain('Published Monitra 1.4.0 for Windows, macOS');
    expect(container.textContent).not.toMatch(/token|password|secret/i);
    expect(buttonNamed('Withdraw')).not.toBeNull();
    expect(buttonNamed('Publish')).toBeNull();
  });

  it('refuses a malformed release before any request is made', async () => {
    await click(buttonNamed('+ New release'));
    await type('release-version', 'v1.5');
    await type('release-grace', '9999');
    await type('release-floor', 'latest');
    const before = requests.length;
    await click(buttonNamed('Create draft'));
    expect(requests.length).toBe(before);
    const text = container.querySelector('[role="dialog"]')!.textContent ?? '';
    expect(text).toContain('three numbers separated by dots');
    expect(text).toContain('Grace period');
    expect(text).toContain('Required minimum version');
  });

  it('creates a mandatory Windows-only draft with its installer, exactly as entered', async () => {
    await click(buttonNamed('+ New release'));
    await type('release-version', '1.5.0');
    await type('release-title', 'Winter release');
    const mac = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))
      .find((box) => box.parentElement?.textContent?.includes('macOS'))!;
    await click(mac);
    const mandatory = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))
      .find((box) => box.parentElement?.textContent?.includes('Mandatory'))!;
    await click(mandatory);
    await type('release-grace', '48');
    const withInstallers = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))
      .find((box) => box.parentElement?.textContent?.includes('Enter the installers now'))!;
    await click(withInstallers);
    expect(container.querySelector('#artifact-darwin-arm64-url')).toBeNull();
    await type('artifact-win32-url', `${HOST}/Monitra-Setup-1.5.0.exe`);
    await type('artifact-win32-sha', 'B'.repeat(64));
    await type('artifact-win32-size', '31000000');
    await click(buttonNamed('Create draft'));

    const post = requests.find((r) => r.method === 'POST')!;
    expect(post.url).toMatch(/\/desktop\/releases\/versions$/);
    expect(post.body).toEqual({
      version: '1.5.0', title: 'Winter release', release_notes: null, release_notes_url: undefined,
      target_platforms: ['win32'], mandatory: true, grace_hours: 48, required_min_version: null, effective_at: null,
      artifacts: [{
        platform: 'win32', architecture: null, download_url: `${HOST}/Monitra-Setup-1.5.0.exe`,
        sha256: 'b'.repeat(64), file_size: 31000000, signed: false,
      }],
    });
    expect(showToast).toHaveBeenCalledWith('Release created as a draft.', 'success');
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(releaseRows()[0].textContent).toContain('1.5.0');
  });

  it('shows the problems a refused publication lists', async () => {
    await click(releaseRows()[1]);
    refuseNext = {
      status: 422,
      body: { detail: { message: 'Version 1.3.3 cannot be published yet: The darwin/arm64 artifact has not been registered.', problems: [] } },
    };
    await click(buttonNamed('Publish'));
    expect(confirmAction).toHaveBeenCalled();
    expect(container.textContent).toContain('darwin/arm64 artifact has not been registered');
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('cannot be published yet'), 'error');
  });

  it('validates, publishes behind a confirmation that states the rule, and withdraws', async () => {
    await click(releaseRows()[1]);
    await click(buttonNamed('Validate'));
    expect(requests.some((r) => r.method === 'POST' && r.url.endsWith('/1.3.3/validate'))).toBe(true);
    expect(container.textContent).toContain('Validated');

    confirmAnswer = false;
    await click(buttonNamed('Publish'));
    expect(requests.some((r) => r.url.endsWith('/1.3.3/publish'))).toBe(false);
    expect(confirmAction.mock.calls.at(-1)![1]).toContain('optional');

    confirmAnswer = true;
    await click(buttonNamed('Publish'));
    expect(requests.some((r) => r.method === 'POST' && r.url.endsWith('/1.3.3/publish'))).toBe(true);
    expect(showToast).toHaveBeenCalledWith('Release published.', 'success');
    expect(buttonNamed('Withdraw')).not.toBeNull();

    await click(buttonNamed('Withdraw'));
    expect(confirmAction.mock.calls.at(-1)![1]).toContain('grace period already running is kept');
    expect(requests.some((r) => r.method === 'POST' && r.url.endsWith('/1.3.3/withdraw'))).toBe(true);
    expect(showToast).toHaveBeenCalledWith('Release withdrawn.', 'success');
  });

  it('states the 24-hour rule when publishing a mandatory release', async () => {
    versions[1] = version('1.3.3', { mandatory: true, enforced_floor: '1.3.3', status: 'validated' });
    await click(releaseRows()[1]);
    confirmAnswer = false;
    await click(buttonNamed('Publish'));
    const message = String(confirmAction.mock.calls.at(-1)![1]);
    expect(message).toContain('24 hours from the first time their desktop sees it');
    expect(message).toContain('cannot be used on the old version');
  });

  it('edits the policy of a published release without touching its platforms', async () => {
    await click(buttonNamed('Edit'));
    const dialog = container.querySelector('[role="dialog"]')!;
    const platformBoxes = Array.from(dialog.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))
      .filter((box) => /Windows|macOS/.test(box.parentElement?.textContent ?? ''));
    expect(platformBoxes.every((box) => box.disabled)).toBe(true);
    await type('release-grace', '12');
    await click(buttonNamed('Save changes'));
    const patch = requests.find((r) => r.method === 'PATCH')!;
    expect(patch.url).toMatch(/\/versions\/1\.4\.0$/);
    expect(patch.body).not.toHaveProperty('target_platforms');
    expect(patch.body).toMatchObject({ grace_hours: 12, mandatory: true, clear_required_min_version: true });
    expect(showToast).toHaveBeenCalledWith('Release updated.', 'success');
  });
});

describe('release validators', () => {
  it('accepts major.minor.patch and nothing else', () => {
    expect(validateVersion('1.4.0')).toEqual({ ok: true, value: '1.4.0' });
    expect(validateVersion(' 1.10.2 ')).toEqual({ ok: true, value: '1.10.2' });
    for (const bad of ['v1.4.0', '1.4', '1.4.0.1', '01.4.0', '1.4.0-rc1', '']) {
      expect(validateVersion(bad).ok, bad).toBe(false);
    }
    expect(validateVersion('', { required: false })).toEqual({ ok: true, value: '' });
  });

  it('accepts a 64-hex digest, folding case, and nothing else', () => {
    expect(validateSha256('A'.repeat(64))).toEqual({ ok: true, value: 'a'.repeat(64) });
    for (const bad of ['a'.repeat(63), 'g'.repeat(64), 'a'.repeat(65), '']) {
      expect(validateSha256(bad).ok, bad).toBe(false);
    }
  });
});
