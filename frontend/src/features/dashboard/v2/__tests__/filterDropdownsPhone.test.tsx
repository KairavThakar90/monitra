// @vitest-environment jsdom
/**
 * The member and project filter dropdowns on a phone.
 *
 * Anchored to its button with `left-0` / `right-0` and a fixed width, a panel ran
 * off the screen whenever the button was not at the matching edge ("All members"
 * at the left of a phone opened 190px past the left of the screen). Below `sm`
 * the panel is pinned to the viewport instead, over a backdrop that closes it.
 * jsdom applies no CSS, so these check the classes that carry that behaviour and
 * the backdrop's close handling -- not pixel positions.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MemberMultiSelect, ProjectMultiSelect } from '../filters';

const MEMBERS = [
  { id: 1, name: 'Akshar Solanki', role: 'employee' },
  { id: 2, name: 'Amber Brown', role: 'leader' },
];

describe('filter dropdowns on a phone', () => {
  let container: HTMLDivElement;
  let root: Root;

  const render = async (ui: React.ReactElement) => {
    await act(async () => { root.render(ui); });
  };
  const click = async (el: Element | null | undefined) => {
    expect(el).toBeTruthy();
    await act(async () => { el!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  };
  const trigger = () => container.querySelector('button') as HTMLButtonElement;
  const backdrop = () => container.querySelector('.fixed.inset-0.sm\\:hidden');
  const panel = () =>
    (container.querySelector('input[placeholder^="Search"]')?.closest('.absolute') ?? null) as HTMLElement | null;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  describe('MemberMultiSelect', () => {
    it('pins its panel to the viewport below sm, and keeps the anchored layout from sm up', async () => {
      await render(<MemberMultiSelect members={MEMBERS} selected={[]} onChange={() => {}} />);
      await click(trigger());

      const classes = panel()!.className;
      expect(classes).toContain('max-sm:fixed');
      expect(classes).toContain('max-sm:left-3');
      expect(classes).toContain('max-sm:right-3');
      // Unchanged for wider screens.
      expect(classes).toContain('right-0');
      expect(classes).toContain('w-[280px]');
    });

    it('keeps the left-hung layout for align="left" from sm up', async () => {
      await render(<MemberMultiSelect members={MEMBERS} selected={[]} onChange={() => {}} align="left" />);
      await click(trigger());
      expect(panel()!.className).toContain('left-0');
      expect(panel()!.className).toContain('max-sm:fixed');
    });

    it('dims the page behind the panel, and closes when the backdrop is tapped', async () => {
      await render(<MemberMultiSelect members={MEMBERS} selected={[]} onChange={() => {}} />);
      expect(backdrop()).toBeNull();

      await click(trigger());
      expect(backdrop()).not.toBeNull();
      expect(panel()).not.toBeNull();

      await click(backdrop());
      expect(backdrop()).toBeNull();
      expect(panel()).toBeNull();
    });

    it('does not add a backdrop to the single-member form field, whose panel is its own width', async () => {
      await render(<MemberMultiSelect members={MEMBERS} selected={[]} onChange={() => {}} single />);
      await click(trigger());
      expect(panel()).not.toBeNull();
      expect(backdrop()).toBeNull();
      expect(panel()!.className).not.toContain('max-sm:fixed');
    });

    it('still selects a member from the list', async () => {
      const onChange = vi.fn();
      await render(<MemberMultiSelect members={MEMBERS} selected={[]} onChange={onChange} />);
      await click(trigger());
      const row = Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.includes('Amber Brown'));
      await click(row);
      expect(onChange).toHaveBeenCalledWith(['2']);
    });

    it('sets the search box to 16px on a phone so iOS does not zoom the page', async () => {
      await render(<MemberMultiSelect members={MEMBERS} selected={[]} onChange={() => {}} />);
      await click(trigger());
      expect(container.querySelector<HTMLInputElement>('input[placeholder="Search members..."]')!.className).toContain('max-sm:text-base');
    });
  });

  describe('ProjectMultiSelect', () => {
    const PROJECTS = [{ id: 7, project_name: 'Coach Companion' }] as never;

    it('pins its panel and dims the page the same way', async () => {
      await render(<ProjectMultiSelect projects={PROJECTS} selected={[]} onChange={() => {}} />);
      await click(trigger());

      expect(panel()!.className).toContain('max-sm:fixed');
      expect(backdrop()).not.toBeNull();

      await click(backdrop());
      expect(panel()).toBeNull();
    });
  });
});
