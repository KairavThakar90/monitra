// @vitest-environment jsdom
/**
 * Screenshot Management -> Size and quality.
 *
 * The real section is rendered against a real RTK Query store with only `fetch`
 * replaced by a small stateful fake of the backend, so what is pinned is the
 * request a browser would make and what the administrator sees afterwards:
 *
 * - the current size and quality come from the server, with its limits as hints;
 * - quality `null` shows Automatic (with the number it starts from), a number
 *   shows Fixed with that number;
 * - an out-of-range or non-integer value blocks Save and says why, in words;
 * - Save sends exactly {image_width, image_height, image_quality}, with `null`
 *   (never an omitted key) for Automatic.
 *
 * The page around it (sections, order, hash, access) is screenshotManagementPage.test.tsx.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import { useGetScreenshotSettingsQuery } from '../../../store/api/screenshotSettingsApi';

const showToast = vi.fn();

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction: async () => true }),
}));

import { SizeAndQuality } from '../AdminScreenshotManagement';

/** What the page does around the section: read the settings, then hand them over. */
const Section = () => {
  const settings = useGetScreenshotSettingsQuery();
  return settings.data ? <SizeAndQuality settings={settings.data} /> : null;
};

type Body = Record<string, unknown>;
type Call = { method: string; path: string; body: Body | null };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const baseSettings = () => ({
  image_width: 1500, image_height: 1500, image_quality: null as number | null,
  default_image_width: 1500, default_image_height: 1500, automatic_image_quality: 79,
  min_image_edge: 250, max_image_edge: 4000, min_image_quality: 10, max_image_quality: 100,
  version: 0, updated_at: null as string | null, updated_by_username: null as string | null,
});

