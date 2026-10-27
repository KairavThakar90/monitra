// @vitest-environment jsdom
/**
 * Settings -> User Management: the three desktop switches per member --
 * Screenshot capture, Desktop notifications and Activity tracking -- one column
 * each, drawn like the Members page's switches, with an all-members switch in
 * the column heading. The screenshot frequency is not edited here.
 *
 * The real page is rendered against a real RTK Query store with only `fetch`
 * replaced by a small stateful fake of the member endpoints, so what is pinned
 * is the request a browser would make:
 *
 * - each switch shows the member's own value, an absent value meaning on;
 * - a row's switch saves at once and sends only that field;
 * - ticking rows opens a toolbar that turns one switch on or off for them;
 * - a column heading's switch changes every active member, on every page;
 * - changing one drawer switch sends that field and never either of the other two;
 * - the page never sends or shows the capture frequency;
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
const confirmHolder: { fn: (title: string, message: string) => Promise<boolean> } = { fn: async () => true };
vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction: (title: string, message: string) => confirmHolder.fn(title, message) }),
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
  let confirm: ReturnType<typeof vi.fn>;

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
  const rowSwitch = (name: string, label: string) =>
    row(name).querySelector(`button[role="switch"][aria-label="${label} for ${name}"]`) as HTMLButtonElement | null;
  const rowOn = (name: string, label: string) => rowSwitch(name, label)!.getAttribute('aria-checked') === 'true';
  const headerSwitch = (label: string) =>
    container.querySelector(`thead button[role="switch"][aria-label="${label} for all members"]`) as HTMLButtonElement | null;
  const configure = (name: string) =>
    Array.from(row(name).querySelectorAll('button')).find((b) => b.textContent?.includes('Configure'));
  const toggle = (label: string) => {
    const el = container.querySelector(`button[role="switch"][aria-label="${label}"]`) as HTMLButtonElement | null;
    return el && { el, checked: el.getAttribute('aria-checked') === 'true' };
  };
  const saveButton = () =>
    Array.from(container.querySelectorAll('button')).find((b) => /^(Save Changes|Saving…)$/.test(b.textContent ?? '')) as HTMLButtonElement;
  const writes = () => calls.filter((c) => c.method !== 'GET');

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
    confirm = vi.fn(async () => true);
    confirmHolder.fn = confirm as unknown as typeof confirmHolder.fn;
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
        // The 100-per-page walk is served two at a time, so it has to go past the first page.
        const limit = Number(url.searchParams.get('limit') ?? 10);
        const page = Number(url.searchParams.get('page') ?? 1);
        const per = limit >= 100 ? 2 : limit;
        return json({
          items: members.slice((page - 1) * per, page * per),
          page, limit, total: members.length, pages: Math.ceil(members.length / per),
        });
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

  it('shows each member\'s three switches in their own columns, an absent value being on', async () => {
    await mount();

    SWITCHES.forEach(({ label }) => expect(rowOn('Alice Active', label)).toBe(true));
    expect(rowOn('Bob Builder', 'Screenshot capture')).toBe(false);
    expect(rowOn('Bob Builder', 'Desktop notifications')).toBe(true);
    expect(rowOn('Carl Casual', 'Screenshot capture')).toBe(true);
    expect(rowOn('Carl Casual', 'Desktop notifications')).toBe(false);
    expect(rowOn('Carl Casual', 'Activity tracking')).toBe(false);
    // The Members page's pill beside the switch.
    expect(row('Bob Builder').textContent).toContain('Off');
    expect(row('Alice Active').textContent).toContain('On');
  });

  it('has no screenshot frequency: not as a column, a pill or a drawer field', async () => {
    await mount();

    const headers = Array.from(container.querySelectorAll('th')).map((th) => th.textContent!.trim());
    expect(headers.some((header) => /Screenshots/.test(header))).toBe(true);
    expect(headers.some((header) => /Notifications/.test(header))).toBe(true);
    expect(headers.some((header) => /Activity/.test(header))).toBe(true);
    expect(headers.some((header) => /frequency/i.test(header))).toBe(false);
    expect(headers.some((header) => /idle/i.test(header))).toBe(false);
    expect(row('Alice Active').textContent).not.toContain('10 min');

    await click(configure('Alice Active'));
    const drawer = container.textContent!;
    expect(drawer).not.toMatch(/Capture Frequency/i);
    expect(drawer).not.toMatch(/Idle Time (Monitoring|Threshold)/);
    expect(container.querySelector('input[type="number"]')).toBeNull();
  });

  it.each(SWITCHES)('a row\'s $label switch saves at once and sends only $field', async ({ label, field }) => {
    await mount();
    await click(rowSwitch('Alice Active', label));

    expect(writes()).toHaveLength(1);
    expect(writes()[0].method).toBe('PATCH');
    expect(writes()[0].path).toMatch(/\/members\/1$/);
    expect(writes()[0].body).toEqual({ [field]: false });
    expect(rowOn('Alice Active', label)).toBe(false);
  });

  it('puts a row\'s switch back, and says why, when the server refuses it', async () => {
    await mount();
    refuseNext = { status: 422, detail: 'Notifications could not be changed.' };
    await click(rowSwitch('Alice Active', 'Desktop notifications'));

    expect(showToast).toHaveBeenCalledWith('Notifications could not be changed.', 'error');
    expect(rowOn('Alice Active', 'Desktop notifications')).toBe(true);
  });

  it('a heading switch reads on only when every listed member is on', async () => {
    await mount();
    expect(headerSwitch('Activity tracking')!.getAttribute('aria-checked')).toBe('false');
    expect(headerSwitch('Screenshot capture')!.getAttribute('aria-checked')).toBe('false');
    await click(rowSwitch('Carl Casual', 'Activity tracking'));
    await click(rowSwitch('Carl Casual', 'Desktop notifications'));
    expect(headerSwitch('Desktop notifications')!.getAttribute('aria-checked')).toBe('true');
  });

  it('a heading switch turns the switch off for every active member on every page, asking first', async () => {
    members.push(member(4, 'Dina Dev'), member(5, 'Eli Eng'));
    members.forEach((m) => { m.notifications_enabled = true; });
    await mount();
    expect(headerSwitch('Desktop notifications')!.getAttribute('aria-checked')).toBe('true');
    await click(headerSwitch('Desktop notifications'));
    await until(() => writes().length >= 5);

    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm.mock.calls[0][0]).toBe('Turn desktop notifications off for every member?');
    // The walk is served two to a page, so members 3-5 are only reached by going past page one.
    expect(writes().map((w) => w.path.replace(/.*\/members\//, '')).sort()).toEqual(['1', '2', '3', '4', '5']);
    writes().forEach((w) => expect(w.body).toEqual({ notifications_enabled: false }));
    expect(showToast).toHaveBeenCalledWith('Desktop notifications turned off for 5 members.', 'success');
    expect(headerSwitch('Desktop notifications')!.getAttribute('aria-checked')).toBe('false');
  });

  it('a heading switch turned on changes only the members who are off', async () => {
    await mount();
    await click(headerSwitch('Desktop notifications'));
    await until(() => writes().length >= 1);

    expect(confirm.mock.calls[0][0]).toBe('Turn desktop notifications on for every member?');
    expect(writes().map((w) => w.path.replace(/.*\/members\//, ''))).toEqual(['3']);
    expect(writes()[0].body).toEqual({ notifications_enabled: true });
  });

  it('changes nothing when the administrator declines the heading switch', async () => {
    await mount();
    confirm.mockResolvedValueOnce(false);
    await click(headerSwitch('Activity tracking'));

    expect(writes()).toHaveLength(0);
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
    await click(toggle('Activity tracking')?.el);
    expect(saveButton().disabled).toBe(false);
    await click(toggle('Activity tracking')?.el);
    expect(saveButton().disabled).toBe(true);
  });

  it.each(SWITCHES)('turning off $label sends $field and neither of the other two switches', async ({ label, field }) => {
    await mount();
    await click(configure('Alice Active'));
    await click(toggle(label)?.el);
    expect(toggle(label)!.checked).toBe(false);
    SWITCHES.filter((other) => other.label !== label).forEach((other) => expect(toggle(other.label)!.checked).toBe(true));
    await click(saveButton());

    expect(writes()).toHaveLength(1);
    expect(writes()[0].method).toBe('PATCH');
    expect(writes()[0].path).toMatch(/\/members\/1$/);
    // What the page already sent, plus only the switch that was changed.
    expect(writes()[0].body).toEqual({ [field]: false });
    SWITCHES.filter((other) => other.field !== field).forEach((other) =>
      expect(Object.keys(writes()[0].body!)).not.toContain(other.field));
    expect(showToast).toHaveBeenCalledWith('Tracking settings saved for Alice Active.', 'success');
    // The drawer closes and the list shows the new state.
    expect(toggle(label)).toBeNull();
  });

  it('turning a switch back on sends true for that field only', async () => {
    await mount();
    await click(configure('Bob Builder'));
    await click(toggle('Screenshot capture')?.el);
    await click(saveButton());

    expect(writes()[0].body).toEqual({ screenshots_enabled: true });
    expect(rowOn('Bob Builder', 'Screenshot capture')).toBe(true);
  });

  it('shows the new state in the list after a save, leaving the other members alone', async () => {
    await mount();
    await click(configure('Alice Active'));
    await click(toggle('Desktop notifications')?.el);
    await click(saveButton());

    expect(rowOn('Alice Active', 'Desktop notifications')).toBe(false);
    expect(rowOn('Alice Active', 'Screenshot capture')).toBe(true);
    expect(rowOn('Carl Casual', 'Desktop notifications')).toBe(false);
    expect(rowOn('Bob Builder', 'Desktop notifications')).toBe(true);
  });

  it('keeps the drawer open with the server\'s message, and the row as it was, when the save is refused', async () => {
    await mount();
    await click(configure('Alice Active'));
    await click(toggle('Screenshot capture')?.el);
    refuseNext = { status: 422, detail: 'Screenshots could not be changed.' };
    await click(saveButton());

    expect(showToast).toHaveBeenCalledWith('Screenshots could not be changed.', 'error');
    expect(container.textContent).toContain('Screenshots could not be changed.');
    expect(toggle('Screenshot capture')).toBeTruthy();
    expect(rowOn('Alice Active', 'Screenshot capture')).toBe(true);
    expect(members[0].screenshots_enabled).toBeUndefined();
    // The other switches did not move.
    expect(toggle('Desktop notifications')!.checked).toBe(true);
    expect(toggle('Activity tracking')!.checked).toBe(true);
  });

  const toolbar = () => container.querySelector('[role="toolbar"]') as HTMLElement | null;
  const toolbarButton = (label: string, word: 'on' | 'off') =>
    toolbar()?.querySelector(`button[aria-label="Turn ${label.toLowerCase()} ${word} for the selected members"]`) as HTMLButtonElement | null;
  const tick = (name: string) => click(container.querySelector(`input[aria-label="Select ${name}"]`));

  it('ticking rows opens a toolbar that turns one switch on or off for just those members', async () => {
    await mount();
    expect(toolbar()).toBeNull();
    await tick('Alice Active');
    await tick('Bob Builder');

    expect(toolbar()!.textContent).toContain('2 selected');
    await click(toolbarButton('Activity tracking', 'off'));
    await until(() => writes().length >= 2);

    expect(writes().map((w) => w.path.replace(/.*\/members\//, ''))).toEqual(['1', '2']);
    writes().forEach((w) => expect(w.body).toEqual({ activity_tracking_enabled: false }));
    expect(showToast).toHaveBeenCalledWith('Activity tracking turned off for 2 members.', 'success');
    // The ticks are cleared once everything was saved, and Carl was never touched.
    expect(toolbar()).toBeNull();
    expect(rowOn('Carl Casual', 'Activity tracking')).toBe(false);
  });

  it('the toolbar changes only the selected members who are not already there', async () => {
    await mount();
    await tick('Alice Active');
    await tick('Bob Builder');
    // Both are already on for notifications, so turning them on is disabled.
    expect(toolbarButton('Desktop notifications', 'on')!.disabled).toBe(true);
    // Bob is already off, Alice is on: only Alice changes.
    await click(toolbarButton('Screenshot capture', 'off'));
    await until(() => writes().length >= 1);

    expect(writes().map((w) => w.path.replace(/.*\/members\//, ''))).toEqual(['1']);
  });

  it('keeps the ticks and says why when the toolbar change is refused', async () => {
    await mount();
    await tick('Alice Active');
    refuseNext = { status: 422, detail: 'Screenshots could not be changed.' };
    await click(toolbarButton('Screenshot capture', 'off'));

    expect(showToast).toHaveBeenCalledWith('Screenshots could not be changed.', 'error');
    expect(toolbar()).toBeTruthy();
    expect(rowOn('Alice Active', 'Screenshot capture')).toBe(true);
  });
});
