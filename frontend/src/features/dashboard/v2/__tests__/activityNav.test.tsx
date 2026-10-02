// @vitest-environment jsdom
/**
 * The sidebar's Activity group.
 *
 * Active Users and Active Task List are one expandable entry, like Reports:
 * collapsed until opened, open on arrival at either page, each child gated as its
 * page is (Active Task List needs `projects:create`, Active Users is open to
 * everyone the shell is shown to), and neither is also offered at the top level.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

let currentUser: Record<string, unknown> | null = null;
vi.mock('../../../auth/authContext', () => ({
  useAuth: () => ({ currentUser, logout: vi.fn() }),
}));

import { V2Shell } from '../V2Shell';

const Where: React.FC = () => <div data-testid="where">{useLocation().pathname}</div>;

describe('sidebar Activity group', () => {
  let container: HTMLDivElement;
  let root: Root;

  const mount = async (path: string) => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={[path]}>
          <V2Shell title="Page">
            <Where />
          </V2Shell>
        </MemoryRouter>,
      );
    });
  };
  const sidebar = () => container.querySelector('aside') as HTMLElement;
  const button = (text: string) =>
    Array.from(sidebar().querySelectorAll('button')).find((b) => b.textContent?.trim() === text) as HTMLButtonElement | undefined;
  const click = async (el: Element | null | undefined) => {
    expect(el).toBeTruthy();
    await act(async () => { el!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  };
  const where = () => container.querySelector('[data-testid="where"]')!.textContent;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    currentUser = {
      id: 1,
      name: 'Admin',
      role_name: 'administrator',
      permissions: { 'projects:create': true, view_employees: true, 'time_entries:view_all': true },
    };
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it('is one collapsed Activity entry, with neither page offered at the top level', async () => {
    await mount('/dashboard');
    expect(button('Activity')).toBeTruthy();
    expect(button('Activity')!.getAttribute('aria-expanded')).toBe('false');
    expect(button('Active Users')).toBeUndefined();
    expect(button('Active Task List')).toBeUndefined();
  });

  it('opens to show Active Users and Active Task List, and closes again', async () => {
    await mount('/dashboard');
    await click(button('Activity'));
    expect(button('Activity')!.getAttribute('aria-expanded')).toBe('true');
    expect(button('Active Users')).toBeTruthy();
    expect(button('Active Task List')).toBeTruthy();

    await click(button('Activity'));
    expect(button('Active Users')).toBeUndefined();
  });

  it('navigates to each page from its entry', async () => {
    await mount('/dashboard');
    await click(button('Activity'));
    await click(button('Active Users'));
    expect(where()).toBe('/admin/active-users');
    await click(button('Active Task List'));
    expect(where()).toBe('/admin/task-listing');
  });

  it('starts open, with the current page marked, when you arrive on either page', async () => {
    await mount('/admin/active-users');
    expect(button('Activity')!.getAttribute('aria-expanded')).toBe('true');
    expect(button('Active Users')!.className).toContain('bg-[#2563EB]/15');
    expect(button('Active Task List')!.className).not.toContain('bg-[#2563EB]/15');

    await act(async () => root.unmount());
    root = createRoot(container);
    await mount('/admin/task-listing');
    expect(button('Activity')!.getAttribute('aria-expanded')).toBe('true');
    expect(button('Active Task List')!.className).toContain('bg-[#2563EB]/15');
  });

  it('offers only Active Users to someone who cannot manage projects', async () => {
    currentUser = {
      id: 2,
      name: 'HR',
      role_name: 'hr',
      permissions: { view_employees: true, 'time_entries:view_all': true },
    };
    await mount('/dashboard');
    await click(button('Activity'));
    expect(button('Active Users')).toBeTruthy();
    expect(button('Active Task List')).toBeUndefined();
  });
});
