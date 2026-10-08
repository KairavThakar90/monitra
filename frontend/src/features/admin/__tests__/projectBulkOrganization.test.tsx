// @vitest-environment jsdom
/**
 * Assigning an Organization to several projects at once, on Project Management.
 *
 * Rendered against a real RTK Query store with only `fetch` stubbed, so what
 * these pin is what a browser would send and show:
 *
 * - the tick boxes are HIDDEN by default: they are a column ("Select (bulk
 *   assign)") that the Columns menu turns on, and turning it off again drops
 *   whatever was ticked;
 * - the header box ticks every project on the page (and is "partly ticked"
 *   while only some are), and ticking nothing shows no bulk bar;
 * - choosing an organization and pressing Assign sends ONE request carrying
 *   the ticked ids in the order shown, after a confirmation, and nothing is
 *   sent until an organization is chosen or if the confirmation is declined;
 * - the result is said in words (changed / already had it / could not be
 *   changed), a refused request says why and keeps the ticks, and the ticks
 *   are dropped when the rows on screen change.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

const showToast = vi.fn();
const confirmAction = vi.fn(async (_title: string, _message: string) => true);

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser: { id: 1, role_name: 'administrator', name: 'Admin', permissions: {} } }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction }),
}));

import { AdminProjectManagement } from '../AdminProjectManagement';

const OWNER = { id: 701, name: 'Owner Alpha', email: 'alpha@example.invalid', role: 'administrator' };
const LEADER = { id: 801, name: 'Leader One', email: 'l1@example.invalid', role: 'leader' };

const project = (overrides: Record<string, unknown> = {}) => ({
  id: 55,
  project_name: 'Alpha',
  description: '',
  status: { id: 1, name: 'Active', color: '#22C55E' },
  owner: OWNER,
  leader: LEADER,
  employees: [],
  deadline: '2099-06-30',
  billing_type: 'free',
  category: null as string | null,
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
/** What the stubbed server answers to the bulk request; the default applies it for real. */
let bulkAnswer: ((body: any) => Response | Promise<Response>) | null;
let container: HTMLDivElement;
let root: Root;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const applyBulk = (body: { project_ids: number[]; category: string }) => {
  const updated: { id: number; project_name: string; category: string }[] = [];
  const unchanged: number[] = [];
  for (const id of body.project_ids) {
    const row = listed.find((item) => item.id === id)!;
    if (row.category === body.category) { unchanged.push(id); continue; }
    row.category = body.category;
    updated.push({ id, project_name: row.project_name, category: body.category });
  }
  return json({ category: body.category, updated, unchanged, failed: [] });
};

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
  if (path.endsWith('/projects/category') && request.method === 'PATCH') {
    return bulkAnswer ? bulkAnswer(body) : applyBulk(body);
  }
  if (path.endsWith('/projects') && request.method === 'GET') {
    return json({ items: listed, pagination: { page: 1, limit: 20, total: listed.length, total_pages: 1 } });
  }
  return json({ detail: 'Not found' }, 404);
};

const flush = async () => {
  for (let i = 0; i < 5; i += 1) {
    await act(async () => { await new Promise((resolveTick) => setTimeout(resolveTick, 0)); });
  }
};

