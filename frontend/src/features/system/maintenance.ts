/**
 * The maintenance notice: the words, the cadence, and the one rule.
 *
 * The rule is that the toast reacts to a *transition* of the backend's answer
 * and never to the answer itself. The status is polled for as long as the
 * session lasts and answers `true` on every poll while maintenance is on; a
 * component that showed a toast per answer would show one every poll. So the
 * decision is made here, from the previous answer and the next one, and the
 * component only ever applies "show", "hide" or nothing.
 *
 * Kept free of React so it can be tested as a function.
 */

/**
 * How often a signed-in session asks. Thirty seconds matches the cadence the
 * desktop already uses for its change probe: an administrator's switch
 * reaches every open session within about half a minute, and the request is
 * a single-row read.
 */
export const MAINTENANCE_POLL_INTERVAL_MS = 30_000;

/**
 * The notice, word for word. Deliberately calm: it says the work is safe and
 * that Monitra is still running, and it names no failure. The desktop client
 * renders the same three strings.
 */
export const MAINTENANCE_COPY = {
  brand: 'MONITRA',
  title: 'Monitra is under maintenance',
  body:
    "We're currently performing maintenance. Your activity is being saved safely " +
    'offline and will sync automatically when connectivity is available.',
  status: 'OFFLINE',
} as const;

export type MaintenanceTransition = 'show' | 'hide' | 'none';

/**
 * What the toast should do, given the last answer it acted on and the newest
 * one from the backend.
 *
 * `undefined` for `next` means "no answer" -- the query has not resolved, or
 * the last poll failed and RTK Query is still holding the previous data. A
 * missing answer changes nothing: the notice is not cleared because the
 * backend could not be reached, and not raised because it could not be
 * reached either. Offline is the network's fact, not this one.
 *
 * `undefined` for `previous` is the start of a session. The first answer is a
 * transition only if it is `true`; there is nothing to hide otherwise.
 */
export function maintenanceTransition(
  previous: boolean | undefined,
  next: boolean | undefined,
): MaintenanceTransition {
  if (next === undefined) return 'none';
  if (previous === next) return 'none';
  if (next) return 'show';
  return previous === undefined ? 'none' : 'hide';
}

export interface MaintenanceNoticeState {
  /** The last answer the notice acted on; undefined until the backend answers. */
  known: boolean | undefined;
  /** Whether the toast is on screen. */
  visible: boolean;
}

export const INITIAL_MAINTENANCE_NOTICE: MaintenanceNoticeState = { known: undefined, visible: false };

/** Apply one answer to the notice's state. Pure; returns the same object when nothing changes. */
export function applyMaintenanceAnswer(
  state: MaintenanceNoticeState,
  answer: boolean | undefined,
): MaintenanceNoticeState {
  const transition = maintenanceTransition(state.known, answer);
  const known = answer === undefined ? state.known : answer;
  if (transition === 'none') {
    return known === state.known ? state : { ...state, known };
  }
  return { known, visible: transition === 'show' };
}
