// @vitest-environment jsdom
/**
 * The message button on a screenshot tile, and the "coming soon" panel it opens.
 *
 * Messaging is not built yet: the button is the entry point and the panel says
 * so. The button is offered wherever somebody is looking at *other people's*
 * captures -- Admin, HR and Leader, in the Employees view -- and never on their
 * own. The real Screenshots page is rendered against a real store with only
 * `fetch` stubbed.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { UserRead } from '../../../api/auth';
import { HourRow } from '../../screenshots/HourRow';
import { ScreenshotMessageDrawer } from '../../screenshots/ScreenshotMessageDrawer';

const user = (role_name: string, id = 3): UserRead => ({
  id, organization_id: 1, username: role_name, email: `${role_name}@example.invalid`,
  name: `The ${role_name}`, role_name, permissions: { view_employees: true }, is_active: true,
});
let currentUser: UserRead = user('leader');

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock('../../auth/authContext', () => ({ useAuth: () => ({ currentUser }) }));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminScreenshots } from '../AdminScreenshots';

const SHOT = {
  id: 501, captured_at: '2026-09-30T05:04:00+00:00', monitor_number: 1, display_count: 1, width: 1000,
  height: 600, file_size_bytes: 1000, view_url: '/x', task_id: 7, task_name: 'Guidance', project_id: 5,
  project_name: 'Apollo',
};
const WINDOW = {
  window_start: '2026-09-30T05:00:00+00:00', window_end: '2026-09-30T05:10:00+00:00', activity_percentage: 60,
  activity_measured_seconds: 600, tracked_seconds: 600, screenshots: [SHOT], screenshot_count: 1,
};
const memberWithCapture = (user_id: number, user_name: string) => ({
  user_id, user_name, screenshot_count: 1, tracked_seconds: 600,
  days: [{ date: '2026-09-30', windows: [WINDOW], screenshot_count: 1, tracked_seconds: 600 }],
});

describe('ScreenshotMessageDrawer', () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it('says the feature is coming soon, and offers nothing to send', async () => {
    const onClose = vi.fn();
    await act(async () => root.render(<ScreenshotMessageDrawer open onClose={onClose} subjectName="Alice" capturedAt="2026-09-30T05:04:00Z" />));
    const text = container.textContent ?? '';
    expect(text).toContain('Screenshot message');
    expect(text).toContain('Coming soon');
    expect(text).toContain('Alice');
    expect(container.querySelector('textarea, input')).toBeNull();
    expect(container.querySelector('aside')!.className).toContain('right-0');
  });

  it('closes from the X, from Escape and from the backdrop, and renders nothing when closed', async () => {
    const onClose = vi.fn();
    await act(async () => root.render(<ScreenshotMessageDrawer open onClose={onClose} />));
    await act(async () => { (container.querySelector('button[aria-label="Close"]') as HTMLButtonElement).click(); });
    await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
    await act(async () => { (container.querySelector('.backdrop-blur-sm') as HTMLElement).click(); });
    expect(onClose).toHaveBeenCalledTimes(3);

    await act(async () => root.render(<ScreenshotMessageDrawer open={false} onClose={onClose} />));
    expect(container.textContent).toBe('');
  });
});

describe('message button on a tile', () => {
  let container: HTMLDivElement;
  let root: Root;
  const block = { key: 'k', startLabel: '10:00 am', endLabel: '11:00 am', trackedSeconds: 600, screenshotCount: 1, windows: [WINDOW] };
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 404 })));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('is only drawn when the page asks for it, and reports the screenshot it belongs to', async () => {
    const onMessage = vi.fn();
    await act(async () => root.render(<HourRow block={block as never} subjectName="Alice" onOpen={() => {}} />));
    expect(container.querySelector('button[title="Message"]')).toBeNull();

    await act(async () => root.render(<HourRow block={block as never} subjectName="Alice" onOpen={() => {}} onMessage={onMessage} />));
    const button = container.querySelector('button[title="Message"]') as HTMLButtonElement;
    expect(button).not.toBeNull();
    // Hidden until the picture is hovered, but reachable from the keyboard.
    expect(button.className).toContain('opacity-0');
    expect(button.className).toContain('group-hover:opacity-100');
    expect(button.className).toContain('focus-visible:opacity-100');
    await act(async () => { button.click(); });
    expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ id: 501 }));
  });
});

describe('AdminScreenshots message button', () => {
  let container: HTMLDivElement;
  let root: Root;

  const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const render = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => root.render(<Provider store={store}><AdminScreenshots /></Provider>));
    await settle();
  };
  const messageButtons = () => container.querySelectorAll('button[title="Message"]');
  const tab = (name: string) =>
    Array.from(container.querySelectorAll('[role="tab"]')).find((el) => el.textContent?.trim() === name) as HTMLButtonElement;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes('/time-entry-screenshots/day')) {
        const pinned = new URL(url, 'http://localhost').searchParams.get('user_id');
        const everyone = [memberWithCapture(11, 'Alice Example'), memberWithCapture(3, 'The leader')];
        return json({ success: true, window_minutes: 10, members: pinned ? everyone.filter((m) => String(m.user_id) === pinned) : everyone });
      }
      if (url.includes('/members') || url.includes('/projects')) return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
      return new Response('', { status: 404 });
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

  for (const role of ['administrator', 'hr', 'leader']) {
    it(`gives a ${role} the message button on employees' screenshots, opening the coming-soon panel`, async () => {
      currentUser = user(role);
      await render();
      expect(messageButtons()).toHaveLength(1); // only Alice: the caller's own row is the Own view's
      await act(async () => { (messageButtons()[0] as HTMLButtonElement).click(); });
      const dialog = document.querySelector('[role="dialog"][aria-label="Screenshot message"]');
      expect(dialog).not.toBeNull();
      expect(dialog!.textContent).toContain('Coming soon');
      expect(dialog!.textContent).toContain('Alice Example');
    });
  }

  it('offers no message button on the caller’s own screenshots', async () => {
    currentUser = user('leader');
    await render();
    await act(async () => { tab('Own').click(); });
    await settle();
    expect(container.textContent).toContain('The leader');
    expect(messageButtons()).toHaveLength(0);
  });

  it('offers nothing to someone who can only see their own screenshots', async () => {
    currentUser = user('manager');
    await render();
    expect(messageButtons()).toHaveLength(0);
  });
});
