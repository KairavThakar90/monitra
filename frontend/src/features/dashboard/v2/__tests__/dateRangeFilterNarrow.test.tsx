// @vitest-environment jsdom
/**
 * The date-range picker on a phone.
 *
 * Wide screens get the presets rail and two months side by side. That panel is
 * ~700px, so on a phone it ran off the screen and the second month could not be
 * reached; below Tailwind's `sm` breakpoint the picker renders ONE calendar in a
 * bottom sheet instead. jsdom has no `matchMedia`, so the wide layout is what
 * every other test sees; these stub it to cover both.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DateRangeFilter, rangeFor, type DateRange } from '../filters';
import { istToday } from '../mockData';

const stubMatchMedia = (matches: boolean) => {
  const listeners = new Set<() => void>();
  const query = {
    matches,
    media: '(max-width: 639px)',
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  };
  vi.stubGlobal('matchMedia', () => query);
  window.matchMedia = (() => query) as unknown as typeof window.matchMedia;
  return {
    set: (next: boolean) => {
      query.matches = next;
      listeners.forEach((fn) => fn());
    },
  };
};

describe('DateRangeFilter on a phone', () => {
  let container: HTMLDivElement;
  let root: Root;
  const original = window.matchMedia;

  const mount = async (onChange: (r: DateRange) => void = () => {}) => {
    const value = rangeFor('today', { preset: 'today', from: '', to: '' });
    await act(async () => {
      root.render(<DateRangeFilter allowAll value={value} onChange={onChange} />);
    });
  };
  const open = async () => {
    const trigger = container.querySelector('button') as HTMLButtonElement;
    await act(async () => { trigger.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  };
  const prevArrows = () => container.querySelectorAll('button[aria-label="Previous month"]');
  const nextArrows = () => container.querySelectorAll('button[aria-label="Next month"]');
  const isHidden = (el: Element) => el.classList.contains('invisible');

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    window.matchMedia = original;
    vi.unstubAllGlobals();
  });

  it('shows two calendars on a wide screen', async () => {
    stubMatchMedia(false);
    await mount();
    await open();
    expect(prevArrows()).toHaveLength(2);
    expect(container.querySelector('[class*="max-sm:fixed"]')).not.toBeNull();
    expect(document.querySelector('.fixed.inset-0')).toBeNull();
  });

  it('shows one calendar, over a backdrop, on a phone', async () => {
    stubMatchMedia(true);
    await mount();
    await open();
    expect(prevArrows()).toHaveLength(1);
    expect(nextArrows()).toHaveLength(1);
    expect(container.querySelector('.fixed.inset-0')).not.toBeNull();
  });

  it('opens the phone calendar on the month of the range, not the month before it', async () => {
    stubMatchMedia(true);
    await mount();
    await open();
    const heading = container.querySelector('.text-\\[15px\\]')!.textContent ?? '';
    const today = istToday();
    // The wide layout clamps its left pane to the month BEFORE the current one
    // (so the right pane is never a future month); one pane has no such need.
    expect(heading).toBe(`${today.toLocaleString('en-US', { month: 'long' })} ${today.getFullYear()}`);
  });

  it('has no arrow towards a month that has no selectable days', async () => {
    stubMatchMedia(true);
    await mount();
    await open();
    // Opens on the current month: there is nothing after it to go to.
    expect(isHidden(nextArrows()[0])).toBe(true);
    expect(isHidden(prevArrows()[0])).toBe(false);
  });

  it('closes when the backdrop is tapped', async () => {
    stubMatchMedia(true);
    await mount();
    await open();
    const backdrop = container.querySelector('.fixed.inset-0') as HTMLElement;
    await act(async () => { backdrop.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(prevArrows()).toHaveLength(0);
    expect(container.querySelector('.fixed.inset-0')).toBeNull();
  });

  it('picks a preset from the grid and closes', async () => {
    stubMatchMedia(true);
    const onChange = vi.fn();
    await mount(onChange);
    await open();
    const yesterday = Array.from(container.querySelectorAll('button')).find((b) => b.textContent === 'Yesterday')!;
    await act(async () => { yesterday.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0].preset).toBe('yesterday');
    expect(prevArrows()).toHaveLength(0);
  });
});
