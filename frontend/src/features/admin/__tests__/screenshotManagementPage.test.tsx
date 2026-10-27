// @vitest-environment jsdom
/**
 * Settings -> Screenshot Management, the page that arranges the sections.
 *
 * Pinned here: "Who is captured" is gone (it is a per-member switch in User
 * Management now), the tabs are Resolution and quality, Idle management and
 * Privacy rules in that order, Resolution and quality is selected by default,
 * `#look` / `#idle` / `#privacy` select the matching tab, the arrow keys move
 * between tabs, the idle summary counts members whose idle detection is on, and
 * a non-administrator sees no controls and causes no member request.
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
  const header = (id: string) => container.querySelector(`#screenshot-${id}-tab`) as HTMLButtonElement | null;
  const expanded = (id: string) => header(id)!.getAttribute('aria-selected') === 'true';
  const panelShown = (id: string) => !(container.querySelector(`#screenshot-${id}-panel`) as HTMLElement).hidden;

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

  it('lists Resolution and quality, Idle management and Privacy rules, in that order', async () => {
    await mount();
    const tabs = Array.from(container.querySelectorAll('[role="tablist"] > [role="tab"]'));
    expect(tabs.map((b) => b.id)).toEqual(['screenshot-look-tab', 'screenshot-idle-tab', 'screenshot-privacy-tab']);
    expect(tabs.map((b) => b.firstChild?.textContent)).toEqual([
      'Resolution and quality', 'Idle management', 'Privacy rules',
    ]);
  });

  it('selects Resolution and quality by default and shows only its panel', async () => {
    await mount();
    expect([expanded('look'), expanded('idle'), expanded('privacy')]).toEqual([true, false, false]);
    expect([panelShown('look'), panelShown('idle'), panelShown('privacy')]).toEqual([true, false, false]);
  });

  it.each([
    ['#look', [true, false, false]],
    ['#idle', [false, true, false]],
    ['#privacy', [false, false, true]],
    ['#capture', [true, false, false]],
    ['#nonsense', [true, false, false]],
  ])('selects the right tab for %s', async (hash, want) => {
    await mount(hash);
    expect([expanded('look'), expanded('idle'), expanded('privacy')]).toEqual(want);
  });

  it('summarises Idle detection as how many active members have it on', async () => {
    await mount();
    await until(() => (header('idle')!.textContent ?? '').includes('of'));
    // Alice (absent = on) and Carl are on, Bob is off; the inactive member and the client do not count.
    expect(header('idle')!.textContent).toContain('2 of 3 on');
  });

  it('keeps every panel mounted when its tab is not selected, and switches panel on click', async () => {
    await mount();
    expect(container.querySelector('#screenshot-idle-panel')).toBeTruthy();
    expect(panelShown('idle')).toBe(false);
    await act(async () => { header('idle')!.click(); });
    expect([expanded('look'), expanded('idle'), expanded('privacy')]).toEqual([false, true, false]);
    expect([panelShown('look'), panelShown('idle'), panelShown('privacy')]).toEqual([false, true, false]);
    // The width box is still mounted while its tab is away.
    expect(container.querySelector('#screenshot-width')).toBeTruthy();
  });

  it('moves between tabs with the arrow keys, Home and End', async () => {
    await mount();
    const press = async (key: string) => {
      const current = container.querySelector('[role="tab"][aria-selected="true"]') as HTMLElement;
      await act(async () => { current.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })); });
    };
    await press('ArrowRight');
    expect(expanded('idle')).toBe(true);
    await press('End');
    expect(expanded('privacy')).toBe(true);
    await press('ArrowRight');
    expect(expanded('look')).toBe(true);
    await press('ArrowLeft');
    expect(expanded('privacy')).toBe(true);
    await press('Home');
    expect(expanded('look')).toBe(true);
    // Only the selected tab is in the tab order.
    expect(header('idle')!.tabIndex).toBe(-1);
    expect(header('look')!.tabIndex).toBe(0);
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
