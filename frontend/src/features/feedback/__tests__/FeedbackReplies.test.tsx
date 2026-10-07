// @vitest-environment jsdom
/**
 * The administrator's reply, in the "Feedback Description" dialog.
 *
 * Pressing Resolved lets an administrator write a note to the employee. It goes
 * out in the status email, and the backend now returns it with the feedback
 * (`replies`), so the View dialog can show what the employee was actually told.
 *
 * The real `FeedbackTable` is rendered against a real RTK Query store with only
 * `fetch` stubbed. What matters:
 *
 *  - a reply is shown in the dialog, with the move it went out with and the date;
 *  - the note is shown as written -- line breaks kept, markup not interpreted;
 *  - a feedback with no reply shows no reply section, and a Resolved one says
 *    that nothing was written rather than going quiet;
 *  - a payload without the field (an older backend, an older cache) still opens;
 *  - one feedback's reply never carries over to the next row's dialog.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { Feedback, FeedbackReply } from '../../../store/api/feedbackApi';

const showToast = vi.fn();
const confirmAction = vi.fn(async () => true);
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction }),
}));

import { FeedbackTable } from '../FeedbackTable';
import { NO_REPLY_NOTE, repliesOf, showsNoReplyNote } from '../feedbackActions';

const reply = (extra: Partial<FeedbackReply> = {}): FeedbackReply => ({
  message: 'Fixed in 1.3.2 -- thanks for flagging it.',
  status: 'resolved',
  created_at: '2026-10-06T08:30:00+00:00',
  ...extra,
});

const row = (id: number, extra: Partial<Feedback> = {}): Feedback => ({
  id, employee_id: 7, employee_name: `Employee ${id}`, category: 'report_a_problem',
  message: `Message number ${id}`, status: 'resolved', created_at: '2026-10-01T05:00:00+00:00',
  updated_at: null, ...extra,
});

const settle = async () => {
  for (let i = 0; i < 6; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }
};

describe('the reply helpers', () => {
  it('reads an absent field as no replies', () => {
    expect(repliesOf({})).toEqual([]);
    expect(repliesOf({ replies: [reply()] })).toHaveLength(1);
  });

  it('remarks on a missing note only for a Resolved feedback', () => {
    expect(showsNoReplyNote({ status: 'resolved' })).toBe(true);
    expect(showsNoReplyNote({ status: 'resolved', replies: [] })).toBe(true);
    expect(showsNoReplyNote({ status: 'resolved', replies: [reply()] })).toBe(false);
    expect(showsNoReplyNote({ status: 'in_progress' })).toBe(false);
    expect(showsNoReplyNote({ status: 'new' })).toBe(false);
  });
});

describe('the reply in the View dialog', () => {
  let container: HTMLDivElement;
  let root: Root;

  const render = async (items: Feedback[], props: Partial<React.ComponentProps<typeof FeedbackTable>> = {}) => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(
        <Provider store={store}>
          <FeedbackTable items={items} showActions canManage {...props} />
        </Provider>,
      );
    });
    await settle();
  };

  const rowView = (index = 0) =>
    Array.from(container.querySelectorAll('tbody button')).filter((b) => b.textContent === 'View')[index] as HTMLButtonElement;
  const open = async (index = 0) => {
    await act(async () => { rowView(index).click(); });
    await settle();
  };
  const modal = () => document.querySelector('h2')?.closest('.rounded-2xl') as HTMLElement | null;
  const replySection = () => modal()?.querySelector('section[aria-label="Reply to the employee"]') ?? null;
  const close = async () => {
    await act(async () => { (document.querySelector('button[aria-label="Close description"]') as HTMLButtonElement).click(); });
  };

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

  it('shows what the administrator wrote, the move it went out with, and the date', async () => {
    await render([row(1, { replies: [reply()] })]);
    await open();

    const section = replySection() as HTMLElement;
    expect(section).not.toBeNull();
    expect(section.textContent).toContain('Reply to Employee 1');
    expect(section.textContent).toContain('Fixed in 1.3.2 -- thanks for flagging it.');
    expect(section.textContent).toContain('Resolved');
    expect(section.textContent).toMatch(/2026/);
    expect(modal()?.textContent).not.toContain(NO_REPLY_NOTE);
  });

  it('shows every note in the order it was sent', async () => {
    await render([row(1, {
      replies: [
        reply({ status: 'in_progress', message: 'Looking into it now.', created_at: '2026-10-05T08:00:00+00:00' }),
        reply({ message: 'All done.' }),
      ],
    })]);
    await open();

    const text = (replySection() as HTMLElement).textContent as string;
    expect(text.indexOf('Looking into it now.')).toBeGreaterThan(-1);
    expect(text.indexOf('Looking into it now.')).toBeLessThan(text.indexOf('All done.'));
    expect(text).toContain('Working');
  });

  it('keeps line breaks and does not interpret markup in the note', async () => {
    await render([row(1, { replies: [reply({ message: 'Line one\nLine two <b>not bold</b>' })] })]);
    await open();

    const paragraph = (replySection() as HTMLElement).querySelector('p') as HTMLElement;
    expect(paragraph.textContent).toBe('Line one\nLine two <b>not bold</b>');
    expect(paragraph.className).toContain('whitespace-pre-wrap');
    expect(paragraph.querySelector('b')).toBeNull();
  });

  it('draws no reply section when nobody replied, and says so for a Resolved feedback', async () => {
    await render([row(1, { replies: [] }), row(2, { status: 'in_progress' })]);

    await open(0);
    expect(replySection()).toBeNull();
    expect(modal()?.textContent).toContain(NO_REPLY_NOTE);
    await close();

    await open(1);
    expect(replySection()).toBeNull();
    expect(modal()?.textContent).not.toContain(NO_REPLY_NOTE);
  });

  it('still opens for a payload that has no `replies` field at all', async () => {
    await render([row(1)]);
    await open();

    expect(modal()).not.toBeNull();
    expect(replySection()).toBeNull();
    expect(modal()?.textContent).toContain('Message number 1');
  });

  it('never carries one feedback\'s reply into the next row\'s dialog', async () => {
    await render([
      row(1, { replies: [reply({ message: 'Only for the first.' })] }),
      row(2, { replies: [reply({ message: 'Only for the second.' })] }),
    ]);

    await open(0);
    expect(modal()?.textContent).toContain('Only for the first.');
    expect(modal()?.textContent).not.toContain('Only for the second.');
    await close();

    await open(1);
    expect(modal()?.textContent).toContain('Only for the second.');
    expect(modal()?.textContent).not.toContain('Only for the first.');
  });
});