describe('SizeAndQuality', () => {
  let container: HTMLDivElement;
  let root: Root;
  let calls: Call[];
  let settings: ReturnType<typeof baseSettings>;
  let refuseNext: { status: number; detail: string } | null;

  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  // Until the page has what it asked for, however long the machine takes to answer.
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
  const input = (id: string) => container.querySelector(`#${id}`) as HTMLInputElement;
  const button = (text: string) =>
    Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.trim() === text) as HTMLButtonElement | undefined;
  const radio = (label: string) =>
    Array.from(container.querySelectorAll('label')).find((l) => l.textContent?.includes(label))!
      .querySelector('input[type="radio"]') as HTMLInputElement;
  const writes = () => calls.filter((c) => c.method !== 'GET');
  const saveButton = () => button('Save changes')!;

  const mount = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><Section /></Provider>); });
    await until(() => !!container.querySelector('#screenshot-width'));
  };

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    showToast.mockClear();
    calls = [];
    refuseNext = null;
    settings = baseSettings();
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), 'http://localhost');
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      const raw = request ? await request.text() : (init?.body as string | undefined);
      const body = raw ? (JSON.parse(raw) as Body) : null;
      calls.push({ method, path: url.pathname, body });

      if (url.pathname.endsWith('/screenshot-settings')) {
        if (method === 'GET') return json(settings);
        if (refuseNext) { const r = refuseNext; refuseNext = null; return json({ detail: r.detail }, r.status); }
        Object.assign(settings, body, { version: settings.version + 1, updated_by_username: 'grace', updated_at: '2026-10-01T09:00:00Z' });
        return json(settings);
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

  describe('screenshot size and quality', () => {
    it('shows the stored size, the allowed range and the default, all from the server', async () => {
      settings.image_width = 1200;
      settings.image_height = 900;
      settings.min_image_edge = 300;
      settings.max_image_edge = 3000;
      await mount();

      expect(input('screenshot-width').value).toBe('1200');
      expect(input('screenshot-height').value).toBe('900');
      expect(container.textContent).toContain('300–3000 px');
      expect(container.textContent).toContain('Default: 1500 × 1500');
      expect(container.textContent).toContain('apply to screenshots captured from now on');
      expect(container.textContent).toContain('already stored keep the size');
      // Never changed: there is no one to credit.
      expect(container.textContent).not.toContain('Last changed by');
    });

    it('credits the last change once there has been one', async () => {
      settings.version = 3;
      settings.updated_by_username = 'grace';
      settings.updated_at = '2026-10-01T09:00:00Z';
      await mount();
      expect(container.textContent).toContain('Last changed by grace');
    });

    it('selects Automatic when the stored quality is null, saying what it starts from', async () => {
      settings.automatic_image_quality = 82;
      await mount();

      expect(radio('Automatic').checked).toBe(true);
      expect(radio('Fixed quality').checked).toBe(false);
      expect(container.textContent).toContain('Starts at 82% and is lowered on busy screens to keep files small.');
      expect(input('screenshot-quality')).toBeNull();
    });

    it('selects Fixed with the stored number when quality is set', async () => {
      settings.image_quality = 64;
      await mount();

      expect(radio('Fixed quality').checked).toBe(true);
      expect(radio('Automatic').checked).toBe(false);
      expect(input('screenshot-quality').value).toBe('64');
      expect(container.textContent).toContain('10–100%');
      expect(container.textContent).toMatch(/sharper, but the files are larger and\s+use more storage/);
    });

    it('keeps Save disabled until something valid has changed', async () => {
      await mount();
      expect(saveButton().disabled).toBe(true);

      await type(input('screenshot-width'), '1200');
      expect(saveButton().disabled).toBe(false);

      await type(input('screenshot-width'), '1500');
      expect(saveButton().disabled).toBe(true);
    });

    it.each([
      ['below the minimum', '100', 'Width must be at least 250.'],
      ['above the maximum', '5000', 'Width must be at most 4000.'],
      ['not a whole number', '1500.5', 'Width must be a whole number.'],
      ['text', 'abc', 'Width must be a whole number.'],
      ['empty', '', 'Width must be a whole number.'],
    ])('blocks Save for a width %s, in words, with the field marked invalid', async (_name, value, message) => {
      await mount();
      await type(input('screenshot-width'), value);

      expect(saveButton().disabled).toBe(true);
      expect(container.textContent).toContain(message);
      const field = input('screenshot-width');
      expect(field.getAttribute('aria-invalid')).toBe('true');
      expect(field.getAttribute('aria-describedby')).toBe('screenshot-width-error');
      expect(container.querySelector('#screenshot-width-error')!.textContent).toBe(message);

      await act(async () => { container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
      await settle();
      expect(writes()).toEqual([]);
    });

    it('blocks Save for a bad height too, and for a fixed quality outside the range', async () => {
      settings.image_quality = 64;
      await mount();

      await type(input('screenshot-height'), '4001');
      expect(container.textContent).toContain('Height must be at most 4000.');
      expect(saveButton().disabled).toBe(true);
      await type(input('screenshot-height'), '900');
      expect(saveButton().disabled).toBe(false);

      await type(input('screenshot-quality'), '5');
      expect(container.textContent).toContain('Quality must be at least 10.');
      expect(input('screenshot-quality').getAttribute('aria-invalid')).toBe('true');
      expect(saveButton().disabled).toBe(true);
      await type(input('screenshot-quality'), '101');
      expect(container.textContent).toContain('Quality must be at most 100.');
      await type(input('screenshot-quality'), '8.5');
      expect(container.textContent).toContain('Quality must be a whole number.');
      expect(saveButton().disabled).toBe(true);
      expect(writes()).toEqual([]);
    });

    it('sends exactly the three keys, with image_quality null for Automatic', async () => {
      await mount();
      await type(input('screenshot-width'), '1200');
      await type(input('screenshot-height'), '800');
      await click(saveButton());

      expect(writes()).toEqual([
        { method: 'PUT', path: expect.stringMatching(/\/screenshot-settings$/), body: { image_width: 1200, image_height: 800, image_quality: null } },
      ]);
      expect(Object.keys(writes()[0].body!).sort()).toEqual(['image_height', 'image_quality', 'image_width']);
      expect(showToast).toHaveBeenCalledWith('Screenshot settings saved.', 'success');
      // The page now shows what the server answered with.
      expect(container.textContent).toContain('Last changed by grace');
      expect(input('screenshot-width').value).toBe('1200');
      expect(saveButton().disabled).toBe(true);
    });

    it('sends the typed percentage for Fixed quality, and a chip fills the box', async () => {
      await mount();
      await click(radio('Fixed quality'));
      // Nothing to save until a percentage is given -- and no scolding before it is typed.
      expect(saveButton().disabled).toBe(true);
      expect(container.querySelector('#screenshot-quality-error')).toBeNull();

      await click(button('75%'));
      expect(input('screenshot-quality').value).toBe('75');
      expect(button('75%')!.getAttribute('aria-pressed')).toBe('true');
      await click(saveButton());

      expect(writes()[0].body).toEqual({ image_width: 1500, image_height: 1500, image_quality: 75 });
    });

    it('switching a fixed quality back to Automatic sends null', async () => {
      settings.image_quality = 64;
      await mount();
      await click(radio('Automatic'));
      await click(saveButton());

      expect(writes()[0].body).toEqual({ image_width: 1500, image_height: 1500, image_quality: null });
    });

    it('Reset to default puts the default size in the boxes without saving', async () => {
      settings.image_width = 1200;
      settings.image_height = 900;
      await mount();
      await click(button('Reset to default'));

      expect(input('screenshot-width').value).toBe('1500');
      expect(input('screenshot-height').value).toBe('1500');
      expect(writes()).toEqual([]);
      expect(saveButton().disabled).toBe(false);
    });

    it('keeps what was typed and says why when the server refuses the save', async () => {
      await mount();
      await type(input('screenshot-width'), '1200');
      refuseNext = { status: 422, detail: 'Width is not acceptable.' };
      await click(saveButton());

      expect(container.textContent).toContain('Width is not acceptable.');
      expect(showToast).toHaveBeenCalledWith('Width is not acceptable.', 'error');
      expect(input('screenshot-width').value).toBe('1200');
      expect(saveButton().disabled).toBe(false);
    });
  });
});
