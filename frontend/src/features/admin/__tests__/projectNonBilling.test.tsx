// @vitest-environment jsdom
/**
 * Budget & Billing in the create/edit project drawer.
 *
 * The form asks two things, in order. First: Billing or Non Billing. Choosing
 * Billing then shows how -- Fixed Hours (needs an hour budget) or Flexible Time
 * (none). Non Billing has nothing further to choose and no budget. They are
 * stored as `fixed`, `free` and `non_billing`.
 *
 * The real screen is rendered against a real RTK Query store with only `fetch`
 * stubbed, so what is pinned is the request a browser would make and what the
 * administrator sees:
 *
 * - Billing and Non Billing are offered first; Fixed Hours and Flexible Time
 *   appear only under Billing, and the hour budget only under Fixed Hours;
 * - Non Billing hides all of that and says the project is not billed;
 * - it is sent as `billing_type: "non_billing"` with no `fixed_hours`, and needs
 *   no budget to save;
 * - switching to it from Fixed Hours drops a budget that was already typed;
 * - Fixed Hours still requires its budget, and Flexible Time still sends `free`;
 * - the list labels the project "Non Billing", and an edit reopens where the
 *   project is: on Billing for fixed/free, on Non Billing for non_billing.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

const showToast = vi.fn();

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser: { id: 1, role_name: 'administrator', name: 'Admin' } }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction: async () => true }),
}));

import { AdminProjectManagement } from '../AdminProjectManagement';

const OWNER = { id: 701, name: 'Owner Alpha', email: 'alpha@example.invalid', role: 'administrator' };
const LEADER = { id: 801, name: 'Leader One', email: 'l1@example.invalid', role: 'leader' };

const project = (overrides: Record<string, unknown> = {}) => ({
  id: 55,
  project_name: 'Existing project',
  description: '',
  status: { id: 1, name: 'Active', color: '#22C55E' },
  owner: OWNER,
  leader: LEADER,
  employees: [],
  deadline: '2099-06-30',
  billing_type: 'free',
  fixed_hours: null,
  organization_id: 1,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
  tasks: [],
  employee_count: 0,
  task_count: 0,
  ...overrides,
});

type Call = { method: string; url: string; body: any };

let calls: Call[];
let listed: ReturnType<typeof project>[];

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const route = async (request: Request): Promise<Response> => {
  const url = new URL(request.url);
  const path = url.pathname;
  const body = request.method === 'GET' ? null : JSON.parse((await request.clone().text()) || 'null');
  calls.push({ method: request.method, url: path + url.search, body });

  if (path.endsWith('/project-management/metadata')) {
    return json({ roles: [], project_statuses: [{ id: 1, project_status: 'Active', color: '#22C55E' }], task_statuses: [] });
  }
  if (path.endsWith('/projects/assignable-owners')) return json([OWNER]);
  if (path.endsWith('/projects/assignable-leaders')) return json([LEADER]);
  if (path.endsWith('/projects/assignable-employees')) return json([]);
  if (path.endsWith('/projects/hours-summary')) return json({ items: [] });
  if (path.endsWith('/members')) return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
  if (path.endsWith('/projects') && request.method === 'GET') {
    return json({ items: listed, pagination: { page: 1, limit: 20, total: listed.length, total_pages: 1 } });
  }
  if (path.endsWith('/projects') && request.method === 'POST') return json(project({ id: 99, ...body }), 201);
  if (/\/projects\/\d+$/.test(path) && request.method === 'PATCH') {
    const current = listed.find((item) => path.endsWith(`/${item.id}`))!;
    return json({ ...current, ...body });
  }
  return json({ detail: 'Not found' }, 404);
};

let container: HTMLDivElement;
let root: Root;

const flush = async () => {
  for (let i = 0; i < 5; i += 1) {
    await act(async () => { await new Promise((resolveTick) => setTimeout(resolveTick, 0)); });
  }
};

const renderPage = async () => {
  const store = configureStore({
    reducer: { [baseApi.reducerPath]: baseApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(baseApi.middleware),
  });
  await act(async () => {
    root.render(<Provider store={store}><AdminProjectManagement /></Provider>);
  });
  await flush();
};

const byText = (selector: string, text: string) =>
  Array.from(container.querySelectorAll<HTMLElement>(selector)).find((node) => node.textContent?.trim() === text);

const click = async (element: Element | null | undefined) => {
  expect(element, 'element to click').toBeTruthy();
  await act(async () => { (element as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  await flush();
};

/** Set a controlled field the way a user's input would, so React sees it. */
const setValue = async (element: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement, value: string) => {
  const prototype = Object.getPrototypeOf(element);
  Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value);
  await act(async () => {
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
  });
};

const labelText = (item: Element) => (item.textContent ?? '').replace(/\s*\*\s*$/, '').trim();
const field = <T extends Element>(label: string) => {
  const node = Array.from(container.querySelectorAll('#project-form label')).find((item) => labelText(item) === label);
  expect(node, `label ${label}`).toBeTruthy();
  return node!.nextElementSibling as unknown as T;
};

