// @vitest-environment jsdom
/**
 * The maintenance toast, rendered.
 *
 * Against a real Redux store and the real RTK Query slice, with `fetch`
 * answered by the test: the card appears when the polled flag turns true,
 * carries the logo, the agreed words and the OFFLINE pill, sits in a corner
 * without a backdrop or a dialog role, leaves a control underneath it fully
 * clickable, is not duplicated by a repeated `true`, disappears on `false`,
 * and is re-asked for on the polling interval and on the browser's `online`
 * event -- so an administrator's switch reaches an open tab without a reload.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { setupListeners } from '@reduxjs/toolkit/query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../store/api/baseApi';
import { systemApi, type MaintenanceStatus } from '../../store/api/systemApi';
import { MAINTENANCE_COPY, MAINTENANCE_POLL_INTERVAL_MS } from '../../features/system/maintenance';
import { MaintenanceNotice } from '../MaintenanceToast';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const status = (maintenance_mode: boolean): MaintenanceStatus => ({
  maintenance_mode,
  updated_at: null,
  server_time: '2026-09-16T10:00:00+00:00',
});

const jsonResponse = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });

const makeStore = () =>
  configureStore({
    reducer: { [baseApi.reducerPath]: baseApi.reducer },
    middleware: (gdm) => gdm().concat(baseApi.middleware),
  });

/** Let RTK Query's thunks and React's effects settle. Fake-timer aware. */
const flush = async () => {
  for (let i = 0; i < 3; i += 1) {
    await act(async () => {
      if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(1);
      else await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
};

describe('MaintenanceNotice', () => {
  let container: HTMLDivElement;
  let root: Root;
  let store: ReturnType<typeof makeStore>;
  let answer: boolean;
  let fetchMock: ReturnType<typeof vi.fn>;
  let clicks = 0;

  const toasts = () => container.querySelectorAll('[data-testid="maintenance-toast"]');

  const mount = async (enabled: boolean) => {
    await act(async () => {
      root.render(
        <Provider store={store}>
          <div>
            <button type="button" id="under" onClick={() => { clicks += 1; }}>Start timer</button>
            <MaintenanceNotice enabled={enabled} />
          </div>
        </Provider>,
      );
    });
    await flush();
  };

  const flip = async (value: boolean) => {
    // `upsertQueryData` is an async thunk: await it, as a real fulfilment
    // would be awaited, before looking at what rendered.
    await act(async () => {
      await store.dispatch(systemApi.util.upsertQueryData('getMaintenanceStatus', undefined, status(value)));
    });
    await flush();
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'token');
    localStorage.setItem('refreshToken', 'refresh');
    answer = false;
    clicks = 0;
    fetchMock = vi.fn(async () => jsonResponse(status(answer)));
    vi.stubGlobal('fetch', fetchMock);
    store = makeStore();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => { root.unmount(); });
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    localStorage.clear();
  });

  it('shows nothing while maintenance is off', async () => {
    await mount(true);
    expect(fetchMock).toHaveBeenCalled();
    expect(toasts().length).toBe(0);
  });

  it('false -> true shows one toast with the logo, the words and the OFFLINE pill', async () => {
    await mount(true);
    await flip(true);

    expect(toasts().length).toBe(1);
    const toast = toasts()[0];
    expect(toast.getAttribute('role')).toBe('status');
    const logo = toast.querySelector('img');
    expect(logo?.getAttribute('alt')).toBe('Monitra');
    expect(logo?.getAttribute('src')).toContain('logo');
    expect(toast.textContent).toContain(MAINTENANCE_COPY.title);
    expect(toast.textContent).toContain(MAINTENANCE_COPY.body);
    const pill = toast.querySelector('[data-testid="maintenance-status"]');
    expect(pill?.textContent?.trim()).toBe('OFFLINE');
  });

  it('never blocks the application underneath it', async () => {
    await mount(true);
    await flip(true);

    // No overlay, no dialog, no acknowledgement. The one control it has
    // minimizes it; there is nothing that dismisses or confirms it.
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(container.querySelector('[aria-modal="true"]')).toBeNull();
    const buttons = Array.from(toasts()[0].querySelectorAll('button'));
    expect(buttons.map((b) => b.getAttribute('data-testid'))).toEqual(['maintenance-minimize']);
    const shell = toasts()[0] as HTMLElement;
    expect(shell.className).toContain('fixed');
    expect(shell.className).toContain('pointer-events-none');
    expect(shell.className).not.toContain('inset-0');

    // The control underneath still works.
    const under = container.querySelector<HTMLButtonElement>('#under')!;
    expect(under.disabled).toBe(false);
    await act(async () => { under.click(); });
    expect(clicks).toBe(1);
  });

  it('true -> true adds nothing; true -> false removes it', async () => {
    await mount(true);
    await flip(true);
    await flip(true);
    await flip(true);
    expect(toasts().length).toBe(1);

    await flip(false);
    expect(toasts().length).toBe(0);

    await flip(false);
    expect(toasts().length).toBe(0);
  });

  it('a second maintenance window shows the toast again', async () => {
    await mount(true);
    await flip(true);
    await flip(false);
    await flip(true);
    expect(toasts().length).toBe(1);
  });

  it('signing out clears it and the next session starts from unknown', async () => {
    await mount(true);
    await flip(true);
    expect(toasts().length).toBe(1);

    await mount(false);
    expect(toasts().length).toBe(0);
  });

  it('polls on the interval, so a change reaches an open tab without a reload', async () => {
    vi.useFakeTimers();
    await mount(true);
    const before = fetchMock.mock.calls.length;
    expect(before).toBeGreaterThan(0);
    expect(toasts().length).toBe(0);

    answer = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(MAINTENANCE_POLL_INTERVAL_MS + 100); });
    await flush();

    expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
    expect(toasts().length).toBe(1);
  });

  describe('minimizing and moving the notice', () => {
    const pill = () => container.querySelector<HTMLButtonElement>('[data-testid="maintenance-expand"]');
    const minimize = () => container.querySelector<HTMLButtonElement>('[data-testid="maintenance-minimize"]');
    const dragArea = () => container.querySelector<HTMLElement>('[data-testid="maintenance-drag-area"]')!;
    const transform = () => (toasts()[0] as HTMLElement).getAttribute('style') ?? '';

    const press = async (el: Element, type: 'click' | 'pointerdown' | 'pointermove' | 'pointerup', x = 0, y = 0) => {
      await act(async () => {
        el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 }));
      });
    };
    /** A press at (x, y), a move to (x + dx, y + dy), and the release there. */
    const dragBy = async (el: Element, dx: number, dy: number) => {
      await press(el, 'pointerdown', 500, 500);
      await press(el, 'pointermove', 500 + dx, 500 + dy);
      await press(el, 'pointerup', 500 + dx, 500 + dy);
    };

    /** A laid-out box in a 1280 x 800 window; jsdom has no layout of its own. */
    const layOut = (box: { left: number; top: number; width: number; height: number }) => {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 });
      const el = toasts()[0] as HTMLElement;
      el.getBoundingClientRect = () =>
        ({
          ...box,
          right: box.left + box.width,
          bottom: box.top + box.height,
          x: box.left,
          y: box.top,
          toJSON: () => ({}),
        }) as DOMRect;
    };
    const RESTING = { left: 900, top: 700, width: 360, height: 80 };

    let originalWidth = 0;
    let originalHeight = 0;
    beforeEach(() => {
      originalWidth = window.innerWidth;
      originalHeight = window.innerHeight;
    });
    afterEach(() => {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: originalHeight });
    });

    it('minimizes to a pill that is still the notice, and expands again', async () => {
      await mount(true);
      await flip(true);
      expect(pill()).toBeNull();

      await press(minimize()!, 'click');
      // Still there -- the fact stays on screen -- but small, with no card text.
      expect(toasts().length).toBe(1);
      expect(pill()?.textContent).toContain('Under maintenance');
      expect(minimize()).toBeNull();
      expect(toasts()[0].textContent).not.toContain(MAINTENANCE_COPY.body);
      expect(toasts()[0].getAttribute('role')).toBe('status');

      await press(pill()!, 'click');
      expect(pill()).toBeNull();
      expect(toasts()[0].textContent).toContain(MAINTENANCE_COPY.body);
    });

    it('never hides the control underneath, minimized or not', async () => {
      await mount(true);
      await flip(true);
      await press(minimize()!, 'click');
      const under = container.querySelector<HTMLButtonElement>('#under')!;
      await act(async () => { under.click(); });
      expect(clicks).toBe(1);
      expect((toasts()[0] as HTMLElement).className).toContain('pointer-events-none');
    });

    it('stays minimized across polls, and starts a new maintenance window open', async () => {
      await mount(true);
      await flip(true);
      await press(minimize()!, 'click');

      await flip(true);
      expect(pill()).not.toBeNull();

      await flip(false);
      expect(toasts().length).toBe(0);
      await flip(true);
      expect(toasts().length).toBe(1);
      expect(pill()).toBeNull();
      expect(toasts()[0].textContent).toContain(MAINTENANCE_COPY.body);
    });

    it('starts in its corner, with no offset', async () => {
      await mount(true);
      await flip(true);
      expect(transform()).toMatch(/translate3d\(0(px)?, 0(px)?, 0(px)?\)/);
    });

    it('is dragged by the distance the pointer moves', async () => {
      await mount(true);
      await flip(true);
      layOut(RESTING);

      await dragBy(dragArea(), -200, -100);
      expect(transform()).toContain('-200px');
      expect(transform()).toContain('-100px');
    });

    it('moves the pill too', async () => {
      await mount(true);
      await flip(true);
      await press(minimize()!, 'click');
      layOut({ left: 1080, top: 720, width: 180, height: 40 });

      await dragBy(dragArea(), -300, -250);
      expect(transform()).toContain('-300px');
      expect(transform()).toContain('-250px');
      // Still a pill after being moved.
      expect(pill()).not.toBeNull();
    });

    it('keeps dragging when the pointer leaves the notice, which a quick flick of the small pill does', async () => {
      await mount(true);
      await flip(true);
      await press(minimize()!, 'click');
      layOut({ left: 1080, top: 720, width: 180, height: 40 });

      // Pressed on the pill; every move and the release arrive on the page,
      // not on the pill, exactly as when the pointer outruns a 36px target.
      await press(pill()!, 'pointerdown', 500, 500);
      await press(document.body, 'pointermove', 400, 450);
      await press(document.body, 'pointerup', 400, 450);

      expect(transform()).toContain('-100px');
      expect(transform()).toContain('-50px');
    });

    it('stops following the pointer once it is released', async () => {
      await mount(true);
      await flip(true);
      layOut(RESTING);

      await dragBy(dragArea(), -100, -100);
      const after = transform();
      await press(document.body, 'pointermove', 100, 100);
      expect(transform()).toBe(after);
    });

    it('leaves nothing listening on the window if it goes away mid-press', async () => {
      const added = vi.spyOn(window, 'addEventListener');
      const removed = vi.spyOn(window, 'removeEventListener');
      await mount(true);
      await flip(true);
      layOut(RESTING);

      await press(dragArea(), 'pointerdown', 500, 500);
      const pointerAdds = added.mock.calls.filter(([type]) => String(type).startsWith('pointer')).length;
      expect(pointerAdds).toBeGreaterThan(0);

      await flip(false); // the notice is removed with the press still down
      const pointerRemoves = removed.mock.calls.filter(([type]) => String(type).startsWith('pointer')).length;
      expect(pointerRemoves).toBe(pointerAdds);
      added.mockRestore();
      removed.mockRestore();
    });

    it('a press that barely moves is a click, not a drag', async () => {
      await mount(true);
      await flip(true);
      layOut(RESTING);

      await dragBy(dragArea(), 2, 1);
      expect(transform()).toMatch(/translate3d\(0(px)?, 0(px)?, 0(px)?\)/);
    });

    it('cannot be dragged off the screen', async () => {
      await mount(true);
      await flip(true);
      layOut(RESTING);

      // Far past the top-left corner: stops 8px from each edge.
      await dragBy(dragArea(), -5000, -5000);
      expect(transform()).toContain(`${8 - RESTING.left}px`);
      expect(transform()).toContain(`${8 - RESTING.top}px`);
    });

    it('a drag that ends over a button does not press it', async () => {
      await mount(true);
      await flip(true);
      layOut(RESTING);

      // Pressed on the minimize button, dragged away, released: the click the
      // browser sends afterwards must not collapse the card.
      const button = minimize()!;
      await press(button, 'pointerdown', 500, 500);
      await press(button, 'pointermove', 400, 450);
      await press(button, 'pointerup', 400, 450);
      await press(button, 'click', 400, 450);

      expect(pill()).toBeNull();
      expect(minimize()).not.toBeNull();
      expect(transform()).toContain('-100px');
    });

    it('a plain click on the minimize button still minimizes it', async () => {
      await mount(true);
      await flip(true);
      layOut(RESTING);

      const button = minimize()!;
      await press(button, 'pointerdown', 500, 500);
      await press(button, 'pointerup', 500, 500);
      await press(button, 'click', 500, 500);

      expect(pill()).not.toBeNull();
    });

    it('is brought back onto the screen when the window shrinks', async () => {
      await mount(true);
      await flip(true);
      // The window is now narrower than where the notice sits.
      layOut({ left: 1200, top: 700, width: 360, height: 80 });

      await act(async () => { window.dispatchEvent(new Event('resize')); });

      // 1280 - 8 - (1200 + 360) = -288 to the left.
      expect(transform()).toContain('-288px');
    });
  });

  it('re-asks when the browser comes back online', async () => {
    // The app binds RTK's window listeners to its own store once, in
    // `store/index.ts`, and that module-level registration is already taken
    // by the time this file's imports resolve. Bind the same `online`
    // handler to this test's store, so the event reaches the slice under
    // test with the slice's own `refetchOnReconnect` configuration.
    const unsubscribe = setupListeners(store.dispatch, (dispatch, actions) => {
      const handleOnline = () => dispatch(actions.onOnline());
      window.addEventListener('online', handleOnline);
      return () => window.removeEventListener('online', handleOnline);
    });
    try {
      await mount(true);
      const before = fetchMock.mock.calls.length;
      expect(before).toBeGreaterThan(0);

      answer = true;
      await act(async () => { window.dispatchEvent(new Event('online')); });
      await flush();

      expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
      expect(toasts().length).toBe(1);
    } finally {
      unsubscribe();
    }
  });
});