const renderPage = async () => {
  const store = configureStore({
    reducer: { [baseApi.reducerPath]: baseApi.reducer },
    middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
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

const setValue = async (element: HTMLInputElement | HTMLSelectElement, value: string) => {
  Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), 'value')!.set!.call(element, value);
  await act(async () => {
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
  });
  await flush();
};

/** Turn a column on or off in the Columns menu, then close the menu. */
const toggleColumn = async (label: string) => {
  await click(byText('button', 'Columns'));
  const input = byText('label', label)?.querySelector('input');
  await click(input);
  await click(byText('button', 'Columns'));
};

const headerBox = () => container.querySelector<HTMLInputElement>('input[aria-label="Select all projects on this page"]');
const rowBox = (name: string) => container.querySelector<HTMLInputElement>(`input[aria-label="Select ${name}"]`);
const bar = () => container.querySelector('[aria-label="Assign organization to the selected projects"]');
const orgSelect = () => container.querySelector<HTMLSelectElement>('select[aria-label="Organization to assign"]');
const assignButton = () => byText('button', 'Assign organization') as HTMLButtonElement | undefined;
const selectedCount = () => container.querySelector('[data-testid="bulk-selected-count"]')?.textContent;
const bulkCalls = () => calls.filter((call) => call.method === 'PATCH' && call.url.endsWith('/projects/category'));
const toastMessages = () => showToast.mock.calls.map((args) => [args[0], args[1]]);

const turnSelectionOn = async () => { await toggleColumn('Select (bulk assign)'); };

beforeEach(() => {
  calls = [];
  bulkAnswer = null;
  listed = [
    project({ id: 55, project_name: 'Alpha', category: null }),
    project({ id: 56, project_name: 'Beta', category: 'kyle' }),
    project({ id: 57, project_name: 'Gamma', category: null }),
  ];
  showToast.mockReset();
  confirmAction.mockReset();
  confirmAction.mockImplementation(async () => true);
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
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

describe('bulk assign: the tick boxes are hidden until turned on', () => {
  it('shows no tick box and no bulk bar by default', async () => {
    await renderPage();

    expect(container.querySelectorAll('tbody input[type="checkbox"]')).toHaveLength(0);
    expect(headerBox()).toBeNull();
    expect(bar()).toBeNull();
    expect(container.querySelector('tbody')?.textContent).toContain('Alpha');
  });

  it('offers them in the Columns menu, unticked', async () => {
    await renderPage();
    await click(byText('button', 'Columns'));

    const input = byText('label', 'Select (bulk assign)')?.querySelector('input');
    expect(input, 'a Select entry in the Columns menu').toBeTruthy();
    expect(input!.checked).toBe(false);
  });

  it('turning it on adds a tick box to the header and to every project, and still no bar', async () => {
    await renderPage();
    await turnSelectionOn();

    expect(headerBox()).toBeTruthy();
    expect(rowBox('Alpha')).toBeTruthy();
    expect(rowBox('Beta')).toBeTruthy();
    expect(rowBox('Gamma')).toBeTruthy();
    expect(bar(), 'nothing ticked, nothing to act on').toBeNull();
  });

  it('turning it off again takes the boxes away and drops what was ticked', async () => {
    await renderPage();
    await turnSelectionOn();
    await click(rowBox('Alpha'));
    expect(bar()).toBeTruthy();

    await toggleColumn('Select (bulk assign)');

    expect(headerBox()).toBeNull();
    expect(bar()).toBeNull();
    await turnSelectionOn();
    expect(rowBox('Alpha')!.checked, 'a tick from before is not remembered').toBe(false);
  });
});

describe('bulk assign: choosing the projects', () => {
  it('ticking a project shows how many are selected', async () => {
    await renderPage();
    await turnSelectionOn();

    await click(rowBox('Alpha'));
    expect(selectedCount()).toBe('1');
    await click(rowBox('Gamma'));
    expect(selectedCount()).toBe('2');
    await click(rowBox('Alpha'));
    expect(selectedCount()).toBe('1');
  });

  it('the header box ticks every project on the page, and again clears them', async () => {
    await renderPage();
    await turnSelectionOn();

    await click(headerBox());
    expect(['Alpha', 'Beta', 'Gamma'].map((name) => rowBox(name)!.checked)).toEqual([true, true, true]);
    expect(selectedCount()).toBe('3');
    expect(headerBox()!.checked).toBe(true);

    await click(headerBox());
    expect(['Alpha', 'Beta', 'Gamma'].map((name) => rowBox(name)!.checked)).toEqual([false, false, false]);
    expect(bar()).toBeNull();
  });

  it('the header box is partly ticked while only some projects are', async () => {
    await renderPage();
    await turnSelectionOn();

    await click(rowBox('Beta'));

    expect(headerBox()!.indeterminate).toBe(true);
    expect(headerBox()!.checked).toBe(false);
    await click(headerBox());   // from partly ticked, it ticks the rest
    expect(selectedCount()).toBe('3');
    expect(headerBox()!.indeterminate).toBe(false);
  });

  it('Clear selection unticks everything and removes the bar', async () => {
    await renderPage();
    await turnSelectionOn();
    await click(headerBox());

    await click(byText('button', 'Clear selection'));

    expect(bar()).toBeNull();
    expect(rowBox('Alpha')!.checked).toBe(false);
  });

  it('the ticks are dropped when the rows on screen change', async () => {
    await renderPage();
    await turnSelectionOn();
    await click(rowBox('Alpha'));
    expect(bar()).toBeTruthy();

    const filter = container.querySelector<HTMLSelectElement>('select[aria-label="Filter by organization"]')!;
    await setValue(filter, 'kyle');

    expect(bar(), 'a project the person can no longer see is not part of the selection').toBeNull();
    await setValue(filter, '');
    expect(rowBox('Alpha')!.checked).toBe(false);
  });
});

describe('bulk assign: assigning the organization', () => {
  const tick = async (...names: string[]) => { for (const name of names) await click(rowBox(name)); };

  it('sends nothing until an organization is chosen', async () => {
    await renderPage();
    await turnSelectionOn();
    await tick('Alpha');

    expect(assignButton()!.disabled).toBe(true);
    await click(assignButton());

    expect(bulkCalls()).toHaveLength(0);
    expect(confirmAction).not.toHaveBeenCalled();
  });

  it('offers the two organizations', async () => {
    await renderPage();
    await turnSelectionOn();
    await tick('Alpha');

    expect(Array.from(orgSelect()!.options).map((option) => [option.value, option.textContent])).toEqual([
      ['', 'Choose organization...'],
      ['kyle', 'Kyle Project'],
      ['st', 'ST Project'],
    ]);
  });

  it('asks first, then sends ONE request with the ticked ids in the order shown', async () => {
    await renderPage();
    await turnSelectionOn();
    await tick('Gamma', 'Alpha');   // ticked out of order on purpose
    await setValue(orgSelect()!, 'st');

    await click(assignButton());

    expect(confirmAction).toHaveBeenCalledWith('Assign organization?', '2 projects will be set to ST Project.');
    expect(bulkCalls()).toHaveLength(1);
    expect(bulkCalls()[0].body).toEqual({ project_ids: [55, 57], category: 'st' });
    expect(calls.filter((call) => call.method === 'PATCH' && /\/projects\/\d+$/.test(call.url)), 'no per-project edits').toHaveLength(0);
  });

  it('says what was done, clears the selection and shows the new organization', async () => {
    await renderPage();
    await toggleColumn('Organization');
    await turnSelectionOn();
    await tick('Alpha', 'Gamma');
    await setValue(orgSelect()!, 'st');

    await click(assignButton());

    expect(toastMessages()).toEqual([['ST Project set for 2 projects.', 'success']]);
    expect(bar()).toBeNull();
    expect(rowBox('Alpha')!.checked).toBe(false);
    const rows = Array.from(container.querySelectorAll('tbody tr')).map((row) => row.textContent ?? '');
    expect(rows[0]).toContain('ST Project');
    expect(rows[1]).toContain('Kyle Project');   // Beta was not selected
    expect(rows[2]).toContain('ST Project');
  });

  it('says a single project in the singular', async () => {
    await renderPage();
    await turnSelectionOn();
    await tick('Alpha');
    await setValue(orgSelect()!, 'kyle');

    await click(assignButton());

    expect(confirmAction).toHaveBeenCalledWith('Assign organization?', '1 project will be set to Kyle Project.');
    expect(toastMessages()).toEqual([['Kyle Project set for 1 project.', 'success']]);
  });

  it('says so when some already had it, and when nothing needed changing', async () => {
    await renderPage();
    await turnSelectionOn();
    await tick('Alpha', 'Beta');   // Beta is already Kyle
    await setValue(orgSelect()!, 'kyle');
    await click(assignButton());

    expect(toastMessages()).toEqual([['Kyle Project set for 1 project. 1 project already had it.', 'success']]);

    showToast.mockReset();
    await tick('Beta');
    await setValue(orgSelect()!, 'kyle');
    await click(assignButton());

    expect(toastMessages()).toEqual([['1 project already had it.', 'info']]);
  });

  it('reports a project that could not be changed, and still says what was', async () => {
    bulkAnswer = () => json({
      category: 'st',
      updated: [{ id: 55, project_name: 'Alpha', category: 'st' }],
      unchanged: [],
      failed: [{ id: 57, detail: 'Project not found.' }],
    });
    await renderPage();
    await turnSelectionOn();
    await tick('Alpha', 'Gamma');
    await setValue(orgSelect()!, 'st');

    await click(assignButton());

    expect(toastMessages()).toEqual([['ST Project set for 1 project. 1 project could not be changed.', 'error']]);
  });

  it('a refused request says why, changes nothing on screen and keeps the ticks to try again', async () => {
    bulkAnswer = () => json({ detail: 'Insufficient permissions for this action' }, 403);
    await renderPage();
    await toggleColumn('Organization');
    await turnSelectionOn();
    await tick('Alpha');
    await setValue(orgSelect()!, 'st');

    await click(assignButton());

    expect(toastMessages()).toEqual([['Insufficient permissions for this action', 'error']]);
    expect(rowBox('Alpha')!.checked).toBe(true);
    expect(orgSelect()!.value).toBe('st');
    expect(container.querySelector('tbody tr')?.textContent).not.toContain('ST Project');
  });

  it('sends nothing when the confirmation is declined, and keeps the selection', async () => {
    confirmAction.mockImplementation(async () => false);
    await renderPage();
    await turnSelectionOn();
    await tick('Alpha', 'Gamma');
    await setValue(orgSelect()!, 'st');

    await click(assignButton());

    expect(bulkCalls()).toHaveLength(0);
    expect(showToast).not.toHaveBeenCalled();
    expect(selectedCount()).toBe('2');
  });
});
