// @vitest-environment jsdom
/**
 * The Logs page and the wording behind it.
 *
 * The page renders the real screen against a real RTK Query store with only
 * `fetch` stubbed, so what is pinned is what a browser would do:
 *
 * - the trail is shown employee-wise, one collapsed accordion per person,
 *   opening onto that person's actions split by IST day;
 * - the dates, the category and the search are sent to the server (the trail
 *   is too dense to filter in the browser) and an empty answer is shown as
 *   empty -- never filled in;
 * - a cut answer says it was cut.
 *
 * The pure helpers are tested directly: what each action is called, which IST
 * day an instant belongs to, and that a label the page has never heard of is
 * shown in its own words rather than dropped.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import { formatISTDate } from '../../../utils/duration';
import type { ActivityLogEntry, ActivityLogMemberGroup } from '../../../store/api/activityLogsApi';
import {
  actionLabel,
  contextLabel,
  entryActionLabel,
  filterMembers,
  groupEntriesByDay,
  istDayOf,
  logsToCsvRows,
  moduleLabel,
  sourceLabel,
} from '../activityLogFormat';

let currentUser: { id: number; role_name: string; name: string } = { id: 1, role_name: 'administrator', name: 'Admin' };

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ title, subtitle, actions, children }: {
    title: string; subtitle?: string; actions?: React.ReactNode; children: React.ReactNode;
  }) => (
    <>
      <h1>{title}</h1>
      <p data-testid="subtitle">{subtitle}</p>
      {actions}
      {children}
    </>
  ),
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser }),
}));

import { AdminActivityLogs } from '../AdminActivityLogs';

const entry = (overrides: Partial<ActivityLogEntry>): ActivityLogEntry => ({
  id: 1,
  module: 'auth',
  action: 'login',
  description: 'Signed in',
  source: 'desktop',
  client_version: '1.3.0',
  project_id: null,
  project_name: null,
  task_id: null,
  task_name: null,
  entity_id: null,
  ip_address: '203.0.113.9',
  created_at: '2026-09-30T04:00:00Z', // 09:30 IST on the 30th
  ...overrides,
});

const ALICE: ActivityLogMemberGroup = {
  user_id: 11,
  name: 'Alice Example',
  email: 'alice@example.invalid',
  designation: 'Engineer',
  role_name: 'employee',
  entry_count: 3,
  last_activity_at: '2026-09-30T06:30:00Z',
  entries: [
    entry({ id: 3, module: 'timer', action: 'timer_started', description: 'Started the timer on Apollo › Guidance',
            project_id: 40, project_name: 'Apollo', task_id: 400, task_name: 'Guidance',
            created_at: '2026-09-30T06:30:00Z' }),
    entry({ id: 2, created_at: '2026-09-30T04:00:00Z' }),
    // 20:00 UTC on the 28th is 01:30 IST on the 29th.
    entry({ id: 1, module: 'desktop', action: 'app_closed', description: 'Closed the Monitra desktop application',
            created_at: '2026-09-28T20:00:00Z' }),
  ],
};

const BOB: ActivityLogMemberGroup = {
  user_id: 12,
  name: 'Bob Example',
  email: 'bob@example.invalid',
  designation: null,
  role_name: 'employee',
  entry_count: 1,
  last_activity_at: '2026-09-29T05:00:00Z',
  entries: [entry({ id: 9, source: 'web', client_version: null, created_at: '2026-09-29T05:00:00Z' })],
};

// ── Wording and grouping ───────────────────────────────────────────────────

describe('activity log wording', () => {
  it('names each recorded action the way a person would', () => {
    expect(actionLabel('login')).toBe('Signed in');
    expect(actionLabel('app_closed')).toBe('Closed the app');
    expect(actionLabel('timer_started')).toBe('Started timer');
    expect(actionLabel('manual_time_requested')).toBe('Requested manual time');
    expect(actionLabel('task_created')).toBe('Added task');
    expect(actionLabel('login_excluded')).toBe('Excluded from signing in');
  });

  it('shows an action it has never heard of in its own words, not hidden', () => {
    // A newer backend may start writing an action before this build ships a
    // label for it. The row must still say what happened.
    expect(actionLabel('screenshot_deleted')).toBe('Screenshot deleted');
    expect(moduleLabel('screenshot')).toBe('Screenshot');
  });

  it('says which client acted, and nothing when none was recorded', () => {
    expect(sourceLabel({ source: 'desktop', client_version: '1.3.0' })).toBe('Desktop 1.3.0');
    expect(sourceLabel({ source: 'desktop', client_version: null })).toBe('Desktop');
    expect(sourceLabel({ source: 'web', client_version: null })).toBe('Web');
    expect(sourceLabel({ source: null, client_version: null })).toBeNull();
  });

  it('says when a sign-in or sign-out was made from the desktop application', () => {
    expect(entryActionLabel({ action: 'login', source: 'desktop' })).toBe('Signed in to the desktop app');
    expect(entryActionLabel({ action: 'logout', source: 'desktop' })).toBe('Signed out of the desktop app');
    expect(entryActionLabel({ action: 'login', source: 'web' })).toBe('Signed in');
    expect(entryActionLabel({ action: 'timer_started', source: 'desktop' })).toBe('Started timer');
  });

  it('joins the project and task an action concerned', () => {
    expect(contextLabel({ project_name: 'Apollo', task_name: 'Guidance' })).toBe('Apollo › Guidance');
    expect(contextLabel({ project_name: 'Apollo', task_name: null })).toBe('Apollo');
    expect(contextLabel({ project_name: null, task_name: null })).toBeNull();
  });
});

describe('activity log grouping', () => {
  it('puts an instant on its IST day, not its UTC day', () => {
    // 20:00 UTC is 01:30 the next morning in IST. Grouped by UTC day this
    // sign-in would sit under the previous day's heading.
    expect(istDayOf('2026-09-28T20:00:00Z')).toBe('2026-09-29');
    expect(istDayOf('2026-09-30T04:00:00Z')).toBe('2026-09-30');
    expect(istDayOf('not a date')).toBe('');
  });

  it('splits one person into days, newest first, without reordering rows', () => {
    const days = groupEntriesByDay(ALICE.entries);
    expect(days.map((group) => group.day)).toEqual(['2026-09-30', '2026-09-29']);
    expect(days[0].entries.map((row) => row.id)).toEqual([3, 2]);
    expect(days[1].entries.map((row) => row.id)).toEqual([1]);
  });

  it('narrows to the chosen employees and shows everyone when none is chosen', () => {
    expect(filterMembers([ALICE, BOB], [])).toEqual([ALICE, BOB]);
    expect(filterMembers([ALICE, BOB], ['12'])).toEqual([BOB]);
    expect(filterMembers([ALICE, BOB], ['999'])).toEqual([]);
  });

  it('exports one row per action with the employee on every row', () => {
    const rows = logsToCsvRows([ALICE, BOB]);
    expect(rows).toHaveLength(4);
    expect(rows[0].slice(0, 2)).toEqual(['Alice Example', 'alice@example.invalid']);
    expect(rows[0]).toContain('Started timer');
    expect(rows[0]).toContain('Apollo');
    expect(rows[3]).toContain('Web');
  });
});

// ── The screen ─────────────────────────────────────────────────────────────

describe('AdminActivityLogs', () => {
  let container: HTMLDivElement;
  let root: Root;
  let requests: string[];
  let reply: { status: number; body: unknown };

  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

  const payload = (members: ActivityLogMemberGroup[], truncated = false) => ({
    start_date: '2026-09-24',
    end_date: '2026-09-30',
    total: members.reduce((sum, member) => sum + member.entries.length, 0),
    truncated,
    modules: ['auth', 'desktop', 'timer', 'manual_time', 'project', 'task', 'member', 'feedback', 'system'],
    members,
  });

  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    }
  };

  const render = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(
        <Provider store={store}>
          <AdminActivityLogs />
        </Provider>,
      );
    });
    await settle();
  };

  const accordions = () => Array.from(container.querySelectorAll('section > button[aria-expanded]')) as HTMLButtonElement[];
  const click = async (element: Element) => {
    await act(async () => {
      element.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await settle();
  };
  const buttonNamed = (name: string) =>
    Array.from(container.querySelectorAll('button')).find((button) => button.textContent?.trim() === name);

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    currentUser = { id: 1, role_name: 'administrator', name: 'Admin' };
    requests = [];
    reply = { status: 200, body: payload([ALICE, BOB]) };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      requests.push(url);
      if (url.includes('/activity-logs')) return json(reply.body, reply.status);
      if (url.includes('/members')) return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
      return json({}, 404);
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('shows one collapsed accordion per employee, with their action count', async () => {
    await render();
    const sections = accordions();
    expect(sections).toHaveLength(2);
    expect(sections.every((button) => button.getAttribute('aria-expanded') === 'false')).toBe(true);
    expect(sections[0].textContent).toContain('Alice Example');
    expect(sections[0].textContent).toContain('3 actions');
    expect(sections[1].textContent).toContain('Bob Example');
    expect(sections[1].textContent).toContain('1 action');
    expect(container.textContent).toContain('4 actions by 2 employees');
    // Closed sections render none of their rows.
    expect(container.textContent).not.toContain('Started the timer on Apollo');
  });

  it('opens an employee onto their actions, split by IST day', async () => {
    await render();
    await click(accordions()[0]);
    expect(accordions()[0].getAttribute('aria-expanded')).toBe('true');
    const text = container.textContent ?? '';
    expect(text).toContain('Started the timer on Apollo › Guidance');
    expect(text).toContain('Started timer');
    expect(text).toContain('Desktop 1.3.0');
    // Day headings come from the app's own IST formatter, so the assertion
    // does not depend on how this runtime abbreviates a month.
    const heading = (day: string) => formatISTDate(`${day}T12:00:00Z`);
    expect(text).toContain(heading('2026-09-30'));
    // The 20:00 UTC close on the 28th is shown under the 29th, its IST day.
    expect(text).toContain(heading('2026-09-29'));
    expect(text).not.toContain(heading('2026-09-28'));
    // Bob stayed closed.
    expect(accordions()[1].getAttribute('aria-expanded')).toBe('false');
  });

  it('expands and collapses everyone at once', async () => {
    await render();
    await click(buttonNamed('Expand all')!);
    expect(accordions().every((button) => button.getAttribute('aria-expanded') === 'true')).toBe(true);
    await click(buttonNamed('Collapse all')!);
    expect(accordions().every((button) => button.getAttribute('aria-expanded') === 'false')).toBe(true);
  });

  it('opens a lone employee without being asked', async () => {
    reply = { status: 200, body: payload([BOB]) };
    await render();
    expect(accordions()).toHaveLength(1);
    expect(accordions()[0].getAttribute('aria-expanded')).toBe('true');
  });

  it('asks the server for the last seven days, and for a category when one is chosen', async () => {
    await render();
    const first = requests.find((url) => url.includes('/activity-logs'))!;
    expect(first).toMatch(/start=\d{4}-\d{2}-\d{2}&end=\d{4}-\d{2}-\d{2}/);
    expect(first).not.toContain('module=');

    await click(container.querySelector('button[aria-label="Filter by category"]')!);
    const timerOption = Array.from(container.querySelectorAll('[role="option"]'))
      .find((option) => option.textContent?.trim() === 'Timer')!;
    await click(timerOption);
    expect(requests.some((url) => url.includes('/activity-logs') && url.includes('module=timer'))).toBe(true);
  });

  it('shows an honest empty state when nothing was recorded', async () => {
    reply = { status: 200, body: payload([]) };
    await render();
    expect(accordions()).toHaveLength(0);
    expect(container.textContent).toContain('No activity recorded in the last 7 days.');
    expect(buttonNamed('Export CSV')!.disabled).toBe(true);
  });

  it('says so when the answer was cut', async () => {
    reply = { status: 200, body: payload([ALICE, BOB], true) };
    await render();
    expect(container.textContent).toContain('only the most recent');
  });

  it('reports a failed load as a failure, not as an empty trail', async () => {
    reply = { status: 500, body: { detail: 'boom' } };
    await render();
    expect(container.textContent).toContain('The logs could not be loaded.');
    expect(container.textContent).not.toContain('No activity recorded');
  });

  it('tells a leader the page is their team', async () => {
    currentUser = { id: 3, role_name: 'leader', name: 'Linus' };
    await render();
    expect(container.querySelector('[data-testid="subtitle"]')!.textContent).toContain('your team');
  });
});
