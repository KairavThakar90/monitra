// @vitest-environment jsdom
/**
 * Noticing that a machine slept, and that midnight passed while it did.
 *
 * After a wake the browser window is usually still focused and still visible,
 * so it fires neither `focus` nor `visibilitychange` -- the only two events
 * RTK Query's `refetchOnFocus` listens to. A timer that should tick every few
 * seconds and instead ticks after an hour is what a suspend always leaves
 * behind, and it is what these hooks read.
 *
 * Only the interval and the clock are faked, so React and the test's own
 * awaits keep running on real timers.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

import { RESUME_GAP_MS, RESUME_TICK_MS, useIstToday, useOnResume } from '../useResume';

const Probe: React.FC<{ onResume: () => void }> = ({ onResume }) => {
  useOnResume(onResume);
  const day = useIstToday();
  return <span data-testid="day">{day}</span>;
};

const HOUR = 60 * 60 * 1000;

describe('useOnResume / useIstToday', () => {
  let container: HTMLDivElement;
  let root: Root;
  let onResume: Mock<() => void>;

  const day = () => container.querySelector('[data-testid="day"]')!.textContent;
  const tick = async (ms: number) => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  };
  /** The machine slept for `ms`: the clock moves, no tick was delivered meanwhile. */
  const sleepFor = async (ms: number) => {
    vi.setSystemTime(Date.now() + ms);
    await tick(RESUME_TICK_MS);
  };
  const setVisibility = (state: 'visible' | 'hidden') => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  };

  const mount = async () => {
    await act(async () => {
      root.render(<Probe onResume={onResume} />);
    });
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    // 23:30 IST on 5 Oct 2026.
    vi.setSystemTime(new Date('2026-10-05T18:00:00Z'));
    setVisibility('visible');
    onResume = vi.fn<() => void>();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    delete (document as { visibilityState?: unknown }).visibilityState;
    vi.useRealTimers();
  });

  describe('useOnResume', () => {
    it('fires once when the clock jumps between two ticks, as it does after a suspend', async () => {
      await mount();
      await sleepFor(2 * HOUR);
      expect(onResume).toHaveBeenCalledTimes(1);
    });

    it('does not fire on ordinary ticks, however long the page has been open', async () => {
      await mount();
      for (let i = 0; i < 40; i += 1) await tick(RESUME_TICK_MS); // 200 s of steady ticking
      expect(onResume).not.toHaveBeenCalled();
    });

    it('is not fooled by a tick that merely ran a little late', async () => {
      await mount();
      await sleepFor(RESUME_GAP_MS - RESUME_TICK_MS - 1_000); // a stretched tick, well under the gap
      expect(onResume).not.toHaveBeenCalled();
    });

    it('stays quiet while the page is hidden: a hidden tab’s throttled timers look like a suspend', async () => {
      await mount();
      setVisibility('hidden');
      await sleepFor(HOUR);
      expect(onResume).not.toHaveBeenCalled();
    });

    it('fires when the page is restored from the back/forward cache, and only then', async () => {
      await mount();
      await act(async () => {
        window.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: false }));
      });
      expect(onResume).not.toHaveBeenCalled();
      await act(async () => {
        window.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: true }));
      });
      expect(onResume).toHaveBeenCalledTimes(1);
    });

    it('calls the latest callback, not the one it was first given', async () => {
      await mount();
      const later = vi.fn<() => void>();
      await act(async () => {
        root.render(<Probe onResume={later} />);
      });
      await sleepFor(HOUR);
      expect(later).toHaveBeenCalledTimes(1);
      expect(onResume).not.toHaveBeenCalled();
    });

    it('stops listening once unmounted', async () => {
      await mount();
      await act(async () => root.unmount());
      await sleepFor(HOUR);
      expect(onResume).not.toHaveBeenCalled();
      root = createRoot(container); // for afterEach
    });
  });

  describe('useIstToday', () => {
    it('starts on the current IST day', async () => {
      await mount();
      expect(day()).toBe('2026-10-05');
    });

    it('moves to the next day when IST midnight passes while the page is open and awake', async () => {
      await mount();
      await tick(20 * 60 * 1000); // 23:50
      expect(day()).toBe('2026-10-05');
      await tick(20 * 60 * 1000); // 00:10 -- steady ticking the whole way, so no resume involved
      expect(onResume).not.toHaveBeenCalled();
      expect(day()).toBe('2026-10-06');
    });

    it('moves to the new day at once when the machine slept through midnight', async () => {
      await mount();
      expect(day()).toBe('2026-10-05');
      await sleepFor(8 * HOUR); // asleep until 07:30 IST on the 6th
      expect(onResume).toHaveBeenCalledTimes(1);
      expect(day()).toBe('2026-10-06');
    });

    it('uses the IST calendar, not the browser’s or UTC', async () => {
      // 19:00 UTC on the 5th is already 00:30 on the 6th in India.
      vi.setSystemTime(new Date('2026-10-05T19:00:00Z'));
      await mount();
      expect(day()).toBe('2026-10-06');
    });
  });
});
