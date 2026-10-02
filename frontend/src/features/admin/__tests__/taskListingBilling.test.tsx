// @vitest-environment jsdom
/**
 * Task Listing (the "Active Task List" page): project type and budget colours.
 *
 * The real page is rendered against a real RTK Query store with only `fetch`
 * replaced, so what is pinned is the request a browser would make and what an
 * administrator sees:
 *
 * - a project-type filter in two steps: Billing / Non Billing, then under Billing
 *   Fixed Hours / Flexible Time. It is sent to the server as repeated
 *   `billing_type` params (so paging stays truthful), and nothing is sent for "all";
 * - each project shows its billing type, and a fixed-hours project shows how much
 *   of its budget is spent, coloured by the dashboard's own bands -- blue under 80%,
 *   yellow 80-99%, green exactly on budget, red over. Flexible and non-billing
 *   projects have nothing to measure and show no meter.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser: { id: 1, role_name: 'administrator', name: 'Admin' } }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminTaskListing } from '../AdminTaskListing';

const HOUR = 3600;

/** Deliberately not anybody real: the picker must offer whatever the API returns. */
const MEMBERS = [
  { id: 7, name: 'Asha Example', email: 'asha@example.invalid', role: 'employee', status: 'active' },
  { id: 9, name: 'Ravi Example', email: 'ravi@example.invalid', role: 'employee', status: 'active' },
];

const project = (overrides: Record<string, unknown> = {}) => ({
  id: 1,
  project_name: 'Project',
  created_date: '2026-08-01',
  status: null,
  billing_type: 'free',
  fixed_hours: null,
  used_seconds: null,
  remaining_seconds: null,
  usage_percentage: null,
  total_task_count: 0,
  total_task_seconds: 0,
  total_task_hours: 0,
  total_task_time: '00:00:00',
  tasks: [],
  ...overrides,
});

/** A fixed-hours project that has used `usedHours` of `fixedHours`. */
const fixedProject = (id: number, name: string, fixedHours: number, usedHours: number) =>
  project({
    id,
    project_name: name,
    billing_type: 'fixed',
    fixed_hours: fixedHours,
    used_seconds: usedHours * HOUR,
    remaining_seconds: (fixedHours - usedHours) * HOUR,
    usage_percentage: Math.round((usedHours / fixedHours) * 10000) / 100,
  });

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

