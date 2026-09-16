/**
 * Ctrl-click on a sidebar entry opens that page in a new tab.
 *
 * The sidebar entries are buttons, not links, so nothing in the browser gives
 * them this for free — the shells have to read the modifier themselves. These
 * cases pin the two halves of that: which modifiers mean "new tab" (Ctrl, and
 * ⌘ for the same gesture on macOS — but never Shift or Alt, which mean other
 * things), and that the opened path is resolved on this origin.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { openPathInNewTab, opensInNewTab } from '../navigation';

const click = (modifiers: Partial<MouseEvent> = {}) =>
  ({ ctrlKey: false, metaKey: false, ...modifiers }) as MouseEvent;

describe('opensInNewTab', () => {
  it('is true for Ctrl-click and for ⌘-click', () => {
    expect(opensInNewTab(click({ ctrlKey: true }))).toBe(true);
    expect(opensInNewTab(click({ metaKey: true }))).toBe(true);
  });

  it('is false for a plain click', () => {
    expect(opensInNewTab(click())).toBe(false);
  });

  it('is false for Shift- and Alt-click, which are not this gesture', () => {
    expect(opensInNewTab(click({ shiftKey: true }))).toBe(false);
    expect(opensInNewTab(click({ altKey: true }))).toBe(false);
  });
});

describe('openPathInNewTab', () => {
  const originalWindow = (globalThis as { window?: unknown }).window;

  afterEach(() => {
    if (originalWindow === undefined) delete (globalThis as { window?: unknown }).window;
    else (globalThis as { window?: unknown }).window = originalWindow;
  });

  const stubWindow = (href: string) => {
    const open = vi.fn();
    (globalThis as { window?: unknown }).window = { open, location: { href } };
    return open;
  };

  it('opens the route on the current origin, in a new tab, without an opener', () => {
    const open = stubWindow('https://monitra.example/dashboard/reports/apps');

    openPathInNewTab('/admin/members');

    expect(open).toHaveBeenCalledWith(
      'https://monitra.example/admin/members',
      '_blank',
      'noopener,noreferrer'
    );
  });

  it('keeps the port and scheme the app is served from', () => {
    const open = stubWindow('http://localhost:5173/member/dashboard');

    openPathInNewTab('/member/reports/urls');

    expect(open).toHaveBeenCalledWith(
      'http://localhost:5173/member/reports/urls',
      '_blank',
      'noopener,noreferrer'
    );
  });
});
