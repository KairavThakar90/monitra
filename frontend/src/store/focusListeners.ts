/**
 * RTK Query's own handler re-sends EVERY active query on every focus -- the
 * comment above the API slice promised "older than the staleness window", and
 * that gate does not exist. Alt-tabbing between the desktop app and the
 * dashboard therefore fired the dashboard's two heavy requests, the project
 * fan-out and the profile read each time. The same events, but a focus within
 * `FOCUS_REFETCH_MIN_INTERVAL_MS` of the last one that refetched is ignored.
 * Coming back online is never throttled: it is rare and it is the recovery.
 */
export const FOCUS_REFETCH_MIN_INTERVAL_MS = 30_000;

interface ListenerActions {
  onFocus: () => unknown;
  onFocusLost: () => unknown;
  onOnline: () => unknown;
  onOffline: () => unknown;
}

export const throttledFocusListeners = (
  dispatch: (action: unknown) => unknown,
  { onFocus, onFocusLost, onOnline, onOffline }: ListenerActions,
  now: () => number = Date.now,
): (() => void) => {
  let lastFocusRefetch = -Infinity;
  const handleFocus = () => {
    const at = now();
    if (at - lastFocusRefetch < FOCUS_REFETCH_MIN_INTERVAL_MS) return;
    lastFocusRefetch = at;
    dispatch(onFocus());
  };
  const handleVisibility = () => {
    if (document.visibilityState === 'visible') handleFocus();
    else dispatch(onFocusLost());
  };
  const handleOnline = () => dispatch(onOnline());
  const handleOffline = () => dispatch(onOffline());
  window.addEventListener('focus', handleFocus, false);
  document.addEventListener('visibilitychange', handleVisibility, false);
  window.addEventListener('online', handleOnline, false);
  window.addEventListener('offline', handleOffline, false);
  return () => {
    window.removeEventListener('focus', handleFocus);
    document.removeEventListener('visibilitychange', handleVisibility);
    window.removeEventListener('online', handleOnline);
    window.removeEventListener('offline', handleOffline);
  };
};
