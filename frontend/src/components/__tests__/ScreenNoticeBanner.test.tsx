// @vitest-environment jsdom
/**
 * The red note above every signed-in screen.
 *
 * It lives in the three shells -- admin/dashboard, member and client -- so a
 * page cannot show it by remembering to, or miss it by forgetting. Each shell
 * is rendered here and must carry the note as the first thing in its main
 * column, above the page header, in red.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../features/auth/authContext', () => ({
  useAuth: () => ({
    currentUser: { id: 1, name: 'Test User', email: 't@example.com', role_name: 'employee', permissions: {} },
    logout: vi.fn(),
  }),
}));
vi.mock('../../store/api/clientPortalApi', () => ({
  useGetMyProfileQuery: () => ({ data: undefined }),
}));

import { V2Shell } from '../../features/dashboard/v2/V2Shell';
import { MemberShell } from '../../features/member/MemberShell';
import { ClientShell } from '../../features/client/ClientShell';
import { SCREEN_NOTICE, ScreenNoticeBanner } from '../ScreenNoticeBanner';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe('screen notice banner', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  const render = async (node: React.ReactNode) => {
    await act(async () => {
      root.render(<MemoryRouter>{node}</MemoryRouter>);
    });
  };

  const notices = () => container.querySelectorAll('[data-testid="screen-notice"]');

  it('says that screen enhancement is in progress, in red', async () => {
    await render(<ScreenNoticeBanner />);
    const banner = container.querySelector('[data-testid="screen-notice"]') as HTMLElement;
    expect(banner.textContent).toBe('Note: Screen enhancement is currently in progress.');
    expect(SCREEN_NOTICE).toBe(banner.textContent);
    expect(banner.className).toContain('text-red-600');
  });

  const shells: Array<[string, () => React.ReactNode]> = [
    ['V2Shell (admin and org-wide screens)', () => <V2Shell title="Page">content</V2Shell>],
    ['MemberShell', () => <MemberShell title="Page">content</MemberShell>],
    ['ClientShell', () => <ClientShell title="Page">content</ClientShell>],
  ];

  it.each(shells)('%s shows the note once, above the page header', async (_name, shell) => {
    await render(shell());
    expect(notices()).toHaveLength(1);

    const banner = notices()[0];
    const header = container.querySelector('header') as HTMLElement;
    expect(header).toBeTruthy();
    expect(banner.nextElementSibling).toBe(header);
    expect(banner.parentElement).toBe(header.parentElement);
  });
});
