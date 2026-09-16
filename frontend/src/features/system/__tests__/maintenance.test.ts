/**
 * The maintenance notice's one rule: react to a transition, never to a level.
 *
 * The status is polled every thirty seconds for the life of a session and
 * answers `true` on every poll while maintenance is on. If the toast reacted
 * to the answer rather than to its change, it would be re-shown every poll.
 * These pin the decision table, and the wording every client renders.
 */
import { describe, expect, it } from 'vitest';

import {
  INITIAL_MAINTENANCE_NOTICE,
  MAINTENANCE_COPY,
  MAINTENANCE_POLL_INTERVAL_MS,
  applyMaintenanceAnswer,
  maintenanceTransition,
} from '../maintenance';

describe('maintenanceTransition', () => {
  it('shows on false -> true and hides on true -> false', () => {
    expect(maintenanceTransition(false, true)).toBe('show');
    expect(maintenanceTransition(true, false)).toBe('hide');
  });

  it('does nothing while the answer is unchanged', () => {
    expect(maintenanceTransition(true, true)).toBe('none');
    expect(maintenanceTransition(false, false)).toBe('none');
  });

  it('treats the first answer of a session as an edge only if it is on', () => {
    expect(maintenanceTransition(undefined, true)).toBe('show');
    expect(maintenanceTransition(undefined, false)).toBe('none');
  });

  it('leaves the notice alone when there is no answer (offline, failed poll, not yet loaded)', () => {
    expect(maintenanceTransition(undefined, undefined)).toBe('none');
    expect(maintenanceTransition(true, undefined)).toBe('none');
    expect(maintenanceTransition(false, undefined)).toBe('none');
  });
});

describe('applyMaintenanceAnswer', () => {
  it('walks a whole maintenance window: on once, held, off once', () => {
    let state = INITIAL_MAINTENANCE_NOTICE;
    const seen: boolean[] = [];
    for (const answer of [false, false, true, true, true, false, false]) {
      const next = applyMaintenanceAnswer(state, answer);
      if (next.visible !== state.visible) seen.push(next.visible);
      state = next;
    }
    expect(seen).toEqual([true, false]);
    expect(state).toEqual({ known: false, visible: false });
  });

  it('returns the same object when nothing changed, so a poll cannot re-render the toast', () => {
    const on = applyMaintenanceAnswer(INITIAL_MAINTENANCE_NOTICE, true);
    expect(on.visible).toBe(true);
    expect(applyMaintenanceAnswer(on, true)).toBe(on);
    expect(applyMaintenanceAnswer(on, undefined)).toBe(on);
  });

  it('keeps a showing notice through a failed poll and clears it only on a real "off"', () => {
    const on = applyMaintenanceAnswer(INITIAL_MAINTENANCE_NOTICE, true);
    const stillOn = applyMaintenanceAnswer(on, undefined);
    expect(stillOn.visible).toBe(true);
    const off = applyMaintenanceAnswer(stillOn, false);
    expect(off.visible).toBe(false);
  });

  it('shows the notice again for a second maintenance window', () => {
    let state = INITIAL_MAINTENANCE_NOTICE;
    state = applyMaintenanceAnswer(state, true);
    state = applyMaintenanceAnswer(state, false);
    state = applyMaintenanceAnswer(state, true);
    expect(state.visible).toBe(true);
  });
});

describe('the words and the cadence', () => {
  it('says what was agreed', () => {
    expect(MAINTENANCE_COPY.title).toBe('Monitra is under maintenance');
    expect(MAINTENANCE_COPY.body).toContain('saved safely offline');
    expect(MAINTENANCE_COPY.body).toContain('sync automatically');
    expect(MAINTENANCE_COPY.status).toBe('OFFLINE');
    expect(MAINTENANCE_COPY.brand).toBe('MONITRA');
  });

  it('names no failure', () => {
    const text = `${MAINTENANCE_COPY.title} ${MAINTENANCE_COPY.body}`.toLowerCase();
    for (const alarming of ['failure', 'lost', 'stopped', 'unavailable', 'disabled', 'error', 'crash']) {
      expect(text).not.toContain(alarming);
    }
  });

  it('polls lightly: between fifteen seconds and a minute', () => {
    expect(MAINTENANCE_POLL_INTERVAL_MS).toBeGreaterThanOrEqual(15_000);
    expect(MAINTENANCE_POLL_INTERVAL_MS).toBeLessThanOrEqual(60_000);
  });
});
