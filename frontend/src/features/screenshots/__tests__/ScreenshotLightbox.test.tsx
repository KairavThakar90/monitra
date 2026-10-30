// @vitest-environment jsdom
/**
 * The screenshot viewer's zoom.
 *
 * One component serves the administrator, the member and the client portal, so
 * these are written against it rather than against any one screen:
 *
 * - it opens at "fit" (100%), and "zoom out" can never go below the whole picture;
 * - the buttons, the keyboard (+ - 0), the mouse wheel and double-click all zoom,
 *   within 100%-500%;
 * - the picture can be dragged once zoomed, and only as far as its own edges;
 * - moving to the next screenshot starts that one at fit, never at the previous
 *   one's magnification or position;
 * - everything it did before still works: the arrows, Escape, the caption, and
 *   the delete button appearing only when the caller passes one.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

vi.mock('../../../components/AuthedImage', () => ({
  AuthedImage: ({ url, alt }: { url: string; alt: string }) => <img src={url} alt={alt} data-testid="picture" />,
}));

import { ScreenshotLightbox, MAX_ZOOM, type LightboxItem } from '../ScreenshotLightbox';

const shot = (id: number) => ({
  id, captured_at: '2026-10-27T08:30:00+00:00', monitor_number: 1, display_count: 1,
}) as unknown as LightboxItem['shot'];

const item = (id: number): LightboxItem => ({
  shot: shot(id),
  subjectName: 'Alice Active',
  window: {
    window_start: '2026-10-27T08:20:00+00:00', window_end: '2026-10-27T08:30:00+00:00',
    activity_percentage: 71, activity_measured_seconds: 120, tracked_seconds: 600,
    screenshots: [], screenshot_count: 1, capture_state: 'captured', capture_reason: null, capture_attempts: 0,
  } as unknown as LightboxItem['window'],
});

describe('ScreenshotLightbox zoom', () => {
  let container: HTMLDivElement;
  let root: Root;
  let index: number;
  let onClose: Mock<() => void>;
  let onDelete: ((shot: LightboxItem['shot']) => void) | undefined;

  const render = async () => {
    await act(async () => {
      root.render(
        <ScreenshotLightbox
          items={[item(1), item(2), item(3)]}
          index={index}
          onIndexChange={(next) => { index = next; void render(); }}
          onClose={onClose}
          onDelete={onDelete}
        />,
      );
    });
  };
  const button = (label: string) => container.querySelector(`button[aria-label="${label}"]`) as HTMLButtonElement;
  const percent = () => container.querySelector('[aria-live="polite"]')!.textContent;
  const content = () => container.querySelector('[data-testid="zoom-content"]') as HTMLElement;
  const viewport = () => container.querySelector('[data-testid="zoom-viewport"]') as HTMLElement;
  const transform = () => content().style.transform;
  const press = async (key: string, extra: KeyboardEventInit = {}) => {
    await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, ...extra })); });
  };
  const click = async (el: Element) => {
    await act(async () => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  };
  const wheel = async (deltaY: number, clientX = 0, clientY = 0) => {
    await act(async () => {
      viewport().dispatchEvent(new WheelEvent('wheel', { deltaY, clientX, clientY, bubbles: true, cancelable: true }));
    });
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    index = 0;
    onClose = vi.fn();
    onDelete = undefined;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });

  it('opens at fit, with zoom out and reset unavailable', async () => {
    await render();

    expect(percent()).toBe('100%');
    expect(button('Zoom out').disabled).toBe(true);
    expect(button('Reset zoom').disabled).toBe(true);
    expect(button('Zoom in').disabled).toBe(false);
    expect(transform()).toContain('scale(1)');
  });

  it('zooms in and out with the buttons, a quarter at a time', async () => {
    await render();
    await click(button('Zoom in'));
    await click(button('Zoom in'));

    expect(percent()).toBe('150%');
    expect(transform()).toContain('scale(1.5)');
    expect(button('Zoom out').disabled).toBe(false);
    expect(button('Reset zoom').disabled).toBe(false);

    await click(button('Zoom out'));
    expect(percent()).toBe('125%');
  });

  it('never zooms out past the whole picture, nor in past the maximum', async () => {
    await render();
    await click(button('Zoom out'));
    expect(percent()).toBe('100%');

    for (let i = 0; i < 40; i += 1) await click(button('Zoom in'));
    expect(percent()).toBe(`${MAX_ZOOM * 100}%`);
    expect(button('Zoom in').disabled).toBe(true);
  });

  it('resets to fit', async () => {
    await render();
    await click(button('Zoom in'));
    await click(button('Zoom in'));
    await click(button('Reset zoom'));

    expect(percent()).toBe('100%');
    expect(transform()).toContain('translate(0px, 0px) scale(1)');
  });

  it('zooms with + and -, and resets with 0', async () => {
    await render();
    await press('+');
    expect(percent()).toBe('125%');
    await press('=');
    expect(percent()).toBe('150%');
    await press('-');
    expect(percent()).toBe('125%');
    await press('0');
    expect(percent()).toBe('100%');
  });

  it('leaves the browser\'s own zoom chords alone', async () => {
    await render();
    await press('+', { ctrlKey: true });
    await press('-', { metaKey: true });
    await press('=', { altKey: true });

    expect(percent()).toBe('100%');
  });

  it('zooms with the mouse wheel, and stops the page behind from scrolling', async () => {
    await render();
    const event = new WheelEvent('wheel', { deltaY: -200, bubbles: true, cancelable: true });
    await act(async () => { viewport().dispatchEvent(event); });

    expect(event.defaultPrevented).toBe(true);
    expect(Number(percent()!.replace('%', ''))).toBeGreaterThan(100);

    await wheel(2000);
    expect(percent()).toBe('100%');
  });

  it('zooms on a double-click, and double-click again returns to fit', async () => {
    await render();
    await act(async () => { viewport().dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); });
    expect(percent()).toBe('200%');

    await act(async () => { viewport().dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); });
    expect(percent()).toBe('100%');
  });

  it('starts every screenshot at fit, never at the previous one\'s zoom', async () => {
    await render();
    await click(button('Zoom in'));
    await click(button('Zoom in'));
    expect(percent()).toBe('150%');

    await click(button('Next screenshot'));

    expect(container.textContent).toContain('2 of 3');
    expect(percent()).toBe('100%');
    expect(transform()).toContain('translate(0px, 0px) scale(1)');
  });

  describe('moving the picture', () => {
    // jsdom has no layout, so give the frame and the picture the sizes a real
    // 1000 x 600 viewer showing an 800 x 400 screenshot would have.
    const size = (el: HTMLElement) => ({ w: el.tagName === 'IMG' ? 800 : 1000, h: el.tagName === 'IMG' ? 400 : 600 });
    beforeEach(() => {
      vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockImplementation(function (this: HTMLElement) { return size(this).w; });
      vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) { return size(this).h; });
      vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
        return { x: 0, y: 0, left: 0, top: 0, width: 1000, height: 600, right: 1000, bottom: 600, toJSON: () => ({}) } as DOMRect;
      });
      // jsdom has no pointer capture.
      HTMLElement.prototype.setPointerCapture = vi.fn();
    });

    const dragBy = async (dx: number, dy: number) => {
      await act(async () => {
        viewport().dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0, clientX: 500, clientY: 300, pointerId: 1 }));
      });
      await act(async () => {
        viewport().dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX: 500 + dx, clientY: 300 + dy, pointerId: 1 }));
      });
      await act(async () => {
        viewport().dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1 }));
      });
    };

    it('does not move a picture that is at fit', async () => {
      await render();
      await dragBy(120, 80);

      expect(transform()).toContain('translate(0px, 0px)');
    });

    it('drags a zoomed picture', async () => {
      await render();
      for (let i = 0; i < 8; i += 1) await click(button('Zoom in')); // 300%: 2400 x 1200 in a 1000 x 600 frame
      await dragBy(120, 80);

      expect(transform()).toContain('translate(120px, 80px)');
    });

    it('stops at the picture\'s own edges', async () => {
      await render();
      for (let i = 0; i < 8; i += 1) await click(button('Zoom in')); // 300%
      await dragBy(5000, -5000);

      // (2400 - 1000) / 2 = 700 across, (1200 - 600) / 2 = 300 down.
      expect(transform()).toContain('translate(700px, -300px)');
    });

    it('keeps the point under the pointer where it was when zooming with the wheel', async () => {
      await render();
      for (let i = 0; i < 4; i += 1) await click(button('Zoom in')); // 200%
      // 300px right of the centre of the frame.
      await wheel(-1, 800, 300);

      const match = /translate\((-?[\d.]+)px, (-?[\d.]+)px\)/.exec(transform())!;
      // Zooming in on a point right of centre moves the picture left, so that point stays put.
      expect(Number(match[1])).toBeLessThan(0);
    });

    it('returns to the centre when zoomed back out to fit', async () => {
      await render();
      for (let i = 0; i < 8; i += 1) await click(button('Zoom in'));
      await dragBy(300, 100);
      await press('0');

      expect(transform()).toContain('translate(0px, 0px) scale(1)');
    });
  });

  describe('everything it did before', () => {
    it('keeps the arrow keys and Escape', async () => {
      await render();
      await press('ArrowRight');
      expect(index).toBe(1);
      await press('ArrowLeft');
      expect(index).toBe(0);
      await press('Escape');
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('still names whose screen it is and when', async () => {
      await render();

      expect(container.textContent).toContain('Alice Active');
      expect(container.textContent).toContain('1 of 3');
      expect(container.textContent).toContain('71% activity');
      expect(container.querySelector('[data-testid="picture"]')!.getAttribute('alt')).toContain('Alice Active');
    });

    it('offers nothing to delete unless the caller passes a handler', async () => {
      await render();
      expect(container.textContent).not.toContain('Delete screenshot');

      onDelete = vi.fn();
      await render();
      expect(container.textContent).toContain('Delete screenshot');
    });

    it('closes when the backdrop is clicked, not when the picture is', async () => {
      await render();
      await click(viewport());
      expect(onClose).not.toHaveBeenCalled();

      await click(container.querySelector('[role="dialog"]')!);
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });
});
