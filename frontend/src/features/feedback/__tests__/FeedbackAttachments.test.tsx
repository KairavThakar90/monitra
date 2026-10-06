// @vitest-environment jsdom
/**
 * Attachments on feedback: the chip in the list, and the section, thumbnails,
 * preview and download in the "Feedback Description" dialog.
 *
 * The real `FeedbackTable` is rendered against a real RTK Query store with only
 * `fetch` stubbed, so the status buttons are exercised through the same
 * mutation they use in the app. Object URLs are mocked locally -- jsdom has
 * none -- and restored after each test.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { Feedback, FeedbackAttachment } from '../../../store/api/feedbackApi';

const showToast = vi.fn();
const confirmAction = vi.fn(async () => true);
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction }),
}));

import { FeedbackTable } from '../FeedbackTable';
import {
  attachmentCountLabel,
  attachmentCountOf,
  formatFileSize,
} from '../attachmentFiles';

const PNG: FeedbackAttachment = {
  id: 41, original_filename: 'problem.png', content_type: 'image/png', file_size: 1_258_291,
  created_at: '2026-10-01T05:00:00+00:00', is_image: true,
};
const SECOND: FeedbackAttachment = {
  id: 42, original_filename: 'a-very-long-screenshot-name-that-must-not-push-the-modal-wider-than-the-viewport.jpg',
  content_type: 'image/jpeg', file_size: 48_000, created_at: '2026-10-01T05:00:00+00:00', is_image: true,
};
const PDF: FeedbackAttachment = {
  id: 43, original_filename: 'notes.pdf', content_type: 'application/pdf', file_size: 2048,
  created_at: '2026-10-01T05:00:00+00:00', is_image: false,
};

const row = (id: number, extra: Partial<Feedback> = {}): Feedback => ({
  id, employee_id: 7, employee_name: 'Alice Example', category: 'report_a_problem',
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

describe('attachment helpers', () => {
  it('words the count in the singular and the plural', () => {
    expect(attachmentCountLabel(1)).toBe('1 attachment');
    expect(attachmentCountLabel(2)).toBe('2 attachments');
  });

  it('treats absent fields as none, and prefers the count when both are given', () => {
    expect(attachmentCountOf({})).toBe(0);
    expect(attachmentCountOf({ attachments: [PNG, PDF] })).toBe(2);
    expect(attachmentCountOf({ attachment_count: 3, attachments: [PNG] })).toBe(3);
  });

  it('writes sizes a person can read', () => {
    expect(formatFileSize(0)).toBe('0 B');
    expect(formatFileSize(512)).toBe('512 B');
    expect(formatFileSize(2048)).toBe('2 KB');
    expect(formatFileSize(48_000)).toBe('47 KB');
    expect(formatFileSize(1_258_291)).toBe('1.2 MB');
    expect(formatFileSize(-1)).toBe('');
  });
});

describe('FeedbackTable attachments', () => {
  let container: HTMLDivElement;
  let root: Root;
  let calls: { url: string; method: string; headers: Record<string, string> }[];
  let contentReply: (url: string) => Response;
  let created: string[];
  let revoked: string[];
  let clicked: { href: string; download: string }[];
  const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  let clickSpy: ReturnType<typeof vi.spyOn>;

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

  const contentCalls = () => calls.filter((call) => call.url.includes('/feedback/attachments/'));
  const buttonByLabel = (label: string) =>
    Array.from(document.querySelectorAll('button')).filter((b) => b.getAttribute('aria-label') === label) as HTMLButtonElement[];
  const rowView = (index = 0) =>
    Array.from(container.querySelectorAll('tbody button')).filter((b) => b.textContent === 'View')[index] as HTMLButtonElement;
  const open = async (index = 0) => {
    await act(async () => { rowView(index).click(); });
    await settle();
  };
  const modal = () => document.querySelector('h2')?.closest('.rounded-2xl') as HTMLElement | null;
  const press = async (key: string) => {
    await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true })); });
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.setItem('accessToken', 'tok-123');
    showToast.mockClear();
    confirmAction.mockClear();
    calls = [];
    created = [];
    revoked = [];
    clicked = [];
    contentReply = () => new Response(new Blob(['img'], { type: 'image/png' }), { status: 200 });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      const headers: Record<string, string> = {};
      new Headers(input instanceof Request ? input.headers : init?.headers).forEach((value, key) => { headers[key] = value; });
      calls.push({ url, method: input instanceof Request ? input.method : (init?.method ?? 'GET'), headers });
      if (url.includes('/feedback/attachments/')) return contentReply(url);
      if (url.includes('/status')) {
        return new Response(JSON.stringify({ ...row(1), status: 'in_progress', notification_queued: true }), {
          status: 200, headers: { 'content-type': 'application/json' },
        });
      }
      return new Response('', { status: 404 });
    }));
    let n = 0;
    URL.createObjectURL = vi.fn(() => { n += 1; const u = `blob:mock-${n}`; created.push(u); return u; }) as never;
    URL.revokeObjectURL = vi.fn((u: string) => { revoked.push(u); }) as never;
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ href: this.href, download: this.download });
    });
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    clickSpy.mockRestore();
    URL.createObjectURL = original.create;
    URL.revokeObjectURL = original.revoke;
    localStorage.removeItem('accessToken');
  });

  describe('the chip in the list', () => {
    it('says "1 attachment" and "2 attachments", in both the table and the stacked card', async () => {
      await render([
        row(1, { attachment_count: 1, attachments: [PNG] }),
        row(2, { attachment_count: 2, attachments: [PNG, PDF] }),
      ]);
      const tableChips = container.querySelectorAll('table [aria-label$="attachment"], table [aria-label$="attachments"]');
      expect(Array.from(tableChips).map((el) => el.getAttribute('aria-label'))).toEqual(['1 attachment', '2 attachments']);
      const cards = container.querySelector('.md\\:hidden') as HTMLElement;
      const cardChips = cards.querySelectorAll('[aria-label$="attachment"], [aria-label$="attachments"]');
      expect(Array.from(cardChips).map((el) => el.textContent)).toEqual(['1 attachment', '2 attachments']);
      expect(cardChips[0].getAttribute('aria-label')).toBe('1 attachment');
    });

    it('draws nothing for a row with none, with zero, or with the fields absent', async () => {
      await render([
        row(1),
        row(2, { attachment_count: 0, attachments: [] }),
      ]);
      expect(container.querySelector('[aria-label$="attachment"], [aria-label$="attachments"]')).toBeNull();
      expect(container.textContent).not.toContain('attachment');
    });

    it('is shown on the member page (no actions column) in the table and the card', async () => {
      await render([row(1, { attachment_count: 2, attachments: [PNG, PDF] })], { showActions: false, canManage: false, showEmployee: false });
      expect(container.querySelectorAll('[aria-label="2 attachments"]')).toHaveLength(2);
    });

    it('fetches no attachment bytes for rows, only metadata is used', async () => {
      await render([row(1, { attachment_count: 1, attachments: [PNG] }), row(2, { attachment_count: 1, attachments: [SECOND] })]);
      expect(contentCalls()).toHaveLength(0);
      expect(created).toHaveLength(0);
    });
  });

  describe('the dialog', () => {
    it('shows the Attachments section with filename and size, and fetches the image with the bearer token', async () => {
      await render([row(1, { attachment_count: 1, attachments: [PNG] })]);
      await open();

      expect(modal()!.textContent).toContain('Attachments');
      expect(modal()!.textContent).toContain('problem.png');
      expect(modal()!.textContent).toContain('1.2 MB');

      expect(contentCalls()).toHaveLength(1);
      expect(contentCalls()[0].url).toMatch(/\/feedback\/attachments\/41\/content$/);
      expect(contentCalls()[0].headers.authorization).toBe('Bearer tok-123');

      const img = modal()!.querySelector('img') as HTMLImageElement;
      expect(img.getAttribute('src')).toBe('blob:mock-1');
      expect(img.getAttribute('alt')).toBe('problem.png');
      expect(img.className).toContain('object-contain');
    });

    it('fetches once per attachment, not once per render', async () => {
      await render([row(1, { attachment_count: 2, attachments: [PNG, SECOND] })]);
      await open();
      await settle();
      expect(contentCalls().map((c) => c.url.match(/attachments\/(\d+)/)![1]).sort()).toEqual(['41', '42']);
    });

    it('truncates a long filename, keeping the full name in its title', async () => {
      await render([row(1, { attachment_count: 1, attachments: [SECOND] })]);
      await open();
      const name = Array.from(modal()!.querySelectorAll('[title]')).find((el) => el.getAttribute('title') === SECOND.original_filename)!;
      expect(name.className).toContain('truncate');
    });

    it('renders no Attachments section when there are none, and fetches nothing', async () => {
      await render([row(1)]);
      await open();
      expect(modal()).not.toBeNull();
      expect(modal()!.textContent).not.toContain('Attachments');
      expect(contentCalls()).toHaveLength(0);
    });

    it('revokes the thumbnail URL when the dialog closes', async () => {
      await render([row(1, { attachment_count: 1, attachments: [PNG] })]);
      await open();
      expect(revoked).toEqual([]);
      await act(async () => { (document.querySelector('button[aria-label="Close description"]') as HTMLButtonElement).click(); });
      expect(revoked).toEqual(['blob:mock-1']);
    });

    it('shows a file card with only Download for a non-image, and never fetches it for display', async () => {
      await render([row(1, { attachment_count: 1, attachments: [PDF] })]);
      await open();
      expect(modal()!.textContent).toContain('notes.pdf');
      expect(modal()!.textContent).toContain('2 KB');
      expect(modal()!.querySelector('img')).toBeNull();
      expect(buttonByLabel('View attachment notes.pdf')).toHaveLength(0);
      expect(buttonByLabel('Download attachment notes.pdf')).toHaveLength(1);
      expect(contentCalls()).toHaveLength(0);
    });
  });

  describe('the preview', () => {
    it('opens from View, closes on Escape without closing the feedback dialog', async () => {
      await render([row(1, { attachment_count: 1, attachments: [PNG] })]);
      await open();
      const view = buttonByLabel('View attachment problem.png');
      expect(view.length).toBeGreaterThan(0);
      const viewButton = view.find((b) => b.textContent === 'View')!;
      // A real click focuses the button first; jsdom's click() does not.
      viewButton.focus();
      await act(async () => { viewButton.click(); });

      const lightbox = document.querySelector('[role="dialog"][aria-modal="true"]') as HTMLElement;
      expect(lightbox).not.toBeNull();
      expect(lightbox.getAttribute('aria-label')).toBe('Attachment problem.png');
      expect(lightbox.querySelector('img')!.getAttribute('src')).toBe('blob:mock-1');
      // The preview reuses the thumbnail's bytes: still one request.
      expect(contentCalls()).toHaveLength(1);
      expect(document.activeElement).toBe(lightbox.querySelector('button[aria-label="Close preview"]'));

      await press('Escape');
      expect(document.querySelector('[role="dialog"]')).toBeNull();
      expect(modal()).not.toBeNull();
      expect(modal()!.textContent).toContain('Feedback Description');
      // Focus goes back to the control that opened it.
      expect(document.activeElement?.textContent).toBe('View');
    });

    it('opens from the thumbnail and closes from the backdrop, leaving the feedback dialog open', async () => {
      await render([row(1, { attachment_count: 1, attachments: [PNG] })]);
      await open();
      await act(async () => { (modal()!.querySelector('img')!.closest('button') as HTMLButtonElement).click(); });
      const lightbox = document.querySelector('[role="dialog"][aria-modal="true"]') as HTMLElement;
      expect(lightbox).not.toBeNull();

      // A click inside the preview does not close it.
      await act(async () => { lightbox.querySelector('img')!.click(); });
      expect(document.querySelector('[role="dialog"]')).not.toBeNull();

      await act(async () => { (lightbox.parentElement as HTMLElement).click(); });
      expect(document.querySelector('[role="dialog"]')).toBeNull();
      expect(modal()).not.toBeNull();
    });

    it('sits above the feedback dialog', async () => {
      await render([row(1, { attachment_count: 1, attachments: [PNG] })]);
      await open();
      await act(async () => { buttonByLabel('View attachment problem.png')[0].click(); });
      const overlay = (document.querySelector('[role="dialog"][aria-modal="true"]') as HTMLElement).parentElement!;
      expect(overlay.className).toContain('z-[70]');
      expect(modal()!.closest('.z-50')).not.toBeNull();
    });
  });

  describe('download', () => {
    it('requests ?download=true with the token, saves through an <a download>, and revokes the URL', async () => {
      await render([row(1, { attachment_count: 1, attachments: [PNG] })]);
      await open();
      const before = contentCalls().length;
      await act(async () => { buttonByLabel('Download attachment problem.png')[0].click(); });
      await settle();

      const downloadCalls = contentCalls().slice(before);
      expect(downloadCalls).toHaveLength(1);
      expect(downloadCalls[0].url).toMatch(/\/feedback\/attachments\/41\/content\?download=true$/);
      expect(downloadCalls[0].headers.authorization).toBe('Bearer tok-123');

      // mock-1 is the thumbnail; mock-2 is the download.
      expect(created).toEqual(['blob:mock-1', 'blob:mock-2']);
      expect(clicked).toEqual([{ href: 'blob:mock-2', download: 'problem.png' }]);
      expect(revoked).toEqual(['blob:mock-2']);
      expect(showToast).not.toHaveBeenCalled();
      // No <a> is left behind, and the page did not move.
      expect(document.querySelector('a[download]')).toBeNull();
      expect(modal()).not.toBeNull();
    });

    it('toasts and stays put when the download fails', async () => {
      await render([row(1, { attachment_count: 1, attachments: [PNG] })]);
      await open();
      contentReply = (url) => url.includes('download=true')
        ? new Response('', { status: 502 })
        : new Response(new Blob(['img']), { status: 200 });
      vi.spyOn(console, 'error').mockImplementation(() => {});
      await act(async () => { buttonByLabel('Download attachment problem.png')[0].click(); });
      await settle();

      expect(showToast).toHaveBeenCalledWith("Couldn't download the attachment. Please try again.", 'error');
      expect(clicked).toHaveLength(0);
      expect(modal()!.textContent).toContain('problem.png');
      // And it can be tried again.
      expect(buttonByLabel('Download attachment problem.png')[0].disabled).toBe(false);
    });

    it('downloads from the preview too', async () => {
      await render([row(1, { attachment_count: 1, attachments: [PNG] })]);
      await open();
      await act(async () => { buttonByLabel('View attachment problem.png')[0].click(); });
      const lightbox = document.querySelector('[role="dialog"][aria-modal="true"]') as HTMLElement;
      await act(async () => { (lightbox.querySelector('button[aria-label="Download attachment problem.png"]') as HTMLButtonElement).click(); });
      await settle();
      expect(clicked).toHaveLength(1);
      expect(clicked[0].download).toBe('problem.png');
    });
  });

  describe('an attachment that cannot be read', () => {
    it('shows "Attachment unavailable" for a 404 and keeps the rest of the feedback usable', async () => {
      contentReply = () => new Response('', { status: 404 });
      await render([row(1, { attachment_count: 1, attachments: [PNG] })]);
      await open();

      expect(modal()!.textContent).toContain('Attachment unavailable');
      expect(modal()!.querySelector('img')).toBeNull();
      expect(modal()!.textContent).toContain('Message number 1');
      expect(modal()!.textContent).toContain('problem.png');
      expect(buttonByLabel('View attachment problem.png')[0].disabled).toBe(true);
      // Download still tries, and says so when it cannot.
      const download = buttonByLabel('Download attachment problem.png')[0];
      expect(download.disabled).toBe(false);
      vi.spyOn(console, 'error').mockImplementation(() => {});
      contentReply = () => new Response('', { status: 404 });
      await act(async () => { download.click(); });
      await settle();
      expect(showToast).toHaveBeenCalledWith("Couldn't download the attachment. Please try again.", 'error');
      expect(created).toEqual([]);
    });

    it('survives a network error', async () => {
      (fetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (input: RequestInfo | URL) => {
        if (urlOf(input).includes('/feedback/attachments/')) throw new TypeError('Failed to fetch');
        return new Response('', { status: 404 });
      });
      await render([row(1, { attachment_count: 1, attachments: [PNG] })]);
      await open();
      expect(modal()!.textContent).toContain('Attachment unavailable');
    });

    it('does not mix up two attachments when one fails', async () => {
      contentReply = (url) => url.includes('/41/')
        ? new Response('', { status: 404 })
        : new Response(new Blob(['img']), { status: 200 });
      await render([row(1, { attachment_count: 2, attachments: [PNG, SECOND] })]);
      await open();
      expect(modal()!.querySelectorAll('img')).toHaveLength(1);
      expect(modal()!.textContent).toContain('Attachment unavailable');
    });
  });

  describe('the status buttons', () => {
    // A reached state is drawn "✓ Working" / "✓ Resolved".
    const named = (label: string) =>
      Array.from(container.querySelectorAll('tbody button')).find((b) => b.textContent?.replace('✓ ', '') === label) as HTMLButtonElement;
    const working = () => named('Working');
    const resolved = () => named('Resolved');

    const cases: [string, Partial<Feedback>][] = [
      ['without attachments', {}],
      ['with attachments', { attachment_count: 1, attachments: [PNG] }],
    ];
    for (const [label, extra] of cases) {
      it(`still send the status update for a row ${label}`, async () => {
        await render([row(1, extra)]);
        expect(working().disabled).toBe(false);
        expect(resolved().disabled).toBe(false);
        await act(async () => { working().click(); });
        await settle();
        expect(confirmAction).toHaveBeenCalledTimes(1);
        const patch = calls.filter((c) => c.method === 'PATCH');
        expect(patch).toHaveLength(1);
        expect(patch[0].url).toMatch(/\/feedback\/1\/status$/);
      });
    }

    it('leaves a resolved row’s buttons disabled exactly as before, attachments or not', async () => {
      await render([row(1, { status: 'resolved', attachment_count: 1, attachments: [PNG] })]);
      expect(working().disabled).toBe(true);
      expect(resolved().disabled).toBe(true);
    });

    it('offers no controls to a read-only viewer, but still shows the attachments', async () => {
      await render([row(1, { attachment_count: 1, attachments: [PNG] })], { canManage: false });
      expect(container.querySelectorAll('tbody button')).toHaveLength(1); // View only
      await open();
      expect(modal()!.textContent).toContain('problem.png');
    });
  });
});