const submit = async () => {
  await act(async () => {
    container.querySelector('#project-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  });
  await flush();
};

const futureDate = () => {
  const date = new Date();
  date.setDate(date.getDate() + 30);
  return date.toISOString().slice(0, 10);
};

/** The first choice: Billing / Non Billing. */
const modeRadios = () => Array.from(container.querySelectorAll<HTMLInputElement>('input[name="billingMode"]'));
/** The second choice, shown only under Billing: Fixed Hours / Flexible Time. */
const typeRadios = () => Array.from(container.querySelectorAll<HTMLInputElement>('input[name="billingType"]'));
const labelOf = (input: HTMLInputElement) => input.closest('label')!.textContent?.trim();
const mode = (value: 'billing' | 'non_billing') => modeRadios().find((input) => input.value === value)!;
const type = (value: 'fixed' | 'free') => typeRadios().find((input) => input.value === value)!;
/** What a user clicks: the visible label, not the screen-reader-only input. */
const choose = (input: HTMLInputElement) => click(input.closest('label'));
const hourBudget = () => container.querySelector<HTMLInputElement>('input[placeholder="e.g. 100"]');
const requests = (method: string, suffix: RegExp) => calls.filter((call) => call.method === method && suffix.test(call.url));

const NOT_BILLED_NOTE = 'This project is not billed and has no hour budget.';

/** Everything the form needs except the billing choice, which each test makes. */
const fillRequiredFields = async () => {
  await setValue(field<HTMLInputElement>('Project Name'), 'Internal tooling');
  await setValue(container.querySelector<HTMLSelectElement>('#project-owner')!, String(OWNER.id));
  await setValue(field<HTMLSelectElement>('Leader'), String(LEADER.id));
  await setValue(field<HTMLInputElement>('Deadline'), futureDate());
  // The Organization is required to create a project.
  await setValue(container.querySelector<HTMLSelectElement>('#project-category')!, 'kyle');
};

const openCreateDrawer = async () => {
  await click(byText('button', '+ Create Project'));
};

const openEditDrawer = async (projectName: string) => {
  const row = Array.from(container.querySelectorAll('tbody tr')).find((item) => item.textContent?.includes(projectName))!;
  await click(Array.from(row.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Manage'));
  await click(byText('button', 'Edit'));
};

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  calls = [];
  listed = [project()];
  showToast.mockReset();
  const entries = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => entries.set(key, value),
    removeItem: (key: string) => entries.delete(key),
    clear: () => entries.clear(),
  });
  vi.stubGlobal('fetch', vi.fn(route));
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => { root.unmount(); });
  container.remove();
  vi.unstubAllGlobals();
});

describe('Billing / Non Billing — the form', () => {
  it('asks Billing or Non Billing first, then Fixed Hours or Flexible Time under Billing', async () => {
    await renderPage();
    await openCreateDrawer();

    expect(modeRadios().map(labelOf)).toEqual(['Billing', 'Non Billing']);
    expect(mode('billing').checked).toBe(true);
    expect(mode('non_billing').checked).toBe(false);

    expect(typeRadios().map(labelOf)).toEqual(['Fixed Hours', 'Flexible Time']);
    expect(type('fixed').checked).toBe(true);
    expect(hourBudget()).not.toBeNull();
    expect(container.textContent).not.toContain(NOT_BILLED_NOTE);
  });

  it('shows no Fixed Hours / Flexible Time choice and no hour budget under Non Billing', async () => {
    await renderPage();
    await openCreateDrawer();
    await choose(mode('non_billing'));

    expect(mode('non_billing').checked).toBe(true);
    expect(mode('billing').checked).toBe(false);
    expect(typeRadios()).toHaveLength(0);
    expect(hourBudget()).toBeNull();
    expect(container.textContent).toContain(NOT_BILLED_NOTE);
  });

  it('brings Fixed Hours and Flexible Time back, on Fixed Hours, when Billing is chosen again', async () => {
    await renderPage();
    await openCreateDrawer();
    await choose(type('free'));
    await choose(mode('non_billing'));
    await choose(mode('billing'));

    expect(typeRadios().map(labelOf)).toEqual(['Fixed Hours', 'Flexible Time']);
    expect(type('fixed').checked).toBe(true);
    expect(hourBudget()).not.toBeNull();
    expect(container.textContent).not.toContain(NOT_BILLED_NOTE);
  });

  it('keeps the hour budget to Fixed Hours alone', async () => {
    await renderPage();
    await openCreateDrawer();
    expect(hourBudget()).not.toBeNull();
    await choose(type('free'));
    expect(hourBudget()).toBeNull();
    expect(mode('billing').checked).toBe(true);
  });
});

