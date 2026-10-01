// @vitest-environment jsdom
/**
 * Admin Settings -> Desktop Notifications.
 *
 * The real page is rendered against a real RTK Query store with only `fetch`
 * replaced by a small stateful fake of the backend, so what is pinned is the
 * request a browser would make and what the administrator sees afterwards:
 *
 * - every built-in reminder is listed, on, with its cadence or time;
 * - the Send checkbox is the whole send / don't-send decision: it PUTs
 *   `{enabled}`, shows the new state at once, and rolls back if refused;
 * - a custom notification is created, switched, edited and deleted;
 * - a bad title / message / time / no day is refused before any request;
 * - editing a built-in sends only what changed, and an interval reminder has
 *   no time field.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

const showToast = vi.fn();
let confirmAnswer = true;

vi.mock('../../dashboard/v2/V2Shell', () => ({
  // `actions` hold the page's "+ Add Notification" button, so they are rendered too.
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction: async () => confirmAnswer }),
}));

import { AdminDesktopNotifications } from '../AdminDesktopNotifications';

type Body = Record<string, unknown>;

const builtin = (key: string, label: string, kind: 'interval' | 'daily', extra: Body = {}) => ({
  key, label, description: `${label} description`, kind,
  every_minutes: kind === 'interval' ? 60 : null,
  default_time: kind === 'daily' ? '13:30' : null,
  enabled: true, time: kind === 'daily' ? '13:30' : null, weekdays: [0, 1, 2, 3, 4, 5, 6],
  ...extra,
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

describe('AdminDesktopNotifications', () => {
  let container: HTMLDivElement;
  let root: Root;
  let state: { version: number; updated_at: string | null; updated_by_username: string | null; builtin: any[]; custom: any[] };
  let calls: { method: string; path: string; body: Body | null }[];
  let refuseNext: { status: number; detail: string } | null;

  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const click = async (el: Element | null | undefined) => {
    expect(el).toBeTruthy();
    await act(async () => { el!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    await settle();
  };
  const type = async (el: HTMLInputElement | HTMLTextAreaElement, value: string) => {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value')!.set!.call(el, value);
    await act(async () => { el.dispatchEvent(new Event('input', { bubbles: true })); });
  };
  const box = (label: string) =>
    container.querySelector(`input[type="checkbox"][aria-label="Send ${label}"]`) as HTMLInputElement | null;
  const button = (text: string) =>
    Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.trim() === text) as HTMLButtonElement | undefined;
  const rowOf = (label: string) => box(label)!.closest('tr, [data-testid="custom-card"]') as HTMLElement;
  const dialog = () => container.querySelector('[role="dialog"]') as HTMLElement | null;
  const writes = () => calls.filter((c) => c.method !== 'GET');

  const bump = () => { state.version += 1; state.updated_by_username = 'grace'; state.updated_at = '2026-10-01T09:00:00Z'; };

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    showToast.mockClear();
    confirmAnswer = true;
    calls = [];
    refuseNext = null;
    state = {
      version: 0, updated_at: null, updated_by_username: null,
      builtin: [
        builtin('hydrate', 'Drink water', 'interval'),
        builtin('lunch', 'Lunch break', 'daily'),
      ],
      custom: [],
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), 'http://localhost');
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      const raw = request ? await request.text() : (init?.body as string | undefined);
      const body = raw ? (JSON.parse(raw) as Body) : null;
      calls.push({ method, path: url.pathname, body });
      if (!url.pathname.includes('/desktop-notifications')) return json({}, 404);
      if (method === 'GET') return json(state);
      if (refuseNext) {
        const refusal = refuseNext;
        refuseNext = null;
        return json({ detail: refusal.detail }, refusal.status);
      }
      const builtinMatch = url.pathname.match(/\/builtin\/([^/]+)$/);
      const customMatch = url.pathname.match(/\/custom\/([^/]+)$/);
      if (builtinMatch && method === 'PUT') {
        Object.assign(state.builtin.find((b) => b.key === builtinMatch[1])!, body);
      } else if (url.pathname.endsWith('/custom') && method === 'POST') {
        state.custom.push({ id: `c${state.custom.length + 1}`, created_at: null, updated_at: null, created_by: 'grace', ...body });
      } else if (customMatch && method === 'PATCH') {
        Object.assign(state.custom.find((c) => c.id === customMatch[1])!, body);
      } else if (customMatch && method === 'DELETE') {
        state.custom = state.custom.filter((c) => c.id !== customMatch[1]);
      }
      bump();
      return json(state);
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><AdminDesktopNotifications /></Provider>); });
    await settle();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('lists every built-in reminder, on, with its cadence or time and days', () => {
    expect(box('Drink water')!.checked).toBe(true);
    expect(box('Lunch break')!.checked).toBe(true);
    expect(rowOf('Drink water').textContent).toContain('Every 60 min while working');
    expect(rowOf('Lunch break').textContent).toContain('13:30 IST');
    expect(rowOf('Lunch break').textContent).toContain('Every day');
    expect(container.textContent).toContain('Built-in reminders (2 of 2 on)');
    expect(container.textContent).toContain('No custom notifications yet.');
  });

  it('puts the Add Notification button in the page header, above the lists, and offers it in the empty state too', () => {
    const buttons = Array.from(container.querySelectorAll('button'));
    const header = buttons.find((b) => b.textContent === '+ Add Notification')!;
    expect(header).toBeTruthy();
    // Header action: it comes before everything else on the page.
    expect(buttons[0]).toBe(header);
    expect(buttons.some((b) => b.textContent === '+ Add your first notification')).toBe(true);
  });

  it('lists custom notifications above the built-in reminders', async () => {
    state.custom.push({ id: 'c1', title: 'Standup', message: 'Soon.', time: '10:25', weekdays: [0, 1, 2, 3, 4], enabled: true, created_at: null, updated_at: null, created_by: 'grace' });
    await act(async () => { root.unmount(); });
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><AdminDesktopNotifications /></Provider>); });
    await settle();

    const text = container.textContent!;
    expect(text.indexOf('Custom notifications (1 of 1 on)')).toBeGreaterThan(-1);
    expect(text.indexOf('Custom notifications')).toBeLessThan(text.indexOf('Built-in reminders'));
    expect(container.querySelectorAll('[data-testid="custom-card"]')).toHaveLength(1);
    expect(text).not.toContain('+ Add your first notification');
  });

  it('unticking Send switches a reminder off at once and sends exactly {enabled:false}', async () => {
    await click(box('Drink water'));

    expect(writes()).toEqual([{ method: 'PUT', path: expect.stringMatching(/\/builtin\/hydrate$/), body: { enabled: false } }]);
    expect(box('Drink water')!.checked).toBe(false);
    expect(box('Lunch break')!.checked).toBe(true);
    expect(container.textContent).toContain('Built-in reminders (1 of 2 on)');
    expect(container.textContent).toContain('Last changed by grace');

    await click(box('Drink water'));
    expect(writes()[1].body).toEqual({ enabled: true });
    expect(box('Drink water')!.checked).toBe(true);
  });

  it('puts the checkbox back and says why when the server refuses', async () => {
    refuseNext = { status: 403, detail: 'Insufficient permissions for this action' };

    await click(box('Drink water'));

    expect(box('Drink water')!.checked).toBe(true);
    expect(showToast).toHaveBeenCalledWith('Insufficient permissions for this action', 'error');
  });

  it('creates a custom notification and lists it', async () => {
    await click(button('+ Add Notification'));
    await type(container.querySelector('#notification-title') as HTMLInputElement, 'Daily standup');
    await type(container.querySelector('#notification-message') as HTMLTextAreaElement, 'Standup in 5 minutes.');
    await type(container.querySelector('#notification-time') as HTMLInputElement, '10:25');
    await click(button('Mon–Fri'));
    await click(button('Save'));

    expect(writes()).toEqual([{
      method: 'POST', path: expect.stringMatching(/\/custom$/),
      body: { title: 'Daily standup', message: 'Standup in 5 minutes.', time: '10:25', weekdays: [0, 1, 2, 3, 4], enabled: true },
    }]);
    expect(dialog()).toBeNull();
    expect(box('Daily standup')!.checked).toBe(true);
    expect(rowOf('Daily standup').textContent).toContain('10:25 IST');
    expect(rowOf('Daily standup').textContent).toContain('Mon–Fri');
    expect(showToast).toHaveBeenCalledWith('Notification created.', 'success');
  });

  it('refuses a bad title, message, time or no day before sending anything', async () => {
    await click(button('+ Add Notification'));
    await type(container.querySelector('#notification-title') as HTMLInputElement, '!!!');
    await type(container.querySelector('#notification-message') as HTMLTextAreaElement, '<script>x</script>');
    await type(container.querySelector('#notification-time') as HTMLInputElement, '');
    // Clear every day.
    for (const day of ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']) await click(button(day));
    await click(button('Save'));

    expect(writes()).toEqual([]);
    const text = dialog()!.textContent!;
    expect(text).toContain('Title');
    expect(text).toContain('Message');
    expect(text).toContain('Time is required.');
    expect(text).toContain('Choose at least one day.');
    expect(dialog()).not.toBeNull();
  });

  it('shows a server refusal inside the dialog, in the server\'s words', async () => {
    refuseNext = { status: 409, detail: 'There can be at most 50 custom notifications. Delete one first.' };
    await click(button('+ Add Notification'));
    await type(container.querySelector('#notification-title') as HTMLInputElement, 'Standup');
    await type(container.querySelector('#notification-message') as HTMLTextAreaElement, 'Soon.');
    await click(button('Save'));

    expect(dialog()!.textContent).toContain('There can be at most 50 custom notifications');
  });

  it('switches a custom notification, edits it, and deletes it after confirming', async () => {
    state.custom.push({ id: 'c1', title: 'Standup', message: 'Soon.', time: '10:25', weekdays: [0, 1, 2, 3, 4], enabled: true, created_at: null, updated_at: null, created_by: 'grace' });
    await act(async () => { root.unmount(); });
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><AdminDesktopNotifications /></Provider>); });
    await settle();

    await click(box('Standup'));
    expect(writes()[0]).toEqual({ method: 'PATCH', path: expect.stringMatching(/\/custom\/c1$/), body: { enabled: false } });
    expect(box('Standup')!.checked).toBe(false);

    await click(Array.from(rowOf('Standup').querySelectorAll('button')).find((b) => b.textContent === 'Edit'));
    await type(container.querySelector('#notification-time') as HTMLInputElement, '11:00');
    await click(button('Save'));
    expect(writes()[1].method).toBe('PATCH');
    expect(writes()[1].body).toMatchObject({ time: '11:00', title: 'Standup' });
    expect(rowOf('Standup').textContent).toContain('11:00 IST');

    confirmAnswer = false;
    await click(Array.from(rowOf('Standup').querySelectorAll('button')).find((b) => b.textContent === 'Delete'));
    expect(writes()).toHaveLength(2); // declined: nothing sent

    confirmAnswer = true;
    await click(Array.from(rowOf('Standup').querySelectorAll('button')).find((b) => b.textContent === 'Delete'));
    expect(writes()[2]).toMatchObject({ method: 'DELETE', path: expect.stringMatching(/\/custom\/c1$/) });
    expect(box('Standup')).toBeNull();
    expect(container.textContent).toContain('No custom notifications yet.');
  });

  it('edits a daily built-in sending only what changed, and an interval one has no time field', async () => {
    await click(Array.from(rowOf('Lunch break').querySelectorAll('button')).find((b) => b.textContent === 'Edit'));
    expect(dialog()!.querySelector('#notification-time')).not.toBeNull();
    await type(dialog()!.querySelector('#notification-time') as HTMLInputElement, '13:45');
    await click(button('Save'));
    expect(writes()).toEqual([{ method: 'PUT', path: expect.stringMatching(/\/builtin\/lunch$/), body: { time: '13:45' } }]);
    expect(rowOf('Lunch break').textContent).toContain('13:45 IST');

    await click(Array.from(rowOf('Drink water').querySelectorAll('button')).find((b) => b.textContent === 'Edit'));
    expect(dialog()!.querySelector('#notification-time')).toBeNull();
    expect(dialog()!.textContent).toContain('no fixed');
    await click(button('Sat'));
    await click(button('Sun'));
    await click(button('Save'));
    expect(writes()[1]).toEqual({ method: 'PUT', path: expect.stringMatching(/\/builtin\/hydrate$/), body: { weekdays: [0, 1, 2, 3, 4] } });
    expect(rowOf('Drink water').textContent).toContain('Mon–Fri');
  });

  it('sends nothing when a built-in is saved without a change', async () => {
    await click(Array.from(rowOf('Lunch break').querySelectorAll('button')).find((b) => b.textContent === 'Edit'));
    await click(button('Save'));
    expect(writes()).toEqual([]);
    expect(dialog()).toBeNull();
  });

  it('puts a built-in back to its default time and every day with Reset to default', async () => {
    state.builtin[1] = builtin('lunch', 'Lunch break', 'daily', { time: '13:45', weekdays: [0, 1] });
    await act(async () => { root.unmount(); });
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><AdminDesktopNotifications /></Provider>); });
    await settle();

    await click(Array.from(rowOf('Lunch break').querySelectorAll('button')).find((b) => b.textContent === 'Edit'));
    await click(button('Reset to default'));
    await click(button('Save'));

    expect(writes()[0].body).toEqual({ time: '13:30', weekdays: [0, 1, 2, 3, 4, 5, 6] });
  });
});