describe('Task Listing: project type filter and budget colours', () => {
  let container: HTMLDivElement;
  let root: Root;
  let summaryQueries: URLSearchParams[];
  let summaryProjects: ReturnType<typeof project>[];

  const flush = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    summaryQueries = [];
    summaryProjects = [];
    const entries = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => entries.get(key) ?? null,
      setItem: (key: string, value: string) => entries.set(key, value),
      removeItem: (key: string) => entries.delete(key),
      clear: () => entries.clear(),
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      if (url.pathname.endsWith('/reports/project-task-summary')) {
        summaryQueries.push(url.searchParams);
        // Filter the way the server does: before the page is cut. A project carries
        // `by`, the members who worked on it today (a test-only field).
        const types = url.searchParams.getAll('billing_type');
        const members = url.searchParams.getAll('member_id').map(Number);
        const shown = summaryProjects
          .filter((item) => !types.length || types.includes(String(item.billing_type)))
          .filter((item) => !members.length || ((item as { by?: number[] }).by ?? []).some((id) => members.includes(id)));
        return json({
          projects: shown,
          pagination: { page: 1, limit: 10, total_projects: shown.length, total_pages: 1 },
        });
      }
      if (url.pathname.endsWith('/project-management/metadata')) {
        return json({ roles: [], project_statuses: [], task_statuses: [] });
      }
      if (url.pathname.endsWith('/projects') && request.method === 'GET') {
        return json({ items: [], pagination: { page: 1, limit: 100, total: 0, total_pages: 1 } });
      }
      if (url.pathname.endsWith('/projects/assignable-employees')) return json([]);
      if (url.pathname.endsWith('/members')) {
        return json({ items: MEMBERS, page: 1, limit: 100, total: MEMBERS.length, pages: 1 });
      }
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

  const mount = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><AdminTaskListing /></Provider>); });
    await flush();
  };

  const typeSelect = () => container.querySelector<HTMLSelectElement>('select[aria-label="Filter by project type"]')!;
  const kindSelect = () => container.querySelector<HTMLSelectElement>('select[aria-label="Filter by billing type"]');
  const choose = async (select: HTMLSelectElement, value: string) => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(select, value);
    await act(async () => { select.dispatchEvent(new Event('change', { bubbles: true })); });
    await flush();
  };
  const lastQuery = () => summaryQueries[summaryQueries.length - 1];
  /** The project names on screen, in order -- what an administrator actually sees. */
  const shown = () => Array.from(container.querySelectorAll('h3')).map((h) => h.textContent);
  const threeKinds = () => [
    fixedProject(1, 'Fixed one', 40, 10),
    project({ id: 2, project_name: 'Flexible one', billing_type: 'free' }),
    project({ id: 3, project_name: 'Internal one', billing_type: 'non_billing' }),
  ];
  const optionsOf = (select: HTMLSelectElement) =>
    Array.from(select.options).map((o) => [o.value, o.textContent]);
  /** The project's header block: everything above its task list. */
  const headerOf = (name: string) =>
    Array.from(container.querySelectorAll('h3')).find((h) => h.textContent === name)!.parentElement!;

  describe('the filter', () => {
    it('offers All Project Types, Billing and Non Billing, and shows every project by default', async () => {
      summaryProjects = threeKinds();
      await mount();
      expect(optionsOf(typeSelect())).toEqual([
        ['', 'All Project Types'],
        ['billing', 'Billing'],
        ['non_billing', 'Non Billing'],
      ]);
      expect(typeSelect().value).toBe('');
      expect(kindSelect()).toBeNull();
      expect(summaryQueries[0].has('billing_type')).toBe(false);
      expect(shown()).toEqual(['Fixed one', 'Flexible one', 'Internal one']);
    });

    it('shows Fixed Hours and Flexible Time only once Billing is chosen, and lists both billed kinds', async () => {
      summaryProjects = threeKinds();
      await mount();
      await choose(typeSelect(), 'billing');

      expect(kindSelect()).not.toBeNull();
      expect(optionsOf(kindSelect()!)).toEqual([
        ['', 'All Billing'],
        ['fixed', 'Fixed Hours'],
        ['free', 'Flexible Time'],
      ]);
      expect(lastQuery().getAll('billing_type').sort()).toEqual(['fixed', 'free']);
      expect(shown()).toEqual(['Fixed one', 'Flexible one']);
    });

    it('narrows Billing to Fixed Hours, then to Flexible Time, then back to both', async () => {
      summaryProjects = threeKinds();
      await mount();
      await choose(typeSelect(), 'billing');

      await choose(kindSelect()!, 'fixed');
      expect(lastQuery().getAll('billing_type')).toEqual(['fixed']);
      expect(shown()).toEqual(['Fixed one']);

      await choose(kindSelect()!, 'free');
      expect(lastQuery().getAll('billing_type')).toEqual(['free']);
      expect(shown()).toEqual(['Flexible one']);

      await choose(kindSelect()!, '');
      expect(shown()).toEqual(['Fixed one', 'Flexible one']);
    });

    it('lists only non-billing projects under Non Billing, with no second choice', async () => {
      summaryProjects = threeKinds();
      await mount();
      await choose(typeSelect(), 'non_billing');
      expect(lastQuery().getAll('billing_type')).toEqual(['non_billing']);
      expect(kindSelect()).toBeNull();
      expect(shown()).toEqual(['Internal one']);
    });

    it('lists every project again for All Project Types, and removes the second choice', async () => {
      summaryProjects = threeKinds();
      await mount();
      await choose(typeSelect(), 'billing');
      await choose(kindSelect()!, 'fixed');
      expect(shown()).toEqual(['Fixed one']);

      await choose(typeSelect(), '');
      expect(kindSelect()).toBeNull();
      expect(shown()).toEqual(['Fixed one', 'Flexible one', 'Internal one']);
    });

    it('forgets Fixed Hours when Billing is left and chosen again', async () => {
      summaryProjects = threeKinds();
      await mount();
      await choose(typeSelect(), 'billing');
      await choose(kindSelect()!, 'fixed');
      await choose(typeSelect(), 'non_billing');
      await choose(typeSelect(), 'billing');

      expect(kindSelect()!.value).toBe('');
      expect(shown()).toEqual(['Fixed one', 'Flexible one']);
    });

    it('says so when no project of the chosen type was worked on today', async () => {
      summaryProjects = [fixedProject(1, 'Fixed one', 40, 10)];
      await mount();
      await choose(typeSelect(), 'non_billing');
      expect(shown()).toEqual(['Nothing worked on today']);
      expect(container.textContent).toContain('project type');
    });

    it('returns to the first page when the filter changes', async () => {
      summaryProjects = threeKinds();
      await mount();
      await choose(typeSelect(), 'billing');
      expect(lastQuery().get('page')).toBe('1');
    });
  });

  describe('the filter bar (the Reports page\'s design)', () => {
    /** The white rounded card holding every filter. */
    const bar = () => container.querySelector('.rounded-2xl') as HTMLElement;
    const buttonWith = (text: string) =>
      Array.from(bar().querySelectorAll('button')).find((b) => b.textContent?.trim().includes(text)) as HTMLButtonElement;
    const press = async (el: Element | undefined) => {
      expect(el, 'element to click').toBeTruthy();
      await act(async () => { el!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
      await flush();
    };
    /** Pick a member in the Members picker the way a user does: open it, click the name. */
    const pickMember = async (name: string) => {
      await press(buttonWith('All members'));
      await press(Array.from(bar().querySelectorAll('button')).find((b) => b.textContent?.includes(name)));
    };
    /** What each project was last worked on by, for the stub's server-side member filter. */
    const withWorkers = () => [
      { ...fixedProject(1, 'Fixed one', 40, 10), by: [7] },
      { ...project({ id: 2, project_name: 'Flexible one', billing_type: 'free' }), by: [9] },
      { ...project({ id: 3, project_name: 'Internal one', billing_type: 'non_billing' }), by: [7, 9] },
    ];

    it('puts the calendar on the left and the other filters on the right, in one rounded card', async () => {
      await mount();
      const date = bar().querySelector('button') as HTMLButtonElement;
      expect(date.textContent).toContain('Today');
      expect(bar().className).toContain('justify-between');
      expect(bar().children[0].contains(date)).toBe(true);
      expect(bar().children[1].contains(buttonWith('All members'))).toBe(true);
      expect(date.compareDocumentPosition(buttonWith('All members')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(date.compareDocumentPosition(buttonWith('All projects')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });

    it('orders the right-hand filters Members, Projects, Project type, then Reset and Expand All', async () => {
      await mount();
      const order = [buttonWith('All members'), buttonWith('All projects'), typeSelect(), buttonWith('Reset'), buttonWith('Expand All')];
      for (let i = 0; i < order.length - 1; i += 1) {
        expect(order[i].compareDocumentPosition(order[i + 1]) & Node.DOCUMENT_POSITION_FOLLOWING, `control ${i}`).toBeTruthy();
      }
    });

    it('draws every filter at one height', async () => {
      await mount();
      await choose(typeSelect(), 'billing'); // brings the second select in too
      const controls = [
        bar().querySelector('button'),
        buttonWith('All members'),
        buttonWith('All projects'),
        typeSelect(),
        kindSelect(),
        buttonWith('Reset'),
        buttonWith('Expand All'),
      ];
      expect(controls).toHaveLength(7);
      for (const control of controls) {
        expect(control, 'a filter control').toBeTruthy();
        expect((control as HTMLElement).className).toContain('h-9');
      }
      // The Projects picker used to be the compact, smaller one on this page.
      expect(buttonWith('All projects').className).not.toContain('py-1.5');
    });

    it('offers a Members filter listing whoever the API returned', async () => {
      await mount();
      await press(buttonWith('All members'));
      expect(bar().textContent).toContain('Asha Example');
      expect(bar().textContent).toContain('Ravi Example');
    });

    it('sends the chosen member and shows only that member\'s projects', async () => {
      summaryProjects = withWorkers() as never;
      await mount();
      expect(shown()).toEqual(['Fixed one', 'Flexible one', 'Internal one']);
      expect(summaryQueries[0].has('member_id')).toBe(false);

      await pickMember('Ravi Example');
      expect(lastQuery().getAll('member_id')).toEqual(['9']);
      expect(shown()).toEqual(['Flexible one', 'Internal one']);
    });

    it('combines the member filter with the project type filter', async () => {
      summaryProjects = withWorkers() as never;
      await mount();
      await pickMember('Asha Example');
      await choose(typeSelect(), 'non_billing');

      expect(lastQuery().getAll('member_id')).toEqual(['7']);
      expect(lastQuery().getAll('billing_type')).toEqual(['non_billing']);
      expect(shown()).toEqual(['Internal one']);
    });

    it('goes back to the first page when the member changes', async () => {
      summaryProjects = withWorkers() as never;
      await mount();
      await pickMember('Asha Example');
      expect(lastQuery().get('page')).toBe('1');
    });

    it('Reset returns every filter to its opening state', async () => {
      summaryProjects = withWorkers() as never;
      await mount();
      await pickMember('Ravi Example');
      await choose(typeSelect(), 'billing');
      await choose(kindSelect()!, 'free');
      expect(shown()).toEqual(['Flexible one']);

      await press(buttonWith('Reset'));
      expect(typeSelect().value).toBe('');
      expect(kindSelect()).toBeNull();
      expect(buttonWith('All members')).toBeTruthy();
      expect(shown()).toEqual(['Fixed one', 'Flexible one', 'Internal one']);
    });

    it('says the member filter applies when nothing matches', async () => {
      summaryProjects = withWorkers() as never;
      await mount();
      await choose(typeSelect(), 'non_billing');
      await pickMember('Ravi Example'); // Internal one is worked by both, so narrow further
      await choose(typeSelect(), 'billing');
      await choose(kindSelect()!, 'fixed');
      expect(shown()).toEqual(['Nothing worked on today']); // Fixed one is Asha's only
      expect(container.textContent).toContain('member, project or project type');
    });
  });

  describe('each project', () => {
    it('is labelled by its billing type', async () => {
      summaryProjects = [
        fixedProject(1, 'Fixed one', 40, 10),
        project({ id: 2, project_name: 'Flexible one', billing_type: 'free' }),
        project({ id: 3, project_name: 'Internal one', billing_type: 'non_billing' }),
      ];
      await mount();
      const chip = (name: string) => headerOf(name).querySelector('[data-testid="billing-chip"]')!.textContent;
      expect(chip('Fixed one')).toBe('Fixed Hours');
      expect(chip('Flexible one')).toBe('Flexible Time');
      expect(chip('Internal one')).toBe('Non Billing');
    });

    it('shows a fixed-hours project\'s used hours against its budget', async () => {
      summaryProjects = [fixedProject(1, 'Fixed one', 40, 32)];
      await mount();
      const usage = headerOf('Fixed one');
      expect(usage.querySelector('[data-testid="budget-usage-hours"]')!.textContent).toBe('Used 32:00:00 of 40h');
      expect(usage.querySelector('[data-testid="budget-usage-percent"]')!.textContent).toBe('80%');
    });

    it('shows no meter for flexible or non-billing projects', async () => {
      summaryProjects = [
        project({ id: 2, project_name: 'Flexible one', billing_type: 'free' }),
        project({ id: 3, project_name: 'Internal one', billing_type: 'non_billing' }),
      ];
      await mount();
      expect(container.querySelectorAll('[data-testid="budget-usage"]')).toHaveLength(0);
    });

    it('shows no chip and no NaN when the backend predates the billing fields', async () => {
      // A deploy can put the web app live before the API: the new fields are simply absent.
      summaryProjects = [{ id: 9, project_name: 'Old API', created_date: '2026-08-01', status: null,
        total_task_count: 0, total_task_seconds: 0, total_task_hours: 0, total_task_time: '00:00:00', tasks: [] } as never];
      await mount();
      expect(shown()).toEqual(['Old API']);
      expect(container.querySelectorAll('[data-testid="billing-chip"]')).toHaveLength(0);
      expect(container.querySelectorAll('[data-testid="budget-usage"]')).toHaveLength(0);
      expect(container.textContent).not.toContain('NaN');
    });

    it('shows no meter for a fixed project that has no budget set', async () => {
      summaryProjects = [project({ id: 4, project_name: 'No budget', billing_type: 'fixed' })];
      await mount();
      expect(container.querySelectorAll('[data-testid="budget-usage"]')).toHaveLength(0);
    });
  });

  describe('the hours colour is the dashboard\'s', () => {
    // jsdom normalises a hex colour to rgb().
    const BLUE = 'rgb(59, 130, 246)';
    const YELLOW = 'rgb(234, 179, 8)';
    const GREEN = 'rgb(16, 185, 129)';
    const RED = 'rgb(239, 68, 68)';

    const colorOf = (name: string) =>
      (headerOf(name).querySelector('[data-testid="budget-usage-hours"]') as HTMLElement).style.color;

    it('is blue while in progress, yellow when closing in, green on budget, red over', async () => {
      summaryProjects = [
        fixedProject(1, 'In progress', 100, 50),
        fixedProject(2, 'Closing in', 100, 90),
        fixedProject(3, 'On budget', 100, 100),
        fixedProject(4, 'Over budget', 100, 120),
      ];
      await mount();
      expect(colorOf('In progress')).toBe(BLUE);
      expect(colorOf('Closing in')).toBe(YELLOW);
      expect(colorOf('On budget')).toBe(GREEN);
      expect(colorOf('Over budget')).toBe(RED);
    });

    it('colours the percentage and the bar the same as the hours', async () => {
      summaryProjects = [fixedProject(1, 'On budget', 40, 40)];
      await mount();
      const header = headerOf('On budget');
      expect((header.querySelector('[data-testid="budget-usage-percent"]') as HTMLElement).style.color).toBe(GREEN);
      const fill = header.querySelector('[data-testid="budget-usage"] .h-full') as HTMLElement;
      expect(fill.style.backgroundColor).toBe(GREEN);
      expect(fill.style.width).toBe('100%');
    });

    it('says by how much a project is over budget, and never draws the bar past full', async () => {
      summaryProjects = [fixedProject(1, 'Over budget', 40, 44)];
      await mount();
      const header = headerOf('Over budget');
      expect(header.querySelector('[data-testid="budget-usage"]')!.textContent).toContain('Over by 04:00:00');
      expect(header.querySelector('[data-testid="budget-usage-percent"]')!.textContent).toBe('110%');
      const fill = header.querySelector('[data-testid="budget-usage"] .h-full') as HTMLElement;
      expect(fill.style.width).toBe('100%');
    });

    it('does not mention "over" for a project within budget', async () => {
      summaryProjects = [fixedProject(1, 'Within', 40, 10)];
      await mount();
      expect(headerOf('Within').querySelector('[data-testid="budget-usage"]')!.textContent).not.toContain('Over by');
    });
  });
});
