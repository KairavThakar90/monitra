// @vitest-environment jsdom
/**
 * The message button on a screenshot tile, and the panel it opens.
 *
 * The panel shows the screenshot and a box for a notice; "Send notice" emails
 * the screenshot with the notice to the employee it belongs to. The request
 * carries a screenshot id and the words -- never an address, which the server
 * takes from the screenshot's own owner. The button is offered to Admin, HR and
 * Leader on other people's captures, never on their own.
 *
 * The real Screenshots page is rendered against a real RTK Query store with
 * only `fetch` stubbed.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { UserRead } from '../../../api/auth';
import type { ScreenshotView } from '../../../store/api/screenshotsApi';
import { HourRow } from '../../screenshots/HourRow';
import { NOTICE_MAX_LENGTH, ScreenshotMessageDrawer } from '../../screenshots/ScreenshotMessageDrawer';

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

const SHOT: ScreenshotView = {
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

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const urlOf = (input: RequestInfo | URL) =>
  typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
const settle = async () => {
  for (let i = 0; i < 6; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }
};

/** Type into a controlled textarea the way a browser does. */
const type = async (textarea: HTMLTextAreaElement, value: string) => {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!;
  await act(async () => {
    setter.call(textarea, value);
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  });
};

describe('ScreenshotMessageDrawer', () => {
  let container: HTMLDivElement;
  let root: Root;
  let noticeCalls: { url: string; body: unknown }[];
  let noticeReply: { status: number; body: unknown };

  const render = async (props: Partial<React.ComponentProps<typeof ScreenshotMessageDrawer>> = {}) => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(
        <Provider store={store}>
          <ScreenshotMessageDrawer open onClose={vi.fn()} subjectName="Alice Example" shot={SHOT} {...props} />
        </Provider>,
      );
    });
    await settle();
  };
  const textarea = () => container.querySelector('textarea') as HTMLTextAreaElement;
  const send = () =>
    Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.includes('Send notice')) as HTMLButtonElement;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    noticeCalls = [];
    noticeReply = { status: 200, body: { success: true, message: 'Notice sent to Alice Example by email.', screenshot_id: 501, recipient_name: 'Alice Example' } };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      if (url.includes('/notice')) {
        const raw = input instanceof Request ? await input.clone().text() : String(init?.body ?? 'null');
        noticeCalls.push({ url, body: JSON.parse(raw) });
        return json(noticeReply.body, noticeReply.status);
      }
      return new Response(new Blob(['img']), { status: 200 });
    }));
    (URL as unknown as { createObjectURL: () => string }).createObjectURL = () => 'blob:x';
    (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = () => {};
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('shows the screenshot, who it goes to, and a box for the notice', async () => {
    await render();
    const text = container.textContent ?? '';
    expect(text).toContain('Screenshot message');
    expect(text).toContain('To Alice Example');
    expect(text).toContain('Apollo');
    expect(text).toContain('Guidance');
    expect(container.querySelector('aside')!.className).toContain('right-0');
    expect(textarea()).not.toBeNull();
    // The picture is fetched through the authorised view route, by id.
    const fetched = (fetch as unknown as { mock: { calls: [RequestInfo | URL][] } }).mock.calls.map(([input]) => urlOf(input));
    expect(fetched.some((url) => url.endsWith('/time-entry-screenshots/501/view'))).toBe(true);
  });

  it('cannot send an empty notice', async () => {
    await render();
    expect(send().disabled).toBe(true);
    await type(textarea(), '   ');
    expect(send().disabled).toBe(true);
  });

  it('sends the screenshot id and the words, and nothing that names a recipient', async () => {
    await render();
    await type(textarea(), 'Please keep the chat window closed while tracking.');
    expect(send().disabled).toBe(false);
    await act(async () => { send().click(); });
    await settle();

    expect(noticeCalls).toHaveLength(1);
    expect(noticeCalls[0].url).toContain('/time-entry-screenshots/501/notice');
    expect(noticeCalls[0].body).toEqual({ message: 'Please keep the chat window closed while tracking.' });
    expect(container.textContent).toContain('Notice sent to Alice Example by email.');
    // Done replaces the form: nothing to send twice.
    expect(container.querySelector('textarea')).toBeNull();
    expect(Array.from(container.querySelectorAll('button')).some((b) => b.textContent === 'Done')).toBe(true);
  });

  it('refuses text the shared rule refuses, before anything is sent', async () => {
    await render();
    await type(textarea(), '!!!');
    await act(async () => { send().click(); });
    await settle();
    expect(noticeCalls).toHaveLength(0);
    expect(container.querySelector('[role="alert"], [id$="-error"]')).not.toBeNull();
  });

  it('shows the count against the limit and cannot exceed it', async () => {
    await render();
    await type(textarea(), 'hello');
    expect(container.textContent).toContain(`5/${NOTICE_MAX_LENGTH}`);
    expect(textarea().maxLength).toBe(NOTICE_MAX_LENGTH);
  });

  it('shows the server’s reason when the notice cannot be sent, and keeps what was typed', async () => {
    noticeReply = { status: 422, body: { detail: 'Alice Example has no usable email address, so the notice cannot be sent.' } };
    await render();
    await type(textarea(), 'A notice');
    await act(async () => { send().click(); });
    await settle();
    expect(container.textContent).toContain('no usable email address');
    expect(textarea().value).toBe('A notice');
    expect(container.textContent).not.toContain('Notice sent');
  });

  it('starts empty for the next screenshot', async () => {
    await render();
    await type(textarea(), 'first notice');
    await render({ shot: { ...SHOT, id: 777 } });
    expect(textarea().value).toBe('');
  });

  it('closes from the X, from Escape and from the backdrop, and renders nothing when closed', async () => {
    const onClose = vi.fn();
    await render({ onClose });
    await act(async () => { (container.querySelector('button[aria-label="Close"]') as HTMLButtonElement).click(); });
    await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
    await act(async () => { (container.querySelector('.backdrop-blur-sm') as HTMLElement).click(); });
    expect(onClose).toHaveBeenCalledTimes(3);

    await render({ open: false });
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
      const url = urlOf(input);
      if (url.includes('/time-entry-screenshots/day')) {
        const pinned = new URL(url, 'http://localhost').searchParams.get('user_id');
        const everyone = [memberWithCapture(11, 'Alice Example'), memberWithCapture(3, 'The leader')];
        return json({ success: true, window_minutes: 10, members: pinned ? everyone.filter((m) => String(m.user_id) === pinned) : everyone });
      }
      if (url.includes('/members') || url.includes('/projects')) return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
      return new Response('', { status: 404 });
    }));
    (URL as unknown as { createObjectURL: () => string }).createObjectURL = () => 'blob:x';
    (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = () => {};
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
    it(`gives a ${role} the message button on employees' screenshots, opening the panel for that screenshot`, async () => {
      currentUser = user(role);
      await render();
      expect(messageButtons()).toHaveLength(1); // only Alice: the caller's own row is the Own view's
      await act(async () => { (messageButtons()[0] as HTMLButtonElement).click(); });
      await settle();
      const dialog = document.querySelector('[role="dialog"][aria-label="Screenshot message"]');
      expect(dialog).not.toBeNull();
      expect(dialog!.textContent).toContain('To Alice Example');
      expect(dialog!.querySelector('textarea')).not.toBeNull();
      expect(dialog!.textContent).toContain('Apollo');
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
