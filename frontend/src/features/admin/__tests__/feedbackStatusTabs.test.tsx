// @vitest-environment jsdom
/**
 * The Feedback page's tabs: no "My feedback" for an administrator, and a status
 * strip -- All / New / Working / Resolved -- that narrows the list.
 *
 * The filter logic is tested directly; the page is then rendered for real (real
 * `AdminFeedback`, real RTK Query store, only `fetch` and the shell/auth
 * replaced) so that what is asserted is what an administrator, and an HR user,
 * would actually see.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { Feedback } from '../../../store/api/feedbackApi';

const auth = vi.hoisted(() => ({ role: 'administrator', id: 1 }));

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({
    currentUser: {
      id: auth.id, organization_id: 1, username: 'me', email: 'me@example.invalid', name: 'Me',
      role_name: auth.role, permissions: {}, is_active: true,
    },
  }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminFeedback } from '../AdminFeedback';
import {
  ScopeTabs,
  StatusTabs,
  filterFeedback,
  type FeedbackFilterState,
} from '../../feedback/feedbackFilters';

const row = (id: number, status: Feedback['status'], employeeId = 7, extra: Partial<Feedback> = {}): Feedback => ({
  id, employee_id: employeeId, employee_name: `Employee ${employeeId}`, category: 'report_a_problem',
  message: `Message number ${id}`, status, created_at: '2026-10-01T05:00:00+00:00',
  updated_at: null, ...extra,
});

const RANGE = { preset: 'custom', from: '2026-09-01', to: '2026-12-31' } as FeedbackFilterState['range'];
const base: FeedbackFilterState = {
  search: '', category: null, range: RANGE, scope: 'all', selectedMembers: [],
};

const ITEMS = [
  row(1, 'new', 7),
  row(2, 'in_progress', 8),
  row(3, 'resolved', 7),
  row(4, 'resolved', 1), // the signed-in user's own
  row(5, 'new', 1),      // the signed-in user's own
];

describe('filterFeedback by status', () => {
  const ids = (state: Partial<FeedbackFilterState>, items = ITEMS, userId: number | null = null) =>
    filterFeedback(items, { ...base, ...state }, userId).map((item) => item.id);

  it('shows everything when no status is chosen, or "all"', () => {
    expect(ids({})).toEqual([1, 2, 3, 4, 5]);
    expect(ids({ status: 'all' })).toEqual([1, 2, 3, 4, 5]);
  });

  it('narrows to New, Working and Resolved', () => {
    expect(ids({ status: 'new' })).toEqual([1, 5]);
    expect(ids({ status: 'in_progress' })).toEqual([2]);
    expect(ids({ status: 'resolved' })).toEqual([3, 4]);
  });

  it('combines with whose feedback it is and with the other filters', () => {
    expect(ids({ status: 'resolved', scope: 'employees' }, ITEMS, 1)).toEqual([3]);
    expect(ids({ status: 'resolved', scope: 'mine' }, ITEMS, 1)).toEqual([4]);
    expect(ids({ status: 'new', selectedMembers: ['7'] })).toEqual([1]);
    expect(ids({ status: 'new', search: 'number 5' })).toEqual([5]);
  });

  it('leaves states with no tab (reviewing, closed) to "All"', () => {
    const items = [row(1, 'new'), row(2, 'reviewing'), row(3, 'closed')];
    expect(ids({ status: 'all' }, items)).toEqual([1, 2, 3]);
    expect(ids({ status: 'new' }, items)).toEqual([1]);
  });
});

describe('the tab strips', () => {
  let container: HTMLDivElement;
  let root: Root;
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
  const tabs = (name: string) =>
    Array.from(container.querySelectorAll(`[role="tablist"][aria-label="${name}"] [role="tab"]`)) as HTMLButtonElement[];

  it('offers all three scopes by default and only the ones asked for otherwise', async () => {
    const counts = { all: 5, employees: 3, mine: 2 };
    await act(async () => {
      root.render(<ScopeTabs value="all" onChange={() => {}} employeesLabel="Employees" counts={counts} />);
    });
    expect(tabs('Whose feedback').map((t) => t.textContent)).toEqual(['All5', 'Employees3', 'My feedback2']);

    await act(async () => {
      root.render(
        <ScopeTabs value="all" onChange={() => {}} employeesLabel="Employees" counts={counts} scopes={['all', 'employees']} />,
      );
    });
    expect(tabs('Whose feedback').map((t) => t.textContent)).toEqual(['All5', 'Employees3']);
  });

  it('shows All, New, Working and Resolved with their counts, and reports a click', async () => {
    const onChange = vi.fn();
    await act(async () => {
      root.render(
        <StatusTabs value="new" onChange={onChange} counts={{ all: 5, new: 2, in_progress: 1, resolved: 2 }} />,
      );
    });
    const strip = tabs('Feedback status');
    expect(strip.map((t) => t.textContent)).toEqual(['All5', 'New2', 'Working1', 'Resolved2']);
    expect(strip.map((t) => t.getAttribute('aria-selected'))).toEqual(['false', 'true', 'false', 'false']);

    await act(async () => { strip[3].click(); });
    expect(onChange).toHaveBeenCalledWith('resolved');
  });
});

describe('the Feedback page', () => {
  let container: HTMLDivElement;
  let root: Root;

  const json = (body: unknown) =>
    new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
  const urlOf = (input: RequestInfo | URL) =>
    typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
  const settle = async () => {
    for (let i = 0; i < 12; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  const render = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(<Provider store={store}><AdminFeedback /></Provider>);
    });
    await settle();
  };

  const tabs = (name: string) =>
    Array.from(container.querySelectorAll(`[role="tablist"][aria-label="${name}"] [role="tab"]`)) as HTMLButtonElement[];
  const labels = (name: string) => tabs(name).map((t) => t.textContent);
  const tab = (name: string, label: string) =>
    tabs(name).find((t) => t.textContent?.startsWith(label)) as HTMLButtonElement;
  const click = async (button: HTMLButtonElement) => { await act(async () => { button.click(); }); await settle(); };
  const shownIds = () =>
    Array.from(container.querySelectorAll('tbody tr')).map((tr) =>
      // The description cell alone: the cells either side would run into the number.
      /Message number (\d+)/.exec(tr.querySelector('.line-clamp-2')?.textContent ?? '')?.[1],
    );

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.setItem('accessToken', 'tok-123');
    auth.role = 'administrator';
    auth.id = 1;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(urlOf(input), 'http://localhost').pathname;
      if (path.endsWith('/feedback')) {
        return json({ items: ITEMS, page: 1, limit: 100, total: ITEMS.length, pages: 1 });
      }
      if (path.endsWith('/members')) return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
      return new Response('', { status: 404 });
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    localStorage.removeItem('accessToken');
  });

  it('gives an administrator All and Employees, with no "My feedback" tab', async () => {
    await render();

    expect(labels('Whose feedback')).toEqual(['All5', 'Employees3']);
    expect(container.textContent).not.toContain('My feedback');
  });

  it('still gives HR the "My feedback" tab', async () => {
    auth.role = 'hr';
    await render();

    expect(labels('Whose feedback')).toEqual(['All5', 'Employees3', 'My feedback2']);
  });

  it('adds the status tabs, counted over the whole list', async () => {
    await render();

    expect(labels('Feedback status')).toEqual(['All5', 'New2', 'Working1', 'Resolved2']);
    expect(shownIds()).toEqual(['1', '2', '3', '4', '5']);
  });

  it('narrows the list to New, then Working, then Resolved, and back to All', async () => {
    await render();

    await click(tab('Feedback status', 'New'));
    expect(shownIds()).toEqual(['1', '5']);

    await click(tab('Feedback status', 'Working'));
    expect(shownIds()).toEqual(['2']);

    await click(tab('Feedback status', 'Resolved'));
    expect(shownIds()).toEqual(['3', '4']);
    expect(tab('Feedback status', 'Resolved').getAttribute('aria-selected')).toBe('true');

    await click(tab('Feedback status', 'All'));
    expect(shownIds()).toEqual(['1', '2', '3', '4', '5']);
  });

  it('keeps the two strips honest about each other', async () => {
    await render();

    // Employees excludes the signed-in admin's own feedback (4 and 5).
    await click(tab('Whose feedback', 'Employees'));
    expect(shownIds()).toEqual(['1', '2', '3']);
    expect(labels('Feedback status')).toEqual(['All3', 'New1', 'Working1', 'Resolved1']);

    // Within Resolved, the scope tabs count only resolved feedback.
    await click(tab('Feedback status', 'Resolved'));
    expect(shownIds()).toEqual(['3']);
    expect(labels('Whose feedback')).toEqual(['All2', 'Employees1']);
  });

  it('puts the status filter back with Reset', async () => {
    await render();
    await click(tab('Feedback status', 'Working'));
    expect(shownIds()).toEqual(['2']);

    const reset = Array.from(container.querySelectorAll('button')).find((b) => b.textContent === 'Reset') as HTMLButtonElement;
    expect(reset).toBeTruthy();
    await click(reset);

    expect(shownIds()).toEqual(['1', '2', '3', '4', '5']);
    expect(tab('Feedback status', 'All').getAttribute('aria-selected')).toBe('true');
  });

  it('says nothing matches, rather than showing a blank, when a status has no rows', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(urlOf(input), 'http://localhost').pathname;
      if (path.endsWith('/feedback')) {
        const items = [row(1, 'new', 7)];
        return json({ items, page: 1, limit: 100, total: 1, pages: 1 });
      }
      return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
    }));
    await render();

    await click(tab('Feedback status', 'Resolved'));

    expect(shownIds()).toEqual([]);
    expect(container.textContent).toContain('No feedback matches these filters.');
  });
});
