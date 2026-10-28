// @vitest-environment jsdom
/**
 * Screenshot Management -> Multi-screen.
 *
 * The real section is rendered against a real RTK Query store with only `fetch`
 * replaced by a small stateful fake of the two endpoints, so what is pinned is the
 * request a browser would make:
 *
 * - members who use more than one screen are the default view, with every member
 *   one click away, and each shows how many screens are connected right now;
 * - a member with three screens can be set to capture one: the two switched off
 *   are the only things sent, through `PUT /screenshot-displays/{id}`;
 * - Save is enabled only for a real change, and never offers to switch off the
 *   last connected screen (that is the member's screenshot switch);
 * - an unplugged screen stays listed and can be decided ahead of its return;
 * - a refused save puts the row back and toasts the server's own words;
 * - nobody is invented: with nothing reported the page says so.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

const showToast = vi.fn();
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction: async () => true }),
}));

import { ScreenManagement } from '../AdminScreenshotScreens';

type Body = Record<string, unknown>;
type Call = { method: string; path: string; body: Body | null };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const screen = (number: number, extra: Body = {}) => ({
  number, width: 1920, height: 1080, left: 0, top: 0, is_primary: number === 1,
  connected: true, capture_enabled: true, ...extra,
});

const member = (id: number, name: string, screens: Body[]): Body => ({
  user_id: id, name, email: `${name.split(' ')[0].toLowerCase()}@example.com`, role: 'employee',
  connected_count: screens.filter((s) => s.connected).length,
  last_reported_at: '2026-10-27T08:30:00Z', screens,
});

describe('ScreenManagement', () => {
  let container: HTMLDivElement;
  let root: Root;
  let calls: Call[];
  let members: Body[];
  let refuseNext: { status: number; detail: string } | null;
  let listStatus: number;

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
  const click = async (el: Element | null | undefined) => {
    expect(el).toBeTruthy();
    await act(async () => { el!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    await settle();
  };
  const type = async (el: HTMLInputElement, value: string) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(el, value);
    await act(async () => { el.dispatchEvent(new Event('input', { bubbles: true })); });
  };
  const toggle = (name: string, number: number) =>
    container.querySelector(`button[role="switch"][aria-label="Capture screen ${number} of ${name}"]`) as HTMLButtonElement | null;
  const on = (name: string, number: number) => toggle(name, number)!.getAttribute('aria-checked') === 'true';
  const saveFor = (name: string) =>
    container.querySelector(`button[aria-label="Save screens for ${name}"]`) as HTMLButtonElement | null;
  const writes = () => calls.filter((c) => c.method !== 'GET');
  const showAll = () =>
    click(Array.from(container.querySelectorAll('button')).find((b) => (b.textContent ?? '').startsWith('All members')));

  const mount = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><ScreenManagement /></Provider>); });
    await until(() => !container.textContent!.includes('Loading screens'));
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.setItem('accessToken', 'tok');
    showToast.mockClear();
    calls = [];
    refuseNext = null;
    listStatus = 200;
    members = [
      member(1, 'Mia Multi', [screen(1), screen(2, { width: 2560, height: 1440, left: 1920 }), screen(3, { left: -1920 })]),
      member(2, 'Sam Single', [screen(1)]),
      member(3, 'Una Unplugged', [screen(1), screen(2, { connected: false, capture_enabled: false })]),
      member(4, 'Dan Dual', [screen(1), screen(2)]),
    ];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), 'http://localhost');
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      const raw = request ? await request.text() : (init?.body as string | undefined);
      const body = raw ? (JSON.parse(raw) as Body) : null;
      calls.push({ method, path: url.pathname, body });

      const one = url.pathname.match(/\/screenshot-displays\/(\d+)$/);
      if (one && method === 'PUT') {
        if (refuseNext) { const r = refuseNext; refuseNext = null; return json({ detail: r.detail }, r.status); }
        const row = members.find((m) => m.user_id === Number(one[1]))! as { screens: Body[]; connected_count: number };
        for (const change of (body as { screens: { number: number; capture_enabled: boolean }[] }).screens) {
          const target = row.screens.find((s) => s.number === change.number)!;
          target.capture_enabled = change.capture_enabled;
        }
        return json(row);
      }
      if (url.pathname.endsWith('/screenshot-displays') && method === 'GET') {
        return listStatus === 200 ? json({ members }) : json({ detail: 'nope' }, listStatus);
      }
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

  it('shows the members using more than one screen first, with how many each has connected', async () => {
    await mount();

    expect(toggle('Mia Multi', 1)).toBeTruthy();
    expect(toggle('Dan Dual', 1)).toBeTruthy();
    // One screen connected, however many are on file, is not "multiple".
    expect(toggle('Sam Single', 1)).toBeNull();
    expect(toggle('Una Unplugged', 1)).toBeNull();
    expect(container.textContent).toContain('3 screens');
    expect(container.textContent).toContain('2 screens');
  });

  it('counts the filters, and "All members" lists everyone who has reported', async () => {
    await mount();
    const filters = Array.from(container.querySelectorAll('[role="group"][aria-label="Which members to show"] button'));
    expect(filters.map((b) => b.textContent)).toEqual(['Using multiple screens2', 'All members4']);

    await showAll();

    expect(toggle('Sam Single', 1)).toBeTruthy();
    expect(toggle('Una Unplugged', 1)).toBeTruthy();
    expect(container.textContent).toContain('1 screen');
  });

  it('describes each screen: its number, size, whether it is the primary, and whether it is connected', async () => {
    await mount();
    await showAll();

    const mia = container.querySelector('[role="group"][aria-label="Screens of Mia Multi"]')!.textContent!;
    expect(mia).toContain('Screen 1');
    expect(mia).toContain('Primary');
    expect(mia).toContain('2560 × 1440');
    expect(container.querySelector('[role="group"][aria-label="Screens of Una Unplugged"]')!.textContent).toContain('Not connected');
    // A screen nobody decided about is captured.
    expect(on('Mia Multi', 2)).toBe(true);
    // A screen the administrator switched off reads as skipped.
    expect(on('Una Unplugged', 2)).toBe(false);
    expect(toggle('Una Unplugged', 2)!.textContent).toContain('Skipped');
  });

  it('keeps Save disabled until something has changed', async () => {
    await mount();
    expect(saveFor('Mia Multi')!.disabled).toBe(true);

    await click(toggle('Mia Multi', 2));
    expect(saveFor('Mia Multi')!.disabled).toBe(false);
    await click(toggle('Mia Multi', 2));
    expect(saveFor('Mia Multi')!.disabled).toBe(true);
    expect(writes()).toEqual([]);
  });

  it('captures one of three screens: switching two off sends exactly those two', async () => {
    await mount();
    await click(toggle('Mia Multi', 2));
    await click(toggle('Mia Multi', 3));
    expect(on('Mia Multi', 1)).toBe(true);
    expect(on('Mia Multi', 2)).toBe(false);
    await click(saveFor('Mia Multi'));

    expect(writes()).toEqual([{
      method: 'PUT',
      path: expect.stringMatching(/\/screenshot-displays\/1$/),
      body: { screens: [{ number: 2, capture_enabled: false }, { number: 3, capture_enabled: false }] },
    }]);
    expect(showToast).toHaveBeenCalledWith('Screen capture saved for Mia Multi.', 'success');
    expect(on('Mia Multi', 2)).toBe(false);
    expect(on('Mia Multi', 1)).toBe(true);
    expect(saveFor('Mia Multi')!.disabled).toBe(true);
    // Nobody else was touched.
    expect(on('Dan Dual', 2)).toBe(true);
  });

  it('sends only the screens that changed', async () => {
    await mount();
    await click(toggle('Dan Dual', 2));
    await click(saveFor('Dan Dual'));
    await click(toggle('Dan Dual', 2));
    await click(saveFor('Dan Dual'));

    expect(writes().map((w) => w.body)).toEqual([
      { screens: [{ number: 2, capture_enabled: false }] },
      { screens: [{ number: 2, capture_enabled: true }] },
    ]);
  });

  it('will not offer to switch off the last connected screen, and says where that is done', async () => {
    await mount();
    await click(toggle('Dan Dual', 1));
    await click(toggle('Dan Dual', 2));

    expect(saveFor('Dan Dual')!.disabled).toBe(true);
    const alert = container.querySelector('[role="alert"]')!;
    expect(alert.textContent).toContain('Leave at least one connected screen on');
    expect(alert.textContent).toContain('User Management');
    await click(saveFor('Dan Dual'));
    expect(writes()).toEqual([]);

    await click(toggle('Dan Dual', 2));
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(saveFor('Dan Dual')!.disabled).toBe(false);
  });

  it('lets an unplugged screen be decided ahead of its return, without counting it as one left on', async () => {
    await mount();
    await showAll();
    // Una has one connected screen (1). Screen 2 is unplugged and currently skipped.
    await click(toggle('Una Unplugged', 2));
    expect(saveFor('Una Unplugged')!.disabled).toBe(false);
    await click(toggle('Una Unplugged', 2));
    // Switching the only connected screen off is refused even though a dormant one is on file.
    await click(toggle('Una Unplugged', 2));
    await click(toggle('Una Unplugged', 1));
    expect(container.querySelector('[role="alert"]')!.textContent).toContain('Leave at least one connected screen on');
    expect(saveFor('Una Unplugged')!.disabled).toBe(true);
  });

  it('puts the row back and toasts the server\'s words when the save is refused', async () => {
    await mount();
    await click(toggle('Mia Multi', 3));
    refuseNext = { status: 422, detail: 'Screen 3 has not been reported by Mia Multi\'s computer.' };
    await click(saveFor('Mia Multi'));

    expect(showToast).toHaveBeenCalledWith('Screen 3 has not been reported by Mia Multi\'s computer.', 'error');
    expect(on('Mia Multi', 3)).toBe(true);
    expect(saveFor('Mia Multi')!.disabled).toBe(true);
    expect(toggle('Mia Multi', 3)!.disabled).toBe(false);
  });

  it('searches by name or email', async () => {
    await mount();
    await type(container.querySelector('input[aria-label="Search members"]') as HTMLInputElement, 'dan');
    expect(toggle('Dan Dual', 1)).toBeTruthy();
    expect(toggle('Mia Multi', 1)).toBeNull();

    await type(container.querySelector('input[aria-label="Search members"]') as HTMLInputElement, 'nobody');
    expect(container.textContent).toContain('No members match your search.');
  });

  it('says plainly when nobody is using more than one screen, and offers the full list', async () => {
    members = [member(2, 'Sam Single', [screen(1)])];
    await mount();

    expect(container.textContent).toContain('No member is using more than one screen right now.');
    await showAll();
    expect(toggle('Sam Single', 1)).toBeTruthy();
  });

  it('invents nobody when no computer has reported', async () => {
    members = [];
    await mount();

    expect(container.textContent).toContain('No member');
    expect(container.textContent).toContain('has reported its screens yet');
    expect(container.querySelector('button[role="switch"]')).toBeNull();
  });

  it('says the screens could not be loaded, and offers a retry', async () => {
    listStatus = 500;
    await mount();

    expect(container.textContent).toContain('Could not load the screens.');
    expect(Array.from(container.querySelectorAll('button')).some((b) => b.textContent === 'Retry')).toBe(true);
  });
});
