// @vitest-environment jsdom
/**
 * Why the project drawer's Status and Team pickers must render into a
 * portal.
 *
 * `StatusPillDropdown` and `AssigneeSelector` (in `AdminProjectManagement.tsx`)
 * position their floating menu with `position: fixed` and coordinates read
 * from `getBoundingClientRect()` -- viewport coordinates. That is correct
 * everywhere the menu is used *except* inside the create/edit drawer, which
 * slides in with a CSS `transform` (Tailwind's `translate-x-...`). Per the
 * CSS Transforms spec, any ancestor with a `transform` other than `none`
 * becomes the containing block for a `position: fixed` descendant instead of
 * the viewport -- so the same coordinates that place the menu correctly in
 * the plain project table placed it somewhere inside (and often clipped by)
 * the drawer's own box when opened there. Clicking Status or the team "+"
 * inside the drawer did nothing visible.
 *
 * This is not really a project-management bug; it is a general fact about
 * `position: fixed` that would silently reappear if a future edit removed
 * the `createPortal(..., document.body)` wrapper as "unnecessary"
 * complexity. This test pins the fact itself: a plain fixed-position child
 * of a transformed ancestor is contained by that ancestor's box, and a
 * portaled one is not.
 */
import { act } from 'react';
import { createPortal } from 'react-dom';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

/** A `translate-x-0`-style ancestor, exactly like the drawer's own wrapper. */
const TransformedAncestor: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div data-testid="drawer" style={{ transform: 'translateX(0px)', position: 'relative' }}>
    {children}
  </div>
);

const PlainFixedChild: React.FC = () => (
  <div data-testid="menu" style={{ position: 'fixed', top: 0, left: 0 }}>
    menu
  </div>
);

const PortaledFixedChild: React.FC = () =>
  createPortal(
    <div data-testid="menu" style={{ position: 'fixed', top: 0, left: 0 }}>
      menu
    </div>,
    document.body,
  );

describe('a transformed ancestor traps a plain position:fixed child', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => { root.unmount(); });
    container.remove();
  });

  it('without a portal, the "fixed" menu is still a descendant of the transformed ancestor', async () => {
    await act(async () => {
      root.render(<TransformedAncestor><PlainFixedChild /></TransformedAncestor>);
    });
    const drawer = container.querySelector('[data-testid="drawer"]');
    const menu = document.querySelector('[data-testid="menu"]');
    // This is the bug, reproduced directly: DOM containment (and therefore
    // clipping by an ancestor's overflow) follows the transformed parent,
    // not the viewport -- whatever CSS `position: fixed` claims.
    expect(drawer?.contains(menu)).toBe(true);
  });

  it('with createPortal(..., document.body), the menu escapes the transformed ancestor entirely', async () => {
    await act(async () => {
      root.render(<TransformedAncestor><PortaledFixedChild /></TransformedAncestor>);
    });
    const drawer = container.querySelector('[data-testid="drawer"]');
    const menu = document.querySelector('[data-testid="menu"]');
    expect(menu).not.toBeNull();
    expect(drawer?.contains(menu)).toBe(false);
    expect(document.body.contains(menu)).toBe(true);
    // Not inside this test's own render container either -- truly a
    // sibling of the app root, same as the real drawer's menus now are.
    expect(container.contains(menu)).toBe(false);
  });
});
