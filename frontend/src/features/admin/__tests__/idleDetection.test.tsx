// @vitest-environment jsdom
/**
 * Screenshot Management -> Idle detection.
 *
 * The real section is rendered against a real RTK Query store with only `fetch`
 * replaced by a small stateful fake of the member endpoints, so what is pinned is
 * the request a browser would make:
 *
 * - each active member's switch and minutes come from the member (absent = on, 5);
 * - clients, the release account and inactive members are never listed;
 * - minutes outside 1-120, text and decimals are refused in words and block Save;
 * - Save is enabled only for a valid change and sends ONLY the field(s) changed,
 *   through the ordinary `PATCH /members/{id}`;
 * - a refused save puts the row back and toasts the server's message;
 * - the minutes box stays editable while the switch is off.
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

import { IdleDetection } from '../AdminIdleDetection';

type Body = Record<string, unknown>;
type Call = { method: string; path: string; body: Body | null };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const member = (id: number, name: string, extra: Body = {}): Body => ({
  id, name, email: `${name.split(' ')[0].toLowerCase()}@example.com`, role: 'employee', status: 'active',
  date_of_joining: null, date_of_birth: null, designation: '', ...extra,
});

describe('IdleDetection', () => {
  let container: HTMLDivElement;
  let root: Root;
  let calls: Call[];
  let members: Body[];
  let refuseNext: { status: number; detail: string } | null;

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
  const switchFor = (name: string) =>
    container.querySelector(`button[role="switch"][aria-label="Idle detection for ${name}"]`) as HTMLButtonElement | null;
  const minutesFor = (name: string) =>
    container.querySelector(`input[aria-label="Idle time in minutes for ${name}"]`) as HTMLInputElement | null;
  const saveFor = (name: string) =>
    container.querySelector(`button[aria-label="Save idle settings for ${name}"]`) as HTMLButtonElement | null;
  const writes = () => calls.filter((c) => c.method !== 'GET');

  const mount = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><IdleDetection /></Provider>); });
    await until(() => !!container.querySelector('button[role="switch"]') || container.textContent!.includes('Could not load'));
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    showToast.mockClear();
    calls = [];
    refuseNext = null;
    members = [
      member(1, 'Alice Active'),
      member(2, 'Bob Builder', { idle_enabled: false, idle_minutes: 15 }),
      member(3, 'Carl Casual', { idle_enabled: true, idle_minutes: 30 }),
      member(4, 'Dora Dormant', { status: 'inactive' }),
      member(5, 'Client Co', { role: 'client' }),
      member(6, 'Release Bot', { role: 'release_bot' }),
    ];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), 'http://localhost');
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      const raw = request ? await request.text() : (init?.body as string | undefined);
      const body = raw ? (JSON.parse(raw) as Body) : null;
      calls.push({ method, path: url.pathname, body });

      const one = url.pathname.match(/\/members\/(\d+)$/);
      if (one && method === 'PATCH') {
        if (refuseNext) { const r = refuseNext; refuseNext = null; return json({ detail: r.detail }, r.status); }
        const row = members.find((m) => m.id === Number(one[1]))!;
        Object.assign(row, body);
        return json(row);
      }
      if (url.pathname.endsWith('/members') && method === 'GET') {
        return json({ items: members, page: 1, limit: 100, total: members.length, pages: 1 });
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

  it('shows each active member\'s switch and minutes, defaulting an absent value to on and 5', async () => {
    await mount();

    expect(switchFor('Alice Active')!.getAttribute('aria-checked')).toBe('true');
    expect(minutesFor('Alice Active')!.value).toBe('5');
    expect(switchFor('Bob Builder')!.getAttribute('aria-checked')).toBe('false');
    expect(minutesFor('Bob Builder')!.value).toBe('15');
    expect(switchFor('Carl Casual')!.getAttribute('aria-checked')).toBe('true');
    expect(minutesFor('Carl Casual')!.value).toBe('30');
    expect(container.textContent).toContain('When idle detection is off, the desktop does not watch this member for inactivity.');
    expect(container.textContent).toContain('A change reaches a running desktop within a few minutes.');
  });

  it('never lists a client, the release account or an inactive member', async () => {
    await mount();

    expect(switchFor('Dora Dormant')).toBeNull();
    expect(switchFor('Client Co')).toBeNull();
    expect(switchFor('Release Bot')).toBeNull();
    expect(container.textContent).not.toContain('Client Co');
    expect(container.textContent).not.toContain('Release Bot');
  });

  it('searches by name or email', async () => {
    await mount();
    await type(container.querySelector('input[aria-label="Search members"]') as HTMLInputElement, 'bob');
    expect(switchFor('Bob Builder')).toBeTruthy();
    expect(switchFor('Alice Active')).toBeNull();

    await type(container.querySelector('input[aria-label="Search members"]') as HTMLInputElement, 'nobody');
    expect(container.textContent).toContain('No members match your search.');
  });

  it('paginates the table', async () => {
    members = Array.from({ length: 25 }, (_, i) => member(100 + i, `Person${String.fromCharCode(65 + i)} Test`));
    await mount();

    expect(switchFor('PersonA Test')).toBeTruthy();
    expect(switchFor('PersonY Test')).toBeNull();
    expect(container.textContent).toContain('1 to 20 of 25');
  });

  it('keeps Save disabled until something has changed', async () => {
    await mount();
    expect(saveFor('Alice Active')!.disabled).toBe(true);

    await type(minutesFor('Alice Active')!, '10');
    expect(saveFor('Alice Active')!.disabled).toBe(false);
    await type(minutesFor('Alice Active')!, '5');
    expect(saveFor('Alice Active')!.disabled).toBe(true);

    await click(switchFor('Alice Active'));
    expect(saveFor('Alice Active')!.disabled).toBe(false);
    await click(switchFor('Alice Active'));
    expect(saveFor('Alice Active')!.disabled).toBe(true);
    expect(writes()).toEqual([]);
  });

  it.each([
    ['zero', '0', 'Idle time must be at least 1.'],
    ['above the maximum', '121', 'Idle time must be at most 120.'],
    ['text', 'abc', 'Idle time must be a whole number.'],
    ['a decimal', '2.5', 'Idle time must be a whole number.'],
    ['empty', '', 'Idle time must be a whole number.'],
  ])('refuses %s minutes in words and blocks Save', async (_name, value, message) => {
    await mount();
    await type(minutesFor('Alice Active')!, value);

    expect(saveFor('Alice Active')!.disabled).toBe(true);
    const field = minutesFor('Alice Active')!;
    expect(field.getAttribute('aria-invalid')).toBe('true');
    expect(container.querySelector(`#${field.getAttribute('aria-describedby')}`)!.textContent).toBe(message);
    await click(saveFor('Alice Active'));
    expect(writes()).toEqual([]);
  });

  it('accepts the limits 1 and 120', async () => {
    await mount();
    await type(minutesFor('Alice Active')!, '1');
    expect(saveFor('Alice Active')!.disabled).toBe(false);
    await type(minutesFor('Alice Active')!, '120');
    expect(saveFor('Alice Active')!.disabled).toBe(false);
    expect(minutesFor('Alice Active')!.getAttribute('aria-invalid')).toBeNull();
  });

  it('sends only idle_minutes when only the minutes changed', async () => {
    await mount();
    await type(minutesFor('Carl Casual')!, '45');
    await click(saveFor('Carl Casual'));

    expect(writes()).toEqual([
      { method: 'PATCH', path: expect.stringMatching(/\/members\/3$/), body: { idle_minutes: 45 } },
    ]);
    expect(showToast).toHaveBeenCalledWith('Idle detection saved for Carl Casual.', 'success');
    expect(minutesFor('Carl Casual')!.value).toBe('45');
    expect(saveFor('Carl Casual')!.disabled).toBe(true);
  });

  it('turning idle detection off sends idle_enabled:false only', async () => {
    await mount();
    await click(switchFor('Alice Active'));
    expect(switchFor('Alice Active')!.getAttribute('aria-checked')).toBe('false');
    await click(saveFor('Alice Active'));

    expect(writes()).toEqual([
      { method: 'PATCH', path: expect.stringMatching(/\/members\/1$/), body: { idle_enabled: false } },
    ]);
    expect(switchFor('Alice Active')!.getAttribute('aria-checked')).toBe('false');
    // Nobody else was touched.
    expect(switchFor('Carl Casual')!.getAttribute('aria-checked')).toBe('true');
  });

  it('sends both fields when both changed, and nothing else', async () => {
    await mount();
    await click(switchFor('Bob Builder'));
    await type(minutesFor('Bob Builder')!, '20');
    await click(saveFor('Bob Builder'));

    expect(writes()[0].body).toEqual({ idle_enabled: true, idle_minutes: 20 });
  });

  it('leaves the minutes box editable, but muted, while idle detection is off', async () => {
    await mount();
    const box = minutesFor('Bob Builder')!;
    expect(box.disabled).toBe(false);
    expect(box.className).toContain('text-slate-400');
    expect(minutesFor('Carl Casual')!.className).not.toContain('text-slate-400');

    await type(box, '25');
    expect(saveFor('Bob Builder')!.disabled).toBe(false);
    await click(saveFor('Bob Builder'));
    expect(writes()[0].body).toEqual({ idle_minutes: 25 });
  });

  it('puts the row back and toasts the server\'s message when the save is refused', async () => {
    await mount();
    await click(switchFor('Alice Active'));
    await type(minutesFor('Alice Active')!, '60');
    refuseNext = { status: 422, detail: 'Idle time is not acceptable.' };
    await click(saveFor('Alice Active'));

    expect(showToast).toHaveBeenCalledWith('Idle time is not acceptable.', 'error');
    expect(switchFor('Alice Active')!.getAttribute('aria-checked')).toBe('true');
    expect(minutesFor('Alice Active')!.value).toBe('5');
    expect(saveFor('Alice Active')!.disabled).toBe(true);
    expect(switchFor('Alice Active')!.disabled).toBe(false);
  });

  it('says the members could not be loaded, and offers a retry', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'nope' }, 403)));
    await mount();
    expect(container.textContent).toContain('Could not load the members.');
    expect(Array.from(container.querySelectorAll('button')).some((b) => b.textContent === 'Retry')).toBe(true);
  });
});
