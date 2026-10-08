// @vitest-environment jsdom
/**
 * Hovering a member's name on the Reports page shows their average activity.
 *
 * The number is the server's own per-member figure for exactly the filters on screen -- the
 * same duration-weighted average as the "Avg. Activity" tile -- fetched from
 * `/react/reports/members`, never rebuilt from the rows beneath. What is pinned:
 *
 * - the card appears on hover and goes on leave, and says the member's percentage;
 * - a member with nothing activity-sampled reads "not recorded", not "0%";
 * - the request carries the page's date range and its project / member filters, and
 *   changing a filter changes the numbers shown;
 * - while it loads, or if it fails, the card says so instead of showing a made-up figure,
 *   and the member list itself is unaffected;
 * - the card is drawn `fixed`, so the section's rounded, clipping edge cannot cut it off,
 *   and hovering never interferes with opening the member;
 * - screen readers get the figure too.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../../store/api/baseApi';
import { MemberBreakdownAccordion, type MemberItemBreakdown } from '../MemberBreakdownAccordion';
import { describeActivity, type MemberActivity } from '../memberActivity';

vi.mock('../V2Shell', () => ({
  V2Shell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

// The page's export dialog reads the signed-in user; it is closed here and only needs one to exist.
vi.mock('../../../auth/authContext', () => ({ useAuth: () => ({ currentUser: { id: 1, role: 'administrator' } }) }));

import { ReportPage } from '../ReportPage';

const flush = async () => {
  for (let i = 0; i < 6; i += 1) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }
};

const hover = async (el: Element | null | undefined) => {
  expect(el).toBeTruthy();
  await act(async () => { el!.dispatchEvent(new MouseEvent('mouseover', { bubbles: true })); });
};
const leave = async (el: Element | null | undefined) => {
  await act(async () => { el!.dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: document.body })); });
};

// ── The accordion, on its own ────────────────────────────────────────────────

const members: MemberItemBreakdown[] = [
  { member_id: 2, member_name: 'Alice Active', seconds: 600, dates: [{ date: '2026-10-01', seconds: 600, items: [{ name: 'V2', seconds: 600 }] }] },
  { member_id: 3, member_name: 'Bob Manual', seconds: 300, dates: [{ date: '2026-10-01', seconds: 300, items: [{ name: 'V2', seconds: 300 }] }] },
];

describe('describeActivity', () => {
  const ready = (byMember: Record<number, number | null>): MemberActivity => ({ isLoading: false, isError: false, byMember });

  it('says the percentage as the server gave it', () => {
    expect(describeActivity(ready({ 2: 48.33 }), 2)).toBe('Activity 48.33%');
    expect(describeActivity(ready({ 2: 0 }), 2)).toBe('Activity 0%');             // a real zero is a real zero
  });

  it('says "not recorded" for nothing sampled, never 0%', () => {
    expect(describeActivity(ready({ 3: null }), 3)).toBe('Activity not recorded');
    expect(describeActivity(ready({}), 9)).toBe('Activity not recorded');           // a member the answer did not include
  });

  it('says it is loading, and that it failed, rather than showing a number nobody measured', () => {
    expect(describeActivity({ isLoading: true, isError: false, byMember: {} }, 2)).toBe('Activity loading…');
    expect(describeActivity({ isLoading: false, isError: true, byMember: {} }, 2)).toBe('Activity unavailable');
    expect(describeActivity({ isLoading: false, isError: true, byMember: { 2: 50 } }, 2)).toBe('Activity unavailable');
  });

  it('keeps showing a figure it already has while it refreshes', () => {
    expect(describeActivity({ isLoading: true, isError: false, byMember: { 2: 50 } }, 2)).toBe('Activity 50%');
  });
});

describe('MemberBreakdownAccordion: the activity card', () => {
  let container: HTMLDivElement;
  let root: Root;

  const render = async (activity?: MemberActivity) => {
    await act(async () => {
      root.render(
        <MemberBreakdownAccordion
          members={members} isLoading={false} isTruncated={false} emptyLabel="none"
          itemLabel="Project" accentColor="#2563EB" activity={activity}
        />,
      );
    });
  };
  const nameOf = (text: string) =>
    Array.from(container.querySelectorAll('[data-testid="member-name"]')).find((el) => el.textContent?.includes(text));
  const card = () => container.querySelector('[role="tooltip"]');

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

  it('shows nothing until a name is hovered, then the member\'s activity, then nothing again', async () => {
    await render({ isLoading: false, isError: false, byMember: { 2: 48.33, 3: null } });
    expect(card()).toBeNull();

    await hover(nameOf('Alice Active'));
    expect(card()!.textContent).toContain('Alice Active');
    expect(card()!.textContent).toContain('Activity 48.33%');

    await leave(nameOf('Alice Active'));
    expect(card()).toBeNull();
  });

  it('shows each member their own figure, and "not recorded" for one nothing was sampled for', async () => {
    await render({ isLoading: false, isError: false, byMember: { 2: 48.33, 3: null } });

    await hover(nameOf('Bob Manual'));
    expect(card()!.textContent).toContain('Activity not recorded');
    expect(card()!.textContent).not.toContain('%');
    await leave(nameOf('Bob Manual'));

    await hover(nameOf('Alice Active'));
    expect(card()!.textContent).toContain('48.33%');
  });

  it('draws the card fixed, outside the section\'s clipping edge', async () => {
    await render({ isLoading: false, isError: false, byMember: { 2: 50 } });
    await hover(nameOf('Alice Active'));

    const positioned = card()!.parentElement!;
    expect(positioned.className).toContain('fixed');
    expect(positioned.className).toContain('pointer-events-none');                 // it never gets in the way of the pointer
  });

  it('gives a screen reader the same figure, beside the name', async () => {
    await render({ isLoading: false, isError: false, byMember: { 2: 48.33 } });

    const hidden = nameOf('Alice Active')!.querySelector('.sr-only');
    expect(hidden!.textContent).toBe(', activity 48.33%');
  });

  it('hovering a name does not stop a click on it opening the member', async () => {
    await render({ isLoading: false, isError: false, byMember: { 2: 50 } });
    const toggle = nameOf('Alice Active')!.closest('button')!;
    expect(toggle.getAttribute('aria-expanded')).toBe('false');

    await hover(nameOf('Alice Active'));
    await act(async () => { toggle.dispatchEvent(new MouseEvent('click', { bubbles: true })); });

    expect(toggle.getAttribute('aria-expanded')).toBe('true');
  });

  const rowOf = (text: string) => nameOf(text)!.closest('button')!;

  it("shows the card when the pointer is anywhere on the member's row, not only on the name", async () => {
    await render({ isLoading: false, isError: false, byMember: { 2: 48.33, 3: null } });
    const row = rowOf('Alice Active');
    const avatar = row.querySelector('span.rounded-full')!;
    const hours = Array.from(row.querySelectorAll('span')).find((el) => el.textContent === '00:10:00')!;

    for (const part of [row, avatar, hours]) {
      await hover(part);
      expect(card()!.textContent).toContain('Activity 48.33%');
      await leave(row);
      expect(card()).toBeNull();
    }
  });

  it('puts the card on the name even when the row is what is hovered', async () => {
    await render({ isLoading: false, isError: false, byMember: { 2: 48.33 } });
    const name = nameOf('Alice Active')!;
    name.getBoundingClientRect = () => ({ left: 100, top: 400, width: 80, height: 20, right: 180, bottom: 420, x: 100, y: 400, toJSON: () => ({}) });

    await hover(rowOf('Alice Active'));

    const positioned = card()!.parentElement as HTMLElement;
    expect(positioned.style.left).toBe('140px');                                   // the name's centre, not the row's
    expect(positioned.style.top).toBe('394px');                                    // just above the name
  });

  it("shows the right member's figure when moving from one row to the next", async () => {
    await render({ isLoading: false, isError: false, byMember: { 2: 48.33, 3: null } });

    await hover(rowOf('Alice Active'));
    await leave(rowOf('Alice Active'));
    await hover(rowOf('Bob Manual'));

    expect(card()!.textContent).toContain('Bob Manual');
    expect(card()!.textContent).toContain('Activity not recorded');
    expect(container.querySelectorAll('[role="tooltip"]').length).toBe(1);
  });

  it('without the activity prop there is no card and nothing extra for a screen reader', async () => {
    await render(undefined);

    await hover(nameOf('Alice Active'));

    expect(card()).toBeNull();
    expect(container.querySelector('.sr-only')).toBeNull();
  });
});

// ── The whole page, against a fake backend ───────────────────────────────────

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const member = (id: number, name: string) => ({ id, name, email: `${name.split(' ')[0].toLowerCase()}@example.com`, role: 'employee', status: 'active' });
const project = (id: number, name: string) => ({ id, project_name: name, organization_id: 1, tasks: [] });
const listPage = (items: unknown[]) => ({ items, page: 1, limit: 200, total: items.length, pages: 1, total_seconds: 900, total_hours: 0.25 });
const log = (id: string, memberId: number, name: string, seconds: number) => ({
  id, date: '2026-10-01', member_id: memberId, member_name: name, role: 'employee',
  project_id: 11, project_name: 'Alpha Project', task_id: 1, task_name: 'Build', app: null, url: null,
  tracked_seconds: seconds, tracked_hours: seconds / 3600, tracked_time: '', activity_percentage: null,
});

describe('Reports page: hovering a member\'s name', () => {
  let container: HTMLDivElement;
  let root: Root;
  let memberRequests: URLSearchParams[];
  let membersAnswer: () => Response;

  const click = async (el: Element | undefined) => {
    expect(el).toBeTruthy();
    await act(async () => { el!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    await flush();
  };
  const buttonByText = (text: string) =>
    Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.includes(text));
  const memberOption = (name: string) =>
    Array.from(container.querySelectorAll('.custom-scrollbar button')).find((b) => b.textContent?.includes(name));
  const projectOption = (name: string) =>
    Array.from(container.querySelectorAll('li button')).find((b) => b.textContent?.includes(name));
  const nameOf = (text: string) =>
    Array.from(container.querySelectorAll('[data-testid="member-name"]')).find((el) => el.textContent?.includes(text));
  const card = () => container.querySelector('[role="tooltip"]');

  const everyoneAnswer = () => json(listPage([
    { member_id: 2, member_name: 'Alice Active', total_seconds: 600, total_hours: 0.17, avg_activity: 48.33, total_members: 1, total_tasks: 1 },
    { member_id: 3, member_name: 'Bob Manual', total_seconds: 300, total_hours: 0.08, avg_activity: null, total_members: 1, total_tasks: 1 },
  ]));

  const mount = async () => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(
        <Provider store={store}>
          <MemoryRouter initialEntries={['/dashboard/reports/projects?start=2026-10-01&end=2026-10-07']}>
            <Routes><Route path="/dashboard/reports/:reportId" element={<ReportPage />} /></Routes>
          </MemoryRouter>
        </Provider>,
      );
    });
    await flush();
  };

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    window.localStorage.clear();
    memberRequests = [];
    membersAnswer = everyoneAnswer;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      const path = url.pathname;
      if (path.endsWith('/react/reports/members')) {
        memberRequests.push(url.searchParams);
        return membersAnswer();
      }
      if (path.endsWith('/react/reports/summary')) return json({ total_hours: 0.25, total_seconds: 900, avg_activity: 26.36, total_members: 2, total_tasks: 1 });
      if (path.endsWith('/react/reports/trend')) return json({ start_date: '2026-10-01', end_date: '2026-10-07', points: [] });
      if (path.includes('/react/reports/')) return json(listPage([]));
      if (path.endsWith('/reports/detailed-logs')) {
        const picked = url.searchParams.getAll('member_id');
        const rows = [log('a', 2, 'Alice Active', 600), log('b', 3, 'Bob Manual', 300)]
          .filter((row) => !picked.length || picked.includes(String(row.member_id)));
        return json({ start_date: '2026-10-01', end_date: '2026-10-07', items: rows, pagination: { page: 1, limit: 200, total: rows.length, total_pages: 1 } });
      }
      if (path.endsWith('/members')) {
        return json({ items: [member(2, 'Alice Active'), member(3, 'Bob Manual')], page: 1, limit: 100, total: 2, pages: 1 });
      }
      if (path.endsWith('/projects')) {
        return json({ items: [project(11, 'Alpha Project'), project(12, 'Beta Project')], pagination: { page: 1, limit: 100, total: 2, total_pages: 1 } });
      }
      return json({}, 404);
    }));
    await mount();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('asks the server for each member\'s activity, for the page\'s own range and no narrowing by default', () => {
    expect(memberRequests.length).toBeGreaterThan(0);
    const query = memberRequests[0];
    expect(query.get('start_date')).toBe('2026-10-01');
    expect(query.get('end_date')).toBe('2026-10-07');
    expect(query.get('limit')).toBe('200');
    expect(query.has('member_id')).toBe(false);
    expect(query.has('project_id')).toBe(false);
  });

  it('shows the server\'s figure for a member when their name is hovered, and not recorded for the other', async () => {
    await hover(nameOf('Alice Active'));
    expect(card()!.textContent).toContain('Activity 48.33%');
    await leave(nameOf('Alice Active'));

    await hover(nameOf('Bob Manual'));
    expect(card()!.textContent).toContain('Activity not recorded');
  });

  it('keeps the member list and hours as they were: this adds a hover, not a column', () => {
    expect(container.textContent).toContain('Alice Active');
    expect(container.textContent).toContain('Bob Manual');
    expect(container.textContent).toContain('00:10:00');                           // Alice's 600 s
    // Nothing visible is printed until a name is hovered (the screen-reader text is deliberately in the DOM).
    const visible = container.cloneNode(true) as HTMLElement;
    visible.querySelectorAll('.sr-only').forEach((el) => el.remove());
    expect(visible.textContent).not.toContain('48.33%');
    expect(card()).toBeNull();
  });

  it('sends the project filter, and shows the new figure it brings back', async () => {
    membersAnswer = () => json(listPage([
      { member_id: 2, member_name: 'Alice Active', total_seconds: 600, total_hours: 0.17, avg_activity: 12.5, total_members: 1, total_tasks: 1 },
    ]));
    memberRequests = [];

    await click(buttonByText('All projects'));
    await click(projectOption('Beta Project'));

    expect(memberRequests.length).toBeGreaterThan(0);
    expect(memberRequests[memberRequests.length - 1].getAll('project_id')).toEqual(['12']);
    await hover(nameOf('Alice Active'));
    expect(card()!.textContent).toContain('Activity 12.5%');
  });

  it('sends the member filter too', async () => {
    memberRequests = [];

    await click(buttonByText('All members'));
    await click(memberOption('Alice Active'));

    expect(memberRequests[memberRequests.length - 1].getAll('member_id')).toEqual(['2']);
  });

  it('says it is unavailable if the server cannot answer, and the members are still listed', async () => {
    await act(async () => root.unmount());
    container.remove();
    membersAnswer = () => json({ detail: 'boom' }, 500);
    await mount();

    expect(nameOf('Alice Active')).toBeTruthy();
    await hover(nameOf('Alice Active'));
    expect(card()!.textContent).toContain('Activity unavailable');
    expect(card()!.textContent).not.toContain('%');
  });
});
