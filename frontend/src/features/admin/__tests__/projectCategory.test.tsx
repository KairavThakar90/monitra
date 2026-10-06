// @vitest-environment jsdom
/**
 * The optional Category of a project: Kyle Project / ST Project / none.
 *
 * Rendered against a real RTK Query store with only `fetch` stubbed, so what
 * these pin is what a browser would send:
 *
 * - the Create Project drawer has an optional Category dropdown that defaults
 *   to "No category", and a project saves without one (sent as null);
 * - choosing Kyle Project or ST Project sends 'kyle' / 'st';
 * - the list can be filtered by category, and sends nothing for all;
 * - editing preselects the project's category, and "No category" clears it.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import { PROJECT_CATEGORY_OPTIONS, projectCategoryLabel } from '../../../utils/projectCategory';

const showToast = vi.fn();

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser: { id: 1, role_name: 'administrator', name: 'Admin', permissions: {} } }),
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
  category: null,
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
let container: HTMLDivElement;
let root: Root;

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

const click = async (element: Element | undefined) => {
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

const categorySelect = () => container.querySelector<HTMLSelectElement>('#project-category')!;
const filterSelect = () => container.querySelector<HTMLSelectElement>('select[aria-label="Filter by category"]')!;
const optionPairs = (select: HTMLSelectElement) => Array.from(select.options).map((o) => [o.value, o.textContent]);

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

const drawerField = <T extends Element>(label: string) => {
  const node = Array.from(container.querySelectorAll('#project-form label'))
    .find((item) => (item.textContent ?? '').replace(/\s*\*\s*$/, '').trim() === label);
  expect(node, `label ${label}`).toBeTruthy();
  return node!.nextElementSibling as unknown as T;
};

const fillRequiredFields = async () => {
  await setValue(drawerField<HTMLInputElement>('Project Name'), 'Categorised project');
  await setValue(container.querySelector<HTMLSelectElement>('#project-owner')!, String(OWNER.id));
  await setValue(drawerField<HTMLSelectElement>('Leader'), String(LEADER.id));
  await setValue(drawerField<HTMLInputElement>('Deadline'), futureDate());
  await click(byText('span', 'Flexible Time'));
};

const openCreateDrawer = async () => { await click(byText('button', '+ Create Project')); };

const openEditDrawer = async (projectName: string) => {
  const row = Array.from(container.querySelectorAll('tbody tr')).find((item) => item.textContent?.includes(projectName));
  await click(Array.from(row!.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Manage'));
  await click(byText('button', 'Edit'));
};

const posts = () => calls.filter((call) => call.method === 'POST' && /\/projects$/.test(call.url));
const patches = () => calls.filter((call) => call.method === 'PATCH');
const lastListQuery = () => {
  const lists = calls.filter((call) => call.method === 'GET' && /\/projects\?/.test(call.url) && !call.url.includes('include_tasks'));
  return new URLSearchParams(lists[lists.length - 1].url.split('?')[1]);
};

beforeEach(() => {
  calls = [];
  listed = [project()];
  showToast.mockReset();
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

describe('project categories', () => {
  it('are Kyle Project and ST Project, and anything else is a dash', () => {
    expect(PROJECT_CATEGORY_OPTIONS).toEqual([
      { value: 'kyle', label: 'Kyle Project' },
      { value: 'st', label: 'ST Project' },
    ]);
    expect(projectCategoryLabel('kyle')).toBe('Kyle Project');
    expect(projectCategoryLabel('st')).toBe('ST Project');
    expect(projectCategoryLabel(null)).toBe('—');
    expect(projectCategoryLabel(undefined)).toBe('—');
  });
});

describe('Create Project: Category dropdown', () => {
  it('is an optional dropdown that defaults to No category', async () => {
    await renderPage();
    await openCreateDrawer();
    expect(categorySelect()).toBeTruthy();
    expect(categorySelect().required).toBe(false);
    expect(optionPairs(categorySelect())).toEqual([['', 'No category'], ['kyle', 'Kyle Project'], ['st', 'ST Project']]);
    expect(categorySelect().value).toBe('');
    expect(container.querySelector('label[for="project-category"]')?.textContent).toContain('(optional)');
  });

  it('saves a project without one, sending null', async () => {
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await submit();

    expect(posts()).toHaveLength(1);
    expect(posts()[0].body.category).toBeNull();
    expect(showToast).toHaveBeenCalledWith('Project created successfully.', 'success');
  });

  it.each([['kyle', 'Kyle Project'], ['st', 'ST Project']])('sends %s when %s is chosen', async (value) => {
    await renderPage();
    await openCreateDrawer();
    await fillRequiredFields();
    await setValue(categorySelect(), value);
    await submit();

    expect(posts()[0].body.category).toBe(value);
    expect(showToast).toHaveBeenCalledWith('Project created successfully.', 'success');
  });

  it('starts empty again the next time the drawer opens', async () => {
    await renderPage();
    await openCreateDrawer();
    await setValue(categorySelect(), 'st');
    await click(container.querySelector('div[class*="bg-slate-900/40"]') as HTMLElement); // the backdrop closes the drawer
    await openCreateDrawer();
    expect(categorySelect().value).toBe('');
  });
});

describe('Project list: category', () => {
  it('has a category filter defaulting to all, and sends nothing for all', async () => {
    await renderPage();
    expect(optionPairs(filterSelect())).toEqual([['', 'All Categories'], ['kyle', 'Kyle Project'], ['st', 'ST Project']]);
    expect(filterSelect().value).toBe('');
    expect(lastListQuery().has('category')).toBe(false);
  });

  it('sends the chosen category, and drops it again for All Categories', async () => {
    await renderPage();
    await setValue(filterSelect(), 'st');
    expect(lastListQuery().get('category')).toBe('st');
    await setValue(filterSelect(), 'kyle');
    expect(lastListQuery().get('category')).toBe('kyle');
    await setValue(filterSelect(), '');
    // Back to "all" is served from the cache of the first, unfiltered query, so
    // no request goes out; what matters is that none ever carried an empty
    // or null category.
    expect(filterSelect().value).toBe('');
    const sent = calls
      .filter((call) => call.method === 'GET' && /\/projects\?/.test(call.url) && !call.url.includes('include_tasks'))
      .map((call) => new URLSearchParams(call.url.split('?')[1]).get('category'));
    expect(sent).toEqual([null, 'st', 'kyle']);
  });

  describe('Category column', () => {
    beforeEach(() => {
      listed = [
        project({ id: 1, project_name: 'Kyle job', category: 'kyle' }),
        project({ id: 2, project_name: 'ST job', category: 'st' }),
        project({ id: 3, project_name: 'Plain job', category: null }),
      ];
    });

    const cells = (name: string) =>
      Array.from(Array.from(container.querySelectorAll('tbody tr')).find((row) => row.textContent?.includes(name))!.querySelectorAll('td'))
        .map((cell) => cell.textContent?.trim());
    const headers = () => Array.from(container.querySelectorAll('thead th')).map((th) => th.textContent?.trim());
    const columnToggle = () =>
      Array.from(container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))
        .find((box) => box.closest('label')?.textContent?.trim() === 'Category')!;
    const openColumnsMenu = async () => { await click(byText('button', 'Columns')); };

    it('is hidden by default: no header and no category in any row', async () => {
      await renderPage();
      expect(headers()).not.toContain('Category');
      for (const name of ['Kyle job', 'ST job', 'Plain job']) {
        expect(cells(name)).not.toContain('Kyle Project');
        expect(cells(name)).not.toContain('ST Project');
      }
    });

    it('is offered in the Columns menu, unticked', async () => {
      await renderPage();
      await openColumnsMenu();
      expect(columnToggle()).toBeTruthy();
      expect(columnToggle().checked).toBe(false);
    });

    it('shows each project’s category, and a dash for an uncategorised one, once turned on', async () => {
      await renderPage();
      await openColumnsMenu();
      await click(columnToggle());

      expect(headers()).toContain('Category');
      expect(cells('Kyle job')).toContain('Kyle Project');
      expect(cells('ST job')).toContain('ST Project');
      expect(cells('Plain job')).not.toContain('Kyle Project');
      expect(cells('Plain job')).not.toContain('ST Project');
      expect(cells('Plain job')).toContain('—');
    });

    it('can be turned off again', async () => {
      await renderPage();
      await openColumnsMenu();
      await click(columnToggle());
      await click(columnToggle());
      expect(headers()).not.toContain('Category');
    });

    it('hiding the column does not hide the category filter', async () => {
      await renderPage();
      expect(filterSelect()).toBeTruthy();
    });
  });
});

describe('Edit Project: Category dropdown', () => {
  it('preselects the project’s category', async () => {
    listed = [project({ category: 'kyle' })];
    await renderPage();
    await openEditDrawer('Existing project');
    expect(categorySelect().value).toBe('kyle');
  });

  it('shows No category for a project that has none', async () => {
    await renderPage();
    await openEditDrawer('Existing project');
    expect(categorySelect().value).toBe('');
  });

  it('saves a change of category', async () => {
    listed = [project({ category: 'kyle' })];
    await renderPage();
    await openEditDrawer('Existing project');
    await setValue(categorySelect(), 'st');
    await submit();
    expect(patches()[0].body.category).toBe('st');
  });

  it('clears the category when No category is chosen', async () => {
    listed = [project({ category: 'st' })];
    await renderPage();
    await openEditDrawer('Existing project');
    await setValue(categorySelect(), '');
    await submit();
    expect(patches()).toHaveLength(1);
    expect(patches()[0].body).toHaveProperty('category', null);
  });
});
