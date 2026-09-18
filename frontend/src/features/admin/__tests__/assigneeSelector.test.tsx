// @vitest-environment jsdom
/**
 * `AssigneeSelector`'s `fullWidth` mode -- the Project Members field in the
 * create/edit project drawer.
 *
 * Two things were reported broken:
 *
 * 1. **"Click anything in the input field."** The drawer used to draw an
 *    input-styled box *around* the selector without that box being part of
 *    its click target: only the small avatar stack, or the small dashed "+"
 *    circle when nothing was picked yet, opened the picker. Everything else
 *    that looked like part of the same field -- the border, the padding,
 *    the empty space beside the icon -- did nothing. `fullWidth` makes the
 *    whole field the trigger.
 * 2. **"The dropdown opens above the input."** The open/closed direction was
 *    computed from the *small inner icon's* position, not the field's, and
 *    it also compared against the menu's full worst-case height rather than
 *    asking whether there was *reasonably* enough room below -- so it
 *    could flip upward even with plenty of visible room beneath the field.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AssigneeSelector } from '../AdminProjectManagement';

const OPTIONS = [
  { id: 1, name: 'Ada Lovelace', role: 'Engineer' },
  { id: 2, name: 'Grace Hopper', role: 'Engineer' },
];

describe('AssigneeSelector (fullWidth)', () => {
  let container: HTMLDivElement;
  let root: Root;
  let isOpen: boolean;
  let setIsOpenCalls: boolean[];

  beforeEach(() => {
    isOpen = false;
    setIsOpenCalls = [];
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => { root.unmount(); });
    container.remove();
    vi.unstubAllGlobals();
  });

  const render = async () => {
    await act(async () => {
      root.render(
        <AssigneeSelector
          selectedIds={[]}
          options={OPTIONS}
          onChange={() => {}}
          isOpen={isOpen}
          setIsOpen={(open) => setIsOpenCalls.push(open)}
          onClose={() => {}}
          fullWidth
        />,
      );
    });
  };

  it('clicking the empty padding of the field opens it, not just the "+" icon', async () => {
    await render();
    const field = container.querySelector('[role="button"]') as HTMLElement;
    expect(field).not.toBeNull();

    // The "+" icon is a small child near the left edge; this click lands
    // well clear of it, on what is otherwise just the field's padding.
    field.getBoundingClientRect = () =>
      ({ left: 0, right: 400, top: 0, bottom: 46, width: 400, height: 46, x: 0, y: 0, toJSON() {} }) as DOMRect;
    await act(async () => {
      field.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: 300, clientY: 23 }));
    });

    expect(setIsOpenCalls).toEqual([true]);
  });

  it('reads "Add members..." when nothing is selected, so the empty field is not just an icon', async () => {
    await render();
    expect(container.textContent).toContain('Add members...');
  });

  it('opens below the field when there is ample room, even though the menu could theoretically be tall', async () => {
    isOpen = false;
    vi.stubGlobal('innerHeight', 900);
    vi.stubGlobal('innerWidth', 1200);
    await render();

    const field = container.querySelector('[role="button"]') as HTMLElement;
    field.getBoundingClientRect = () =>
      ({ left: 40, right: 440, top: 300, bottom: 346, width: 400, height: 46, x: 40, y: 300, toJSON() {} }) as DOMRect;

    await act(async () => {
      field.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });

    // Rerender with isOpen now true (the real component is driven by the
    // caller's own state, exactly as the drawer drives it).
    isOpen = true;
    await render();
    const menu = document.body.querySelector('[data-testid="menu"], .fixed.z-50.w-64') as HTMLElement | null;
    expect(menu).not.toBeNull();
    // Below the field's bottom (346), not above its top (300).
    expect(parseFloat(menu!.style.top)).toBeGreaterThanOrEqual(346);
  });

  it('flips above only when below is genuinely tight and above has more room', async () => {
    vi.stubGlobal('innerHeight', 500);
    vi.stubGlobal('innerWidth', 1200);
    isOpen = false;
    await render();

    const field = container.querySelector('[role="button"]') as HTMLElement;
    // 40px of room below (well under the 160px threshold), 420px above.
    field.getBoundingClientRect = () =>
      ({ left: 40, right: 440, top: 420, bottom: 460, width: 400, height: 40, x: 40, y: 420, toJSON() {} }) as DOMRect;

    await act(async () => {
      field.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });

    isOpen = true;
    await render();
    const menu = document.body.querySelector('.fixed.z-50.w-64') as HTMLElement | null;
    expect(menu).not.toBeNull();
    const top = parseFloat(menu!.style.top);
    // Above the field's top (420), and never off the top of the screen.
    expect(top).toBeLessThan(420);
    expect(top).toBeGreaterThanOrEqual(8);
  });
});
