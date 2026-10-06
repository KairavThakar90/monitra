// @vitest-environment jsdom
/**
 * The Resolved button: the dialog it opens, and the optional message that dialog
 * lets an administrator add to the email the employee receives.
 *
 * The real `FeedbackTable` is rendered against a real RTK Query store with only
 * `fetch` stubbed, so what is asserted is the request the app would actually
 * send -- not that a callback was called. The things that matter:
 *
 *  - nothing is sent until the dialog is confirmed (opening it is not a request);
 *  - an empty or whitespace-only note sends exactly the body it always did, so
 *    "no message" stays the standard email;
 *  - a note is sent trimmed and otherwise untouched;
 *  - Cancel and Esc send nothing, and a failure keeps the dialog open with the
 *    note still in it;
 *  - Working is unchanged and still asks the plain confirmation;
 *  - a note typed for one row cannot leak into another.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { Feedback } from '../../../store/api/feedbackApi';

const showToast = vi.fn();
const confirmAction = vi.fn(async () => true);
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction }),
}));

import { FeedbackTable } from '../FeedbackTable';
import { STATUS_MESSAGE_MAX_LENGTH } from '../feedbackActions';

const row = (id: number, extra: Partial<Feedback> = {}): Feedback => ({
  id, employee_id: 7, employee_name: `Employee ${id}`, category: 'report_a_problem',
  message: `Message number ${id}`, status: 'new', created_at: '2026-10-01T05:00:00+00:00',
  updated_at: null, ...extra,
});

const urlOf = (input: RequestInfo | URL) =>
  typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
const settle = async () => {
  for (let i = 0; i < 6; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }
};

type Call = { url: string; method: string; body: unknown };

describe('Resolved opens a dialog with an optional message', () => {
  let container: HTMLDivElement;
  let root: Root;
  let calls: Call[];
  let statusReply: () => Response;

  const render = async (items: Feedback[]) => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(
        <Provider store={store}>
          <FeedbackTable items={items} showActions canManage />
        </Provider>,
      );
    });
    await settle();
  };

  const patches = () => calls.filter((call) => call.method === 'PATCH');
  const named = (label: string, index = 0) =>
    Array.from(container.querySelectorAll('tbody button')).filter(
      (b) => b.textContent?.replace('✓ ', '') === label,
    )[index] as HTMLButtonElement;
  const dialog = () => document.querySelector('[role="dialog"]') as HTMLElement | null;
  const textarea = () => document.getElementById('resolve-feedback-message') as HTMLTextAreaElement;
  const dialogButton = (label: string) =>
    Array.from(dialog()!.querySelectorAll('button')).find((b) => b.textContent === label) as HTMLButtonElement;

  const openResolve = async (index = 0) => {
    await act(async () => { named('Resolved', index).click(); });
    await settle();
  };
  const type = async (value: string) => {
    // React tracks the value itself; the native setter is how a real keystroke gets past that.
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!;
    await act(async () => {
      setter.call(textarea(), value);
      textarea().dispatchEvent(new Event('input', { bubbles: true }));
    });
  };
  const submit = async () => {
    await act(async () => { dialogButton('Mark as Resolved').click(); });
    await settle();
  };
  const press = async (key: string) => {
    await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true })); });
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.setItem('accessToken', 'tok-123');
    showToast.mockClear();
    confirmAction.mockClear();
    calls = [];
    statusReply = () => new Response(
      JSON.stringify({ ...row(1), status: 'resolved', notification_queued: true }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    );
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      const method = input instanceof Request ? input.method : (init?.method ?? 'GET');
      let body: unknown = null;
      if (input instanceof Request) {
        const text = await input.clone().text();
        body = text ? JSON.parse(text) : null;
      } else if (typeof init?.body === 'string') {
        body = JSON.parse(init.body);
      }
      calls.push({ url, method, body });
      if (url.includes('/status')) return statusReply();
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
    localStorage.removeItem('accessToken');
  });

  describe('opening it', () => {
    it('shows the dialog, names the employee, and sends nothing yet', async () => {
      await render([row(1)]);
      await openResolve();

      expect(dialog()).not.toBeNull();
      expect(dialog()!.textContent).toContain('Mark this feedback as resolved?');
      expect(dialog()!.textContent).toContain('Employee 1');
      expect(dialog()!.textContent).toContain('(optional)');
      expect(textarea()).not.toBeNull();
      expect(patches()).toHaveLength(0);
    });

    it('replaces the plain confirmation for Resolved rather than adding to it', async () => {
      await render([row(1)]);
      await openResolve();
      expect(confirmAction).not.toHaveBeenCalled();
    });

    it('puts the cursor in the message box', async () => {
      await render([row(1)]);
      await openResolve();
      expect(document.activeElement).toBe(textarea());
    });

    it('is not offered on a row that is already resolved', async () => {
      await render([row(1, { status: 'resolved' })]);
      expect(named('Resolved').disabled).toBe(true);
      await act(async () => { named('Resolved').click(); });
      expect(dialog()).toBeNull();
    });
  });

  describe('confirming', () => {
    it('with no message sends exactly the body it always did', async () => {
      await render([row(1)]);
      await openResolve();
      await submit();

      expect(patches()).toHaveLength(1);
      expect(patches()[0].url).toMatch(/\/feedback\/1\/status$/);
      expect(patches()[0].body).toEqual({ status: 'resolved' });
      expect(Object.keys(patches()[0].body as object)).toEqual(['status']);
    });

    it('treats a message of only spaces and line breaks as no message', async () => {
      await render([row(1)]);
      await openResolve();
      await type('   \n\n  \t ');
      await submit();

      expect(patches()).toHaveLength(1);
      expect(patches()[0].body).toEqual({ status: 'resolved' });
    });

    it('sends the message, trimmed and otherwise exactly as written', async () => {
      await render([row(1)]);
      await openResolve();
      await type('  Fixed in the next release.\nThanks for flagging it — Ada & co. (5 < 10 users hit it.)  ');
      await submit();

      expect(patches()).toHaveLength(1);
      expect(patches()[0].body).toEqual({
        status: 'resolved',
        message: 'Fixed in the next release.\nThanks for flagging it — Ada & co. (5 < 10 users hit it.)',
      });
    });

    it('sends one request however many times the button is pressed', async () => {
      let release: () => void = () => {};
      const gate = new Promise<void>((resolve) => { release = resolve; });
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>;
      const base = fetchMock.getMockImplementation() as (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
      fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (urlOf(input).includes('/status')) await gate;
        return base(input, init);
      });

      await render([row(1)]);
      await openResolve();
      await type('Done.');
      await act(async () => { dialogButton('Mark as Resolved').click(); });
      // In flight: the button says so and is locked, and a second press is inert.
      expect(dialogButton('Sending…').disabled).toBe(true);
      expect(textarea().disabled).toBe(true);
      await act(async () => {
        dialog()!.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      });
      await act(async () => { release(); });
      await settle();

      expect(patches()).toHaveLength(1);
    });

    it('closes the dialog and says the message went with the email', async () => {
      await render([row(1)]);
      await openResolve();
      await type('Fixed.');
      await submit();

      expect(dialog()).toBeNull();
      expect(showToast).toHaveBeenCalledTimes(1);
      const [text, kind] = showToast.mock.calls[0];
      expect(kind).toBe('success');
      expect(text).toContain('Employee 1 has been notified by email');
      expect(text).toContain('with your message');
    });

    it('does not mention a message when none was written', async () => {
      await render([row(1)]);
      await openResolve();
      await submit();

      expect(dialog()).toBeNull();
      expect(showToast.mock.calls[0][0]).toContain('has been notified by email');
      expect(showToast.mock.calls[0][0]).not.toContain('with your message');
    });

    it('does not claim the message was sent when the server queued no email', async () => {
      statusReply = () => new Response(
        JSON.stringify({ ...row(1), status: 'resolved', notification_queued: false }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      );
      await render([row(1)]);
      await openResolve();
      await type('Fixed.');
      await submit();

      expect(showToast.mock.calls[0][0]).toContain('No new email was sent');
      expect(showToast.mock.calls[0][0]).not.toContain('with your message');
    });
  });

  describe('leaving without resolving', () => {
    it('Cancel sends nothing and closes the dialog', async () => {
      await render([row(1)]);
      await openResolve();
      await type('Half-written thought');
      await act(async () => { dialogButton('Cancel').click(); });

      expect(dialog()).toBeNull();
      expect(patches()).toHaveLength(0);
      expect(showToast).not.toHaveBeenCalled();
      expect(named('Resolved').disabled).toBe(false);
    });

    it('Esc sends nothing and closes the dialog', async () => {
      await render([row(1)]);
      await openResolve();
      await press('Escape');

      expect(dialog()).toBeNull();
      expect(patches()).toHaveLength(0);
    });

    it('forgets what was typed, so a reopened dialog starts empty', async () => {
      await render([row(1)]);
      await openResolve();
      await type('Left over?');
      await press('Escape');
      await openResolve();

      expect(textarea().value).toBe('');
    });

    it('does not let Esc interrupt a request already in flight', async () => {
      let release: () => void = () => {};
      const gate = new Promise<void>((resolve) => { release = resolve; });
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>;
      const base = fetchMock.getMockImplementation() as (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
      fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (urlOf(input).includes('/status')) await gate;
        return base(input, init);
      });

      await render([row(1)]);
      await openResolve();
      await act(async () => { dialogButton('Mark as Resolved').click(); });
      await press('Escape');
      expect(dialog()).not.toBeNull();

      await act(async () => { release(); });
      await settle();
      expect(dialog()).toBeNull();
    });
  });

  describe('validation', () => {
    it('counts what has been typed against the backend’s limit', async () => {
      await render([row(1)]);
      await openResolve();
      expect(document.querySelector('[data-testid="resolve-feedback-count"]')!.textContent)
        .toBe(`0/${STATUS_MESSAGE_MAX_LENGTH}`);
      await type('hello');
      expect(document.querySelector('[data-testid="resolve-feedback-count"]')!.textContent)
        .toBe(`5/${STATUS_MESSAGE_MAX_LENGTH}`);
    });

    it('accepts a message of exactly the limit', async () => {
      await render([row(1)]);
      await openResolve();
      const exact = 'a'.repeat(STATUS_MESSAGE_MAX_LENGTH);
      await type(exact);
      await submit();

      expect(patches()).toHaveLength(1);
      expect((patches()[0].body as { message: string }).message).toBe(exact);
    });

    it('refuses a message over the limit, says why, and sends nothing', async () => {
      await render([row(1)]);
      await openResolve();
      await type('a'.repeat(STATUS_MESSAGE_MAX_LENGTH + 1));
      await submit();

      expect(patches()).toHaveLength(0);
      expect(dialog()).not.toBeNull();
      expect(dialog()!.querySelector('[role="alert"], [id$="-error"]')).not.toBeNull();
      expect(textarea().getAttribute('aria-invalid')).toBe('true');
      // What was typed is still there to be shortened.
      expect(textarea().value).toHaveLength(STATUS_MESSAGE_MAX_LENGTH + 1);
    });

    it('refuses markup in the message, as every prose field does, and sends nothing', async () => {
      await render([row(1)]);
      await openResolve();
      await type('Please <b>read</b> this');
      await submit();

      expect(patches()).toHaveLength(0);
      expect(dialog()).not.toBeNull();
      expect(textarea().getAttribute('aria-invalid')).toBe('true');
      expect(textarea().value).toBe('Please <b>read</b> this');
    });

    it('lets the same dialog go ahead once the message is shortened', async () => {
      await render([row(1)]);
      await openResolve();
      await type('a'.repeat(STATUS_MESSAGE_MAX_LENGTH + 1));
      await submit();
      expect(patches()).toHaveLength(0);

      await type('a'.repeat(STATUS_MESSAGE_MAX_LENGTH));
      await submit();
      expect(patches()).toHaveLength(1);
    });
  });

  describe('when the request fails', () => {
    it('keeps the dialog open with the message still in it, and says what went wrong', async () => {
      statusReply = () => new Response(JSON.stringify({ detail: 'boom' }), {
        status: 500, headers: { 'content-type': 'application/json' },
      });
      vi.spyOn(console, 'error').mockImplementation(() => {});
      await render([row(1)]);
      await openResolve();
      await type('Worth keeping.');
      await submit();

      expect(dialog()).not.toBeNull();
      expect(textarea().value).toBe('Worth keeping.');
      expect(showToast).toHaveBeenCalledTimes(1);
      expect(showToast.mock.calls[0][1]).toBe('error');
      // Nothing is stuck: the button is back, ready for another try.
      expect(dialogButton('Mark as Resolved').disabled).toBe(false);
      expect(textarea().disabled).toBe(false);
    });

    it('can be retried after a failure, sending the same message', async () => {
      let attempt = 0;
      statusReply = () => {
        attempt += 1;
        return attempt === 1
          ? new Response('{}', { status: 502, headers: { 'content-type': 'application/json' } })
          : new Response(JSON.stringify({ ...row(1), status: 'resolved', notification_queued: true }), {
            status: 200, headers: { 'content-type': 'application/json' },
          });
      };
      vi.spyOn(console, 'error').mockImplementation(() => {});
      await render([row(1)]);
      await openResolve();
      await type('Try me twice.');
      await submit();
      await submit();

      expect(patches()).toHaveLength(2);
      expect(patches()[0].body).toEqual({ status: 'resolved', message: 'Try me twice.' });
      expect(patches()[1].body).toEqual({ status: 'resolved', message: 'Try me twice.' });
      expect(dialog()).toBeNull();
    });
  });

  describe('Working is unchanged', () => {
    it('still asks the plain confirmation, opens no dialog, and sends no message', async () => {
      await render([row(1)]);
      await act(async () => { named('Working').click(); });
      await settle();

      expect(confirmAction).toHaveBeenCalledTimes(1);
      expect(dialog()).toBeNull();
      expect(patches()).toHaveLength(1);
      expect(patches()[0].body).toEqual({ status: 'in_progress' });
    });

    it('sends nothing when that confirmation is declined', async () => {
      confirmAction.mockResolvedValueOnce(false);
      await render([row(1)]);
      await act(async () => { named('Working').click(); });
      await settle();

      expect(patches()).toHaveLength(0);
    });

    it('still lets an in-progress row be resolved, with a message', async () => {
      await render([row(1, { status: 'in_progress' })]);
      await openResolve();
      await type('Closing the loop.');
      await submit();

      expect(patches()[0].body).toEqual({ status: 'resolved', message: 'Closing the loop.' });
    });
  });

  describe('rows are independent', () => {
    it('opens the dialog for the row that was pressed, and names that row’s employee', async () => {
      await render([row(1), row(2)]);
      await openResolve(1);

      expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1);
      expect(dialog()!.textContent).toContain('Employee 2');
      expect(dialog()!.textContent).not.toContain('Employee 1');
    });

    it('sends the message to the row that was pressed', async () => {
      await render([row(1), row(2)]);
      await openResolve(1);
      await type('For two only.');
      await submit();

      expect(patches()).toHaveLength(1);
      expect(patches()[0].url).toMatch(/\/feedback\/2\/status$/);
    });

    it('does not carry a message from one row into the next', async () => {
      await render([row(1), row(2)]);
      await openResolve(0);
      await type('Only for the first.');
      await press('Escape');
      await openResolve(1);

      expect(textarea().value).toBe('');
    });
  });
});
