// @vitest-environment jsdom
/**
 * The Screenshots day picker wears the shared range picker's look -- a presets
 * rail beside two months -- but is still a one-day control: a single click
 * commits and closes, and nothing past today can be chosen.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DayFilter, istTodayIso } from '../DayFilter';

describe('DayFilter', () => {
  let container: HTMLDivElement;
  let root: Root;
  const onChange = vi.fn();

  const render = async (value: string) => {
    await act(async () => {
      root.render(<DayFilter value={value} onChange={onChange} />);
    });
  };
  const click = async (el: Element) => {
    await act(async () => {
      el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
  };
  const open = async () => {
    const trigger = container.querySelector('button[aria-expanded]') as HTMLButtonElement;
    await click(trigger);
  };
  const buttonByText = (text: string) =>
    Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.trim() === text) as
      | HTMLButtonElement
      | undefined;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    onChange.mockReset();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it('opens a presets rail beside two months, like the range picker', async () => {
    await render(istTodayIso());
    expect(container.querySelector('[aria-label="Previous month"]')).toBeNull();
    await open();

    for (const label of ['Today', 'Yesterday', '2 days ago', '3 days ago', 'A week ago']) {
      expect(buttonByText(label), label).toBeDefined();
    }
    // Two panes, each with Mo..Su headers.
    expect(container.textContent!.match(/Mo/g)).toHaveLength(2);
  });

  it('commits a preset on one click and closes', async () => {
    await render(istTodayIso());
    await open();

    await click(buttonByText('Yesterday')!);

    expect(onChange).toHaveBeenCalledTimes(1);
    const yesterday = new Date();
    yesterday.setDate(yesterday.getDate() - 1);
    expect(onChange.mock.calls[0][0] <= istTodayIso()).toBe(true);
    expect(onChange.mock.calls[0][0]).not.toBe(istTodayIso());
    expect(container.querySelector('[aria-label="Next month"]')).toBeNull();
  });

  it('commits a clicked calendar day on one click and closes', async () => {
    await render(istTodayIso());
    await open();

    // The 1st of a month shown in the left pane is always in the past or today.
    const day = Array.from(container.querySelectorAll('button:not([disabled])')).find(
      (b) => b.textContent?.trim() === '1' && !b.hasAttribute('aria-label'),
    ) as HTMLButtonElement;
    await click(day);

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0]).toMatch(/^\d{4}-\d{2}-01$/);
    expect(container.textContent).not.toContain('A week ago');
  });

  it('never lets a day after today be chosen', async () => {
    await render(istTodayIso());
    await open();
    const future = Array.from(container.querySelectorAll('button[aria-disabled="true"]'));
    for (const button of future) await click(button);
    expect(onChange).not.toHaveBeenCalled();
  });
});
