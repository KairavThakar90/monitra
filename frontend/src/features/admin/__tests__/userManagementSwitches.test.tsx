// @vitest-environment jsdom
/**
 * Settings -> User Management: the three desktop switches per member --
 * Screenshot capture, Desktop notifications and Activity tracking.
 *
 * The real page is rendered against a real RTK Query store with only `fetch`
 * replaced by a small stateful fake of the member endpoints, so what is pinned
 * is the request a browser would make:
 *
 * - each switch (and the compact On/Off pill in the list) shows the member's own
 *   value, an absent value meaning on;
 * - changing one switch sends that field, beside what the page already sent
 *   (the capture frequency), and never either of the other two;
 * - a refused save keeps the drawer open with the server's message and leaves
 *   the member's list row as it was;
 * - assigning to several members leaves a switch alone unless it is set.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

const showToast = vi.fn();
vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction: async () => true }),
}));

import { AdminUserManagement } from '../AdminUserManagement';

type Body = Record<string, unknown>;
type Call = { method: string; path: string; body: Body | null };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const member = (id: number, name: string, extra: Body = {}): Body => ({
  id, name, email: `${name.split(' ')[0].toLowerCase()}@example.com`, role: 'employee', status: 'active',
  date_of_joining: null, date_of_birth: null, designation: 'Dev',
  capture_frequency: 10, idle_enabled: true, idle_minutes: 5, ...extra,
});

const SWITCHES = [
  { label: 'Screenshot capture', field: 'screenshots_enabled', pill: 'Screenshots' },
  { label: 'Desktop notifications', field: 'notifications_enabled', pill: 'Notifications' },
  { label: 'Activity tracking', field: 'activity_tracking_enabled', pill: 'Activity' },
] as const;

describe('AdminUserManagement: desktop switches', () => {
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
    await act(async () => { (el as HTMLElement).click(); });
    await settle();
  };
  const row = (name: string) =>
    Array.from(container.querySelectorAll('tbody tr')).find((tr) => tr.textContent?.includes(name)) as HTMLElement;
  const pills = (name: string) => row(name).textContent ?? '';
  const configure = (name: string) =>
    Array.from(row(name).querySelectorAll('button')).find((b) => b.textContent?.includes('Configure'));
  const toggle = (label: string) =>
    container.querySelector(`input[role="switch"][aria-label="${label}"]`) as HTMLInputElement | null;
  const bulkSelect = (label: string) => container.querySelector(`select[aria-label="${label}"]`) as HTMLSelectElement | null;
  const saveButton = () =>
    Array.from(container.querySelectorAll('button')).find((b) => /^(Save Changes|Saving…)$/.test(b.textContent ?? '')) as HTMLButtonElement;
  const writes = () => calls.filter((c) => c.method !== 'GET');
  const choose = async (el: HTMLSelectElement, value: string) => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(el, value);
    await act(async () => { el.dispatchEvent(new Event('change', { bubbles: true })); });
  };

  const mount = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><AdminUserManagement /></Provider>); });
    await until(() => !!configure('Alice Active'));
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    showToast.mockClear();
    calls = [];
    refuseNext = null;
    members = [
      member(1, 'Alice Active'),
      member(2, 'Bob Builder', { screenshots_enabled: false, notifications_enabled: true, activity_tracking_enabled: true }),
      member(3, 'Carl Casual', { screenshots_enabled: true, notifications_enabled: false, activity_tracking_enabled: false }),
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
        const target = members.find((m) => m.id === Number(one[1]))!;
        Object.assign(target, body);
        return json(target);
      }
      if (url.pathname.endsWith('/members') && method === 'GET') {
        return json({ items: members, page: 1, limit: 10, total: members.length, pages: 1 });
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

  it('shows each member\'s three switches as compact On/Off indicators, an absent value being on', async () => {
    await mount();

    expect(pills('Alice Active')).toContain('Screenshots On');
    expect(pills('Alice Active')).toContain('Notifications On');
    expect(pills('Alice Active')).toContain('Activity On');
    expect(pills('Bob Builder')).toContain('Screenshots Off');
    expect(pills('Bob Builder')).toContain('Notifications On');
    expect(pills('Carl Casual')).toContain('Screenshots On');
    expect(pills('Carl Casual')).toContain('Notifications Off');
    expect(pills('Carl Casual')).toContain('Activity Off');
    // The capture-frequency pill is still there.
    expect(pills('Alice Active')).toContain('10 min');
  });

  it('no longer edits idle detection: that is set in Screenshot Management, in one place', async () => {
    await mount();

    const headers = Array.from(container.querySelectorAll('th')).map((th) => th.textContent!.trim());
    expect(headers).toContain('Screenshot Frequency');
    expect(headers).toContain('Desktop Switches');
    expect(headers.some((header) => /idle/i.test(header))).toBe(false);

    await click(configure('Alice Active'));
    const drawer = container.textContent!;
    expect(drawer).not.toMatch(/Idle Time (Monitoring|Threshold)/);
    expect(container.querySelector('input[type="number"][max="120"]')).toBeNull();
    // The member's own capture frequency is still editable here.
    expect(drawer).toContain('Screenshot Capture Frequency');
  });

  it('renders each switch in the drawer from the member\'s value, with its help text', async () => {
    await mount();
    await click(configure('Carl Casual'));

    expect(toggle('Screenshot capture')!.checked).toBe(true);
    expect(toggle('Desktop notifications')!.checked).toBe(false);
    expect(toggle('Activity tracking')!.checked).toBe(false);
    const text = container.textContent!;
    expect(text).toContain('Capture periodic screenshots while this member is tracking time. Screenshots already taken are kept.');
    expect(text).toContain(
      "Show this member reminders, messages from the administrator and routine status notices. Alerts that need attention are always shown.",
    );
    expect(text).toContain(
      "Measure keyboard and mouse activity, and the applications and websites used, while tracking time. Idle detection is managed in Screenshot Management.",
    );
    expect(text).toContain('Changes take effect on a running desktop within a few minutes.');
    expect(text).toContain('Choose what the Monitra desktop app does for this member.');
  });

  it('treats an absent value as on in the drawer', async () => {
    await mount();
    await click(configure('Alice Active'));
    SWITCHES.forEach(({ label }) => expect(toggle(label)!.checked).toBe(true));
  });

  it('keeps Save disabled until something changes', async () => {
    await mount();
    await click(configure('Alice Active'));
    expect(saveButton().disabled).toBe(true);
    await click(toggle('Activity tracking'));
    expect(saveButton().disabled).toBe(false);
    await click(toggle('Activity tracking'));
    expect(saveButton().disabled).toBe(true);
  });

  it.each(SWITCHES)('turning off $label sends $field and neither of the other two switches', async ({ label, field }) => {
    await mount();
    await click(configure('Alice Active'));
    await click(toggle(label));
    expect(toggle(label)!.checked).toBe(false);
    SWITCHES.filter((other) => other.label !== label).forEach((other) => expect(toggle(other.label)!.checked).toBe(true));
    await click(saveButton());

    expect(writes()).toHaveLength(1);
    expect(writes()[0].method).toBe('PATCH');
    expect(writes()[0].path).toMatch(/\/members\/1$/);
    // What the page already sent, plus only the switch that was changed.
    expect(writes()[0].body).toEqual({ capture_frequency: 10, [field]: false });
    SWITCHES.filter((other) => other.field !== field).forEach((other) =>
      expect(Object.keys(writes()[0].body!)).not.toContain(other.field));
    expect(showToast).toHaveBeenCalledWith('Tracking settings saved for Alice Active.', 'success');
    // The drawer closes and the list shows the new state.
    expect(toggle(label)).toBeNull();
  });

  it('turning a switch back on sends true for that field only', async () => {
    await mount();
    await click(configure('Bob Builder'));
    await click(toggle('Screenshot capture'));
    await click(saveButton());

    expect(writes()[0].body).toEqual({ capture_frequency: 10, screenshots_enabled: true });
    expect(pills('Bob Builder')).toContain('Screenshots On');
  });

  it('shows the new state in the list after a save, leaving the other members alone', async () => {
    await mount();
    await click(configure('Alice Active'));
    await click(toggle('Desktop notifications'));
    await click(saveButton());

    expect(pills('Alice Active')).toContain('Notifications Off');
    expect(pills('Alice Active')).toContain('Screenshots On');
    expect(pills('Carl Casual')).toContain('Notifications Off');
    expect(pills('Bob Builder')).toContain('Notifications On');
  });

  it('keeps the drawer open with the server\'s message, and the row as it was, when the save is refused', async () => {
    await mount();
    await click(configure('Alice Active'));
    await click(toggle('Screenshot capture'));
    refuseNext = { status: 422, detail: 'Screenshots could not be changed.' };
    await click(saveButton());

    expect(showToast).toHaveBeenCalledWith('Screenshots could not be changed.', 'error');
    expect(container.textContent).toContain('Screenshots could not be changed.');
    expect(toggle('Screenshot capture')).toBeTruthy();
    expect(pills('Alice Active')).toContain('Screenshots On');
    expect(members[0].screenshots_enabled).toBeUndefined();
    // The other switches did not move.
    expect(toggle('Desktop notifications')!.checked).toBe(true);
    expect(toggle('Activity tracking')!.checked).toBe(true);
  });

  it('assigning to several members offers "Leave unchanged" and sends only the switch that was set', async () => {
    await mount();
    await click(container.querySelector('input[aria-label="Select Alice Active"]'));
    await click(container.querySelector('input[aria-label="Select Bob Builder"]'));
    await click(Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.includes('Assign to 2 selected')));

    SWITCHES.forEach(({ label }) => expect(bulkSelect(label)!.value).toBe('keep'));
    await choose(bulkSelect('Activity tracking')!, 'off');
    await click(saveButton());

    expect(writes().map((w) => w.path.replace(/.*\/members\//, ''))).toEqual(['1', '2']);
    writes().forEach((w) =>
      expect(w.body).toEqual({ capture_frequency: 10, activity_tracking_enabled: false }));
  });

  it('assigning to several members without setting any switch sends none of them', async () => {
    await mount();
    await click(container.querySelector('input[aria-label="Select Alice Active"]'));
    await click(container.querySelector('input[aria-label="Select Bob Builder"]'));
    await click(Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.includes('Assign to 2 selected')));
    await click(saveButton());

    writes().forEach((w) => SWITCHES.forEach(({ field }) => expect(Object.keys(w.body!)).not.toContain(field)));
  });
});
