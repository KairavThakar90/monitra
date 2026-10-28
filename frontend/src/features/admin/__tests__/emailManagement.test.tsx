// @vitest-environment jsdom
/**
 * Settings -> Email Management.
 *
 * The real page is rendered against a real RTK Query store with only `fetch` replaced
 * by a small stateful fake of `/email-cc`, so what is pinned is the request a browser
 * would make:
 *
 * - the page lists every email the server describes (number, who it goes to, when it is
 *   sent) and one column per CC address, ticked exactly where the server says;
 * - the two private-link emails are shown but locked, with nothing to tick;
 * - a tick is copied only on that email, and nothing is sent until Save;
 * - an address can be added (validated, listed once) and removed, and a new one starts
 *   with nothing ticked;
 * - Save sends the whole list (a replacement) with each address's emails, Discard puts
 *   the saved state back, and a refused save keeps the unsaved ticks and says why;
 * - an untouched deployment says so, and an emptied list says nobody is copied.
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
vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import { AdminEmailManagement } from '../AdminEmailManagement';

type Body = Record<string, unknown>;
type Call = { method: string; path: string; body: Body | null };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const trigger = (number: number, key: string, name: string, extra: Body = {}) => ({
  key, number, name, goes_to: `Goes to ${name}`, trigger: `When ${name}`, cc_allowed: true, note: null, active: true, ...extra,
});

const TRIGGERS = [
  trigger(1, 'welcome', 'Welcome'),
  trigger(2, 'feedback', 'Feedback received'),
  trigger(3, 'release', 'New version available', { active: false, note: 'Off by default (RELEASE_EMAIL_ENABLED).' }),
  trigger(4, 'client_invitation', 'Client invitation', { cc_allowed: false, note: 'Carries a private link, so it is never copied to anyone else.' }),
  trigger(5, 'client_login_link', 'Client sign-in link', { cc_allowed: false, note: 'Carries a private link, so it is never copied to anyone else.' }),
];
const COPYABLE = ['welcome', 'feedback', 'release'];

describe('AdminEmailManagement', () => {
  let container: HTMLDivElement;
  let root: Root;
  let calls: Call[];
  let state: { addresses: { email: string; triggers: string[] }[]; version: number; is_default: boolean };
  let refuseNext: { status: number; detail: string } | null;
  let loadStatus: number;

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
  const cell = (email: string, name: string) =>
    container.querySelector(`input[aria-label="Copy ${email} on ${name}"]`) as HTMLInputElement | null;
  const ticked = (email: string, name: string) => cell(email, name)!.checked;
  const column = (email: string) =>
    container.querySelector(`input[aria-label="Copy ${email} on every email"]`) as HTMLInputElement | null;
  const row = (name: string) =>
    container.querySelector(`input[aria-label="Copy every address on ${name}"]`) as HTMLInputElement | null;
  const button = (text: string) =>
    Array.from(container.querySelectorAll('button')).find((b) => b.textContent === text) as HTMLButtonElement | undefined;
  const writes = () => calls.filter((c) => c.method !== 'GET');
  const newAddress = () => container.querySelector('input[aria-label="New CC address"]') as HTMLInputElement;
  const addAddress = async (value: string) => {
    await type(newAddress(), value);
    await click(button('Add address'));
  };

  const mount = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><AdminEmailManagement /></Provider>); });
    await until(() => !container.textContent!.includes('Loading emails'));
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.setItem('accessToken', 'tok');
    showToast.mockClear();
    calls = [];
    refuseNext = null;
    loadStatus = 200;
    state = {
      is_default: false, version: 2,
      addresses: [
        { email: 'hr@example.com', triggers: ['welcome', 'feedback'] },
        { email: 'boss@example.com', triggers: ['feedback'] },
      ],
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), 'http://localhost');
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      const raw = request ? await request.text() : (init?.body as string | undefined);
      const body = raw ? (JSON.parse(raw) as Body) : null;
      calls.push({ method, path: url.pathname, body });
      if (!url.pathname.endsWith('/email-cc')) return json({}, 404);

      const view = () => ({
        triggers: TRIGGERS, addresses: state.addresses, max_addresses: 3, is_default: state.is_default,
        version: state.version, updated_at: state.version ? '2026-10-27T08:30:00Z' : null,
        updated_by_username: state.version ? 'grace' : null,
      });
      if (method === 'GET') return loadStatus === 200 ? json(view()) : json({ detail: 'nope' }, loadStatus);
      if (refuseNext) { const r = refuseNext; refuseNext = null; return json({ detail: r.detail }, r.status); }
      state = { addresses: (body as { addresses: typeof state.addresses }).addresses, version: state.version + 1, is_default: false };
      return json(view());
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

  it('lists every email with who it goes to and when, in the server\'s order', async () => {
    await mount();

    const rows = Array.from(container.querySelectorAll('tbody tr'));
    expect(rows).toHaveLength(5);
    expect(rows.map((r) => r.querySelector('td')!.textContent)).toEqual(['1', '2', '3', '4', '5']);
    expect(container.textContent).toContain('Goes to Welcome');
    expect(container.textContent).toContain('When Feedback received');
  });

  it('ticks each address exactly where the server says it is copied', async () => {
    await mount();

    expect(ticked('hr@example.com', 'Welcome')).toBe(true);
    expect(ticked('hr@example.com', 'Feedback received')).toBe(true);
    expect(ticked('hr@example.com', 'New version available')).toBe(false);
    expect(ticked('boss@example.com', 'Welcome')).toBe(false);
    expect(ticked('boss@example.com', 'Feedback received')).toBe(true);
  });

  it('shows the two private-link emails locked, with nothing to tick', async () => {
    await mount();

    expect(cell('hr@example.com', 'Client invitation')).toBeNull();
    expect(cell('hr@example.com', 'Client sign-in link')).toBeNull();
    expect(row('Client invitation')).toBeNull();
    const text = container.textContent!;
    expect(text).toContain('No CC.');
    expect(text).toContain('never copied to anyone else');
  });

  it('says when a whole email is switched off in configuration, and still lets it be set', async () => {
    await mount();

    expect(container.textContent).toContain('Switched off in configuration');
    expect(cell('hr@example.com', 'New version available')).toBeTruthy();
  });

  it('counts the emails, the addresses and what each address is copied on', async () => {
    await mount();

    const text = container.textContent!;
    expect(text).toContain('Emails with a copy2 of 3');       // welcome and feedback have a copy; release does not
    expect(text).toContain('CC addresses2');
    const headers = Array.from(container.querySelectorAll('thead th')).map((th) => th.textContent);
    expect(headers.some((h) => h!.includes('hr@example.com') && h!.includes('2 of 3'))).toBe(true);
    expect(headers.some((h) => h!.includes('boss@example.com') && h!.includes('1 of 3'))).toBe(true);
  });

  it('sends nothing until Save, and keeps Save disabled until something changes', async () => {
    await mount();
    expect(button('Save changes')!.disabled).toBe(true);
    expect(container.textContent).toContain('Everything shown is saved.');

    await click(cell('boss@example.com', 'Welcome'));
    expect(button('Save changes')!.disabled).toBe(false);
    expect(container.textContent).toContain('You have unsaved changes.');
    await click(cell('boss@example.com', 'Welcome'));
    expect(button('Save changes')!.disabled).toBe(true);
    expect(writes()).toEqual([]);
  });

  it('copies an address only on the email that was ticked, and saves the whole list', async () => {
    await mount();
    await click(cell('boss@example.com', 'Welcome'));
    await click(cell('hr@example.com', 'Feedback received'));
    await click(button('Save changes'));

    expect(writes()).toEqual([{
      method: 'PUT',
      path: expect.stringMatching(/\/email-cc$/),
      body: { addresses: [
        { email: 'hr@example.com', triggers: ['welcome'] },
        { email: 'boss@example.com', triggers: ['welcome', 'feedback'] },
      ] },
    }]);
    expect(showToast).toHaveBeenCalledWith('Email management saved.', 'success');
    expect(button('Save changes')!.disabled).toBe(true);
    expect(ticked('boss@example.com', 'Welcome')).toBe(true);
    expect(ticked('hr@example.com', 'Feedback received')).toBe(false);
  });

  it('ticks a whole column or a whole row at once', async () => {
    await mount();
    await click(column('boss@example.com'));
    expect(['Welcome', 'Feedback received', 'New version available'].map((n) => ticked('boss@example.com', n)))
      .toEqual([true, true, true]);
    await click(column('boss@example.com'));
    expect(['Welcome', 'Feedback received', 'New version available'].map((n) => ticked('boss@example.com', n)))
      .toEqual([false, false, false]);

    await click(row('Welcome'));
    expect([ticked('hr@example.com', 'Welcome'), ticked('boss@example.com', 'Welcome')]).toEqual([true, true]);
    await click(row('Welcome'));
    expect([ticked('hr@example.com', 'Welcome'), ticked('boss@example.com', 'Welcome')]).toEqual([false, false]);
  });

  it('adds a new address with nothing ticked, lower-cased, and saves it', async () => {
    await mount();
    await addAddress('New.Person@Example.com');

    expect(cell('new.person@example.com', 'Welcome')).toBeTruthy();
    expect(ticked('new.person@example.com', 'Welcome')).toBe(false);
    expect(newAddress().value).toBe('');
    await click(cell('new.person@example.com', 'Welcome'));
    await click(button('Save changes'));

    expect((writes()[0].body as { addresses: unknown[] }).addresses[2]).toEqual(
      { email: 'new.person@example.com', triggers: ['welcome'] },
    );
  });

  it.each([
    ['text', 'not-an-address'],
    ['empty', ''],
    ['a header injection', 'x@example.com\r\nBcc: evil@x.com'],
  ])('refuses %s as a new address', async (_name, value) => {
    await mount();
    await addAddress(value);

    expect(container.querySelector('#cc-add-error')!.textContent).toBeTruthy();
    expect(container.querySelectorAll('thead th')).toHaveLength(5 + 2);
    expect(button('Save changes')!.disabled).toBe(true);
  });

  it('refuses an address that is already listed, whatever its case', async () => {
    await mount();
    await addAddress('HR@Example.com');

    expect(container.querySelector('#cc-add-error')!.textContent).toBe('That address is already in the list.');
    expect(container.querySelectorAll('thead th')).toHaveLength(5 + 2);
  });

  it('refuses a list longer than the server allows', async () => {
    await mount();
    await addAddress('third@example.com');
    expect(container.querySelector('#cc-add-error')).toBeNull();
    await addAddress('fourth@example.com');

    expect(container.querySelector('#cc-add-error')!.textContent).toBe('At most 3 addresses can be copied.');
  });

  it('removes an address from the grid, and from the server on Save', async () => {
    await mount();
    await click(container.querySelector('button[aria-label="Remove boss@example.com"]'));

    expect(cell('boss@example.com', 'Welcome')).toBeNull();
    await click(button('Save changes'));
    expect(writes()[0].body).toEqual({ addresses: [{ email: 'hr@example.com', triggers: ['welcome', 'feedback'] }] });
  });

  it('says nobody is copied when the list is emptied, and can still be saved', async () => {
    await mount();
    await click(container.querySelector('button[aria-label="Remove boss@example.com"]'));
    await click(container.querySelector('button[aria-label="Remove hr@example.com"]'));

    expect(container.textContent).toContain('No address is copied on any email.');
    expect(row('Welcome')).toBeTruthy();
    expect(row('Welcome')!.disabled).toBe(true);
    await click(button('Save changes'));
    expect(writes()[0].body).toEqual({ addresses: [] });
  });

  it('puts the saved state back on Discard', async () => {
    await mount();
    await click(cell('boss@example.com', 'Welcome'));
    await click(container.querySelector('button[aria-label="Remove hr@example.com"]'));
    await addAddress('extra@example.com');
    await click(button('Discard'));

    expect(cell('hr@example.com', 'Welcome')).toBeTruthy();
    expect(cell('extra@example.com', 'Welcome')).toBeNull();
    expect(ticked('boss@example.com', 'Welcome')).toBe(false);
    expect(button('Save changes')!.disabled).toBe(true);
    expect(writes()).toEqual([]);
  });

  it('keeps the unsaved ticks and says why when the server refuses the save', async () => {
    await mount();
    await click(cell('boss@example.com', 'Welcome'));
    refuseNext = { status: 422, detail: 'Each CC address may be listed once.' };
    await click(button('Save changes'));

    expect(showToast).toHaveBeenCalledWith('Each CC address may be listed once.', 'error');
    expect(ticked('boss@example.com', 'Welcome')).toBe(true);
    expect(button('Save changes')!.disabled).toBe(false);
  });

  it('says plainly when nothing has been saved yet', async () => {
    state = { is_default: true, version: 0, addresses: [{ email: 'hr@example.com', triggers: COPYABLE }] };
    await mount();

    expect(container.textContent).toContain('Nothing has been saved here yet.');
    expect(container.textContent).not.toContain('Last changed by');
  });

  it('names who last changed it', async () => {
    await mount();
    expect(container.textContent).toContain('Last changed by');
    expect(container.textContent).toContain('grace');
  });

  it('says it could not load, and offers a retry', async () => {
    loadStatus = 500;
    await mount();

    expect(container.textContent).toContain('Could not load email management.');
    expect(button('Retry')).toBeTruthy();
  });
});