describe('Billing / Non Billing — create', () => {
  it('creates the project as non_billing, with no hour budget and no budget needed', async () => {
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await choose(mode('non_billing'));
    await submit();

    const [created] = requests('POST', /\/projects$/);
    expect(created, 'a create request').toBeTruthy();
    expect(created.body.billing_type).toBe('non_billing');
    expect(created.body.fixed_hours).toBeNull();
    expect(created.body.project_name).toBe('Internal tooling');
    expect(showToast).toHaveBeenCalledWith('Project created successfully.', 'success');
  });

  it('drops an hour budget that was typed before switching to Non Billing', async () => {
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await setValue(hourBudget()!, '120');
    await choose(mode('non_billing'));
    await submit();

    const [created] = requests('POST', /\/projects$/);
    expect(created.body.billing_type).toBe('non_billing');
    expect(created.body.fixed_hours).toBeNull();

    // And a fresh form does not resurrect it.
    await openCreateDrawer();
    expect(hourBudget()!.value).toBe('');
  });

  it('still requires an hour budget under Fixed Hours, and sends nothing without one', async () => {
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await submit();

    expect(requests('POST', /\/projects$/)).toHaveLength(0);
    expect(showToast).toHaveBeenCalledWith('Please correct the highlighted fields.', 'error');

    await setValue(hourBudget()!, '80');
    await submit();
    const [created] = requests('POST', /\/projects$/);
    expect(created.body.billing_type).toBe('fixed');
    expect(created.body.fixed_hours).toBe(80);
  });

  it('still sends Flexible Time as free, with no hour budget', async () => {
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await choose(type('free'));
    await submit();

    const [created] = requests('POST', /\/projects$/);
    expect(created.body.billing_type).toBe('free');
    expect(created.body.fixed_hours).toBeNull();
  });
});

describe('Billing / Non Billing — list and edit', () => {
  it('labels each project by its billing type in the table', async () => {
    listed = [
      project({ id: 1, project_name: 'Fixed one', billing_type: 'fixed', fixed_hours: 40 }),
      project({ id: 2, project_name: 'Flexible one', billing_type: 'free' }),
      project({ id: 3, project_name: 'Internal one', billing_type: 'non_billing' }),
    ];
    await renderPage();
    const rowOf = (name: string) =>
      Array.from(container.querySelectorAll('tbody tr')).find((item) => item.textContent?.includes(name))!;

    expect(rowOf('Fixed one').textContent).toContain('40 Hours');
    // The column says "Flexible Time" -- the name the filter and the Create
    // Project form use -- not the old "Free Time".
    expect(rowOf('Flexible one').textContent).toContain('Flexible Time');
    expect(rowOf('Flexible one').textContent).not.toContain('Free Time');
    expect(rowOf('Flexible one').textContent).not.toContain('Non Billing');
    expect(rowOf('Internal one').textContent).toContain('Non Billing');
    expect(rowOf('Internal one').textContent).not.toContain('Flexible Time');
  });

  it('reopens a non-billing project on Non Billing and saves it unchanged', async () => {
    listed = [project({ id: 3, project_name: 'Internal one', billing_type: 'non_billing' })];
    await renderPage();
    await openEditDrawer('Internal one');

    expect(mode('non_billing').checked).toBe(true);
    expect(typeRadios()).toHaveLength(0);
    expect(hourBudget()).toBeNull();

    await submit();
    const [patched] = requests('PATCH', /\/projects\/3$/);
    expect(patched.body.billing_type).toBe('non_billing');
    expect(patched.body.fixed_hours).toBeNull();
  });

  it('reopens a Flexible Time project on Billing, with Flexible Time selected and no budget', async () => {
    listed = [project({ id: 5, project_name: 'Flexible one', billing_type: 'free' })];
    await renderPage();
    await openEditDrawer('Flexible one');

    expect(mode('billing').checked).toBe(true);
    expect(type('free').checked).toBe(true);
    expect(type('fixed').checked).toBe(false);
    expect(hourBudget()).toBeNull();
  });

  it('reopens a Fixed Hours project on Billing with its budget', async () => {
    listed = [project({ id: 6, project_name: 'Was fixed', billing_type: 'fixed', fixed_hours: 60 })];
    await renderPage();
    await openEditDrawer('Was fixed');

    expect(mode('billing').checked).toBe(true);
    expect(type('fixed').checked).toBe(true);
    expect(hourBudget()!.value).toBe('60');
  });

  it('moves a fixed project to Non Billing by clearing its budget', async () => {
    listed = [project({ id: 4, project_name: 'Was fixed', billing_type: 'fixed', fixed_hours: 60 })];
    await renderPage();
    await openEditDrawer('Was fixed');
    expect(hourBudget()!.value).toBe('60');

    await choose(mode('non_billing'));
    await submit();

    const [patched] = requests('PATCH', /\/projects\/4$/);
    expect(patched.body.billing_type).toBe('non_billing');
    expect(patched.body.fixed_hours).toBeNull();
  });

  it('moves a non-billing project to Billing as Fixed Hours, which then needs a budget', async () => {
    listed = [project({ id: 7, project_name: 'Internal one', billing_type: 'non_billing' })];
    await renderPage();
    await openEditDrawer('Internal one');
    await choose(mode('billing'));
    expect(type('fixed').checked).toBe(true);

    await submit();
    expect(requests('PATCH', /\/projects\/7$/)).toHaveLength(0);

    await setValue(hourBudget()!, '25');
    await submit();
    const [patched] = requests('PATCH', /\/projects\/7$/);
    expect(patched.body.billing_type).toBe('fixed');
    expect(patched.body.fixed_hours).toBe(25);
  });
});
