// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';

import { FOCUS_REFETCH_MIN_INTERVAL_MS, throttledFocusListeners } from '../focusListeners';

describe('the focus listener', () => {
  let stop: (() => void) | undefined;
  afterEach(() => stop?.());

  const setup = () => {
    const dispatched: string[] = [];
    let clock = 1_000_000;
    stop = throttledFocusListeners(
      (action) => dispatched.push(action as string),
      { onFocus: () => 'focus', onFocusLost: () => 'lost', onOnline: () => 'online', onOffline: () => 'offline' },
      () => clock,
    );
    return { dispatched, advance: (ms: number) => { clock += ms; } };
  };

  it('refetches on the first focus', () => {
    const { dispatched } = setup();
    window.dispatchEvent(new Event('focus'));
    expect(dispatched).toEqual(['focus']);
  });

  it('does not refetch again for an alt-tab a moment later', () => {
    const { dispatched, advance } = setup();
    window.dispatchEvent(new Event('focus'));
    for (let i = 0; i < 10; i += 1) {
      advance(1_000);
      window.dispatchEvent(new Event('focus'));
      document.dispatchEvent(new Event('visibilitychange'));
    }
    expect(dispatched.filter((a) => a === 'focus')).toHaveLength(1);
  });

  it('refetches again once the window has passed', () => {
    const { dispatched, advance } = setup();
    window.dispatchEvent(new Event('focus'));
    advance(FOCUS_REFETCH_MIN_INTERVAL_MS + 1);
    window.dispatchEvent(new Event('focus'));
    expect(dispatched.filter((a) => a === 'focus')).toHaveLength(2);
  });

  it('never throttles coming back online -- that is the recovery', () => {
    const { dispatched } = setup();
    window.dispatchEvent(new Event('online'));
    window.dispatchEvent(new Event('online'));
    expect(dispatched).toEqual(['online', 'online']);
  });

  it('stops listening when told to', () => {
    const { dispatched } = setup();
    stop?.();
    stop = undefined;
    window.dispatchEvent(new Event('focus'));
    expect(dispatched).toEqual([]);
  });
});
