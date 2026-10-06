import { useCallback, useEffect, useRef, useState } from 'react';
import { istTodayISO } from '../utils/duration';

/** How often the drift check reads the clock. */
export const RESUME_TICK_MS = 5_000;

/**
 * A gap between two checks longer than this means the page was suspended.
 * Four times the tick: a busy main thread or a throttled timer stretches a
 * tick by a second or two, never by twenty.
 */
export const RESUME_GAP_MS = 20_000;

/** How often the IST day is re-read while the page is open and awake. */
const DAY_CHECK_MS = 30_000;

/**
 * Calls `onResume` once when the page comes back from being suspended -- a
 * laptop that slept or hibernated, or a page restored from the back/forward
 * cache.
 *
 * Why this exists: after a wake the window is usually still focused and still
 * visible, so the browser fires neither `focus` nor `visibilitychange`, which
 * are the only two events RTK Query's `refetchOnFocus` listens to. The page
 * therefore kept showing whatever it had fetched before the machine went to
 * sleep. A timer that should fire every few seconds and instead fires after
 * minutes is the one signal a suspend always leaves behind.
 *
 * Only reported while the page is visible. A hidden tab's timers are
 * throttled to once a minute, which looks exactly like a suspend, and nobody
 * is looking at it; coming back to it is a `visibilitychange`, which the
 * slice already handles.
 */
export function useOnResume(onResume: () => void): void {
  const latest = useRef(onResume);
  useEffect(() => {
    latest.current = onResume;
  });

  useEffect(() => {
    let last = Date.now();
    const timer = setInterval(() => {
      const now = Date.now();
      const gap = now - last;
      last = now;
      if (gap > RESUME_GAP_MS && document.visibilityState === 'visible') latest.current();
    }, RESUME_TICK_MS);

    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) latest.current();
    };
    window.addEventListener('pageshow', onPageShow);

    return () => {
      clearInterval(timer);
      window.removeEventListener('pageshow', onPageShow);
    };
  }, []);
}

/**
 * Today's IST date, kept current for as long as the page is mounted.
 *
 * Re-read on a slow tick, whenever the page becomes visible, and on resume,
 * so a tab left open overnight -- or a machine that slept through midnight --
 * moves to the new day without a reload. It changes identity only when the
 * day itself changes, so depending on it does not re-render anything in
 * between.
 */
export function useIstToday(): string {
  const [day, setDay] = useState(() => istTodayISO());

  const refresh = useCallback(() => {
    setDay((current) => {
      const next = istTodayISO();
      return next === current ? current : next;
    });
  }, []);

  useOnResume(refresh);

  useEffect(() => {
    const timer = setInterval(refresh, DAY_CHECK_MS);
    const onVisible = () => {
      if (document.visibilityState === 'visible') refresh();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [refresh]);

  return day;
}
