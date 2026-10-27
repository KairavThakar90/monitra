// @vitest-environment jsdom
/**
 * Settings -> Screenshot Management, the page that arranges the sections.
 *
 * Pinned here: "Who is captured" is gone (it is a per-member switch in User
 * Management now), the sections are Size and quality, Idle detection and Privacy
 * rules in that order, Size and quality is open by default, `#look` / `#idle` /
 * `#privacy` open the matching section, the idle summary counts members whose
 * idle detection is on, and a non-administrator sees no controls and causes no
 * member request.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { MemoryRouter } from 'react-router-dom';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminScreenshotManagementPage } from '../AdminScreenshotManagementPage';

type Body = Record<string, unknown>;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const member = (id: number, name: string, extra: Body = {}): Body => ({
  id, name, email: `${name.split(' ')[0].toLowerCase()}@example.com`, role: 'employee', status: 'active',
  date_of_joining: null, date_of_birth: null, designation: '', ...extra,
});

const SETTINGS = {
  image_width: 1500, image_height: 1500, image_quality: null,
  default_image_width: 1500, default_image_height: 1500, automatic_image_quality: 79,
  min_image_edge: 250, max_image_edge: 4000, min_image_quality: 10, max_image_quality: 100,
  version: 0, updated_at: null, updated_by_username: null,
};

describe('AdminScreenshotManagementPage', () => {
  let container: HTMLDivElement;
  let root: Root;
  let paths: string[];
  let settingsStatus: number;

  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const until = async (done: () => boolean) => {
    await settle();
    for (let i = 0; i < 400 && !done(); i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    }
    await settle();
  };
  const header = (id: string) => container.querySelector(`#screenshot-${id}-header`) as HTMLButtonElement | null;
  const expanded = (id: string) => header(id)!.getAttribute('aria-expanded') === 'true';

  const mount = async (hash = '') => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(
        <Provider store={store}>
          <MemoryRouter initialEntries={[`/admin/settings/screenshot-management${hash}`]}>
            <AdminScreenshotManagementPage />
          </MemoryRouter>
        </Provider>,
      );
    });
    await until(() => (settingsStatus === 200 ? !!container.querySelector('#screenshot-width') : container.textContent!.includes('Only administrators')));
    await settle();
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.setItem('accessToken', 'tok');
    paths = [];
    settingsStatus = 200;
    const members = [
      member(1, 'Alice Active'),
      member(2, 'Bob Builder', { idle_enabled: false }),
      member(3, 'Carl Casual', { idle_enabled: true }),
      member(4, 'Dora Dormant', { status: 'inactive', idle_enabled: false }),
      member(5, 'Client Co', { role: 'client', idle_enabled: false }),
    ];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), 'http://localhost');
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      paths.push(`${method} ${url.pathname}`);
      if (url.pathname.endsWith('/screenshot-settings')) {
        return settingsStatus === 200 ? json(SETTINGS) : json({ detail: 'Forbidden' }, settingsStatus);
      }
      if (url.pathname.endsWith('/members') && method === 'GET') {
        return json({ items: members, page: 1, limit: 100, total: members.length, pages: 1 });
      }
      if (url.pathname.endsWith('/screenshot/applications') || url.pathname.endsWith('/screenshot/urls')) return json([]);
      if (url.pathname.includes('/screenshot-exclusions')) return json([]);
      return json({}, 404);
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('has no "Who is captured" section and says nothing about choosing who is captured', async () => {
    await mount();
    expect(container.textContent).not.toContain('Who is captured');
    expect(header('capture')).toBeNull();
    expect(container.textContent).not.toContain('Member-wise screenshot control');
    expect(container.querySelector('button[aria-label^="Screenshots for"]')).toBeNull();
    expect(container.textContent).not.toContain('Decide who is captured');
  });

  it('lists Size and quality, Idle detection and Privacy rules, in that order', async () => {
    await mount();
    const headers = Array.from(container.querySelectorAll('h2 > button[id^="screenshot-"]')).map((b) => b.id);
    expect(headers).toEqual(['screenshot-look-header', 'screenshot-idle-header', 'screenshot-privacy-header']);
    const titles = Array.from(container.querySelectorAll('h2 > button')).map((b) => b.querySelector('span span')?.textContent);
    expect(titles).toEqual(['Size and quality', 'Idle detection', 'Privacy rules']);
  });

  it('opens Size and quality by default and leaves the others closed', async () => {
    await mount();
    expect([expanded('look'), expanded('idle'), expanded('privacy')]).toEqual([true, false, false]);
  });

  it.each([
    ['#look', [true, false, false]],
    ['#idle', [false, true, false]],
    ['#privacy', [false, false, true]],
    ['#capture', [true, false, false]],
    ['#nonsense', [true, false, false]],
  ])('opens the right section for %s', async (hash, want) => {
    await mount(hash);
    expect([expanded('look'), expanded('idle'), expanded('privacy')]).toEqual(want);
  });

  it('summarises Idle detection as how many active members have it on', async () => {
    await mount();
    await until(() => (header('idle')!.textContent ?? '').includes('of'));
    // Alice (absent = on) and Carl are on, Bob is off; the inactive member and the client do not count.
    expect(header('idle')!.textContent).toContain('2 of 3 on');
  });

  it('keeps every section mounted when collapsed, and Expand all opens them all', async () => {
    await mount();
    expect(container.querySelector('#screenshot-idle-panel')).toBeTruthy();
    expect((container.querySelector('#screenshot-idle-panel') as HTMLElement).hidden).toBe(true);
    const expandAll = Array.from(container.querySelectorAll('button')).find((b) => b.textContent === 'Expand all')!;
    await act(async () => { expandAll.click(); });
    expect([expanded('look'), expanded('idle'), expanded('privacy')]).toEqual([true, true, true]);
  });

  it('shows no controls, and asks for no members, when the server refuses the page with 403', async () => {
    settingsStatus = 403;
    await mount();

    expect(container.textContent).toContain('Only administrators can open this page.');
    expect(container.querySelector('#screenshot-width')).toBeNull();
    expect(container.querySelector('button[role="switch"]')).toBeNull();
    expect(paths.filter((p) => p.endsWith('/members'))).toEqual([]);
  });
});
