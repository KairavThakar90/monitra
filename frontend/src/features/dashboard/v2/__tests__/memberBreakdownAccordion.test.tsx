// @vitest-environment jsdom
/**
 * The report pages' Member Breakdown is an accordion twice over: a member opens
 * to their days, and each day opens to its projects / tasks / apps / sites.
 * The newest day opens with the member; older days start collapsed; Expand all
 * and Collapse all act on one member's days.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { MemberBreakdownAccordion, type MemberItemBreakdown } from '../MemberBreakdownAccordion';

const members: MemberItemBreakdown[] = [
  {
    member_id: 1,
    member_name: 'Manav Store',
    seconds: 600,
    dates: [
      { date: '2026-10-01', seconds: 225, items: [{ name: 'dummy', seconds: 225 }] },
      { date: '2026-09-30', seconds: 300, items: [{ name: 'V2', seconds: 300 }] },
      { date: '2026-09-29', seconds: 75, items: [{ name: 'Beta Launch', seconds: 75 }] },
    ],
  },
  {
    member_id: 2,
    member_name: 'One Day',
    seconds: 60,
    dates: [{ date: '2026-10-01', seconds: 60, items: [{ name: 'solo', seconds: 60 }] }],
  },
];

describe('MemberBreakdownAccordion', () => {
  let container: HTMLDivElement;
  let root: Root;

  const click = async (el: Element | undefined) => {
    expect(el).toBeTruthy();
    await act(async () => { el!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  };
  const button = (text: string) =>
    Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.includes(text));
  const dayButton = (label: string) =>
    Array.from(container.querySelectorAll('button[aria-controls][aria-expanded]')).find((b) =>
      b.textContent?.toUpperCase().includes(label),
    );
  const panelOf = (b: Element | undefined) => document.getElementById(b!.getAttribute('aria-controls')!)!;

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root.render(
        <MemberBreakdownAccordion
          members={members}
          isLoading={false}
          isTruncated={false}
          emptyLabel="none"
          itemLabel="Project"
          accentColor="#2563EB"
        />,
      );
    });
    await click(button('Manav Store'));
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it('opens the newest day with the member and keeps older days collapsed', () => {
    expect(dayButton('01 OCT')!.getAttribute('aria-expanded')).toBe('true');
    expect(panelOf(dayButton('01 OCT')).hidden).toBe(false);
    expect(dayButton('30 SEP')!.getAttribute('aria-expanded')).toBe('false');
    expect(panelOf(dayButton('30 SEP')).hidden).toBe(true);
    expect(panelOf(dayButton('29 SEP')).hidden).toBe(true);
  });

  it('opens and closes one day without touching the others', async () => {
    await click(dayButton('30 SEP'));
    expect(panelOf(dayButton('30 SEP')).hidden).toBe(false);
    expect(panelOf(dayButton('01 OCT')).hidden).toBe(false);
    expect(panelOf(dayButton('29 SEP')).hidden).toBe(true);

    await click(dayButton('01 OCT'));
    expect(panelOf(dayButton('01 OCT')).hidden).toBe(true);
    expect(panelOf(dayButton('30 SEP')).hidden).toBe(false);
  });

  it('expands and collapses every day of the member at once', async () => {
    await click(button('Expand all days'));
    for (const label of ['01 OCT', '30 SEP', '29 SEP']) expect(panelOf(dayButton(label)).hidden).toBe(false);

    await click(button('Collapse all days'));
    for (const label of ['01 OCT', '30 SEP', '29 SEP']) expect(panelOf(dayButton(label)).hidden).toBe(true);
  });

  it('offers no expand-all control for a member with a single day', async () => {
    await click(button('One Day'));
    expect(container.textContent!.match(/Expand all days|Collapse all days/g)).toHaveLength(1); // Manav's only
  });
});
