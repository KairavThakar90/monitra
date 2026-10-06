// @vitest-environment jsdom
/**
 * The project picker in the Add Client / Edit Access modals.
 *
 * It lists every project with a checkbox. These tests render the real screen
 * against a real RTK Query store, with only `fetch` stubbed, and pin:
 *
 * - a search box narrows the list by project name;
 * - "Select all" ticks (and unticks) the projects the search is showing and
 *   leaves the rest alone, and shows a mixed state when only some are ticked;
 * - the count is how many projects are selected in total, hidden ones included;
 * - Enter in the search box does not submit the form;
 * - what is sent on save is exactly the selected projects.
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

import { AdminClients } from '../AdminClients';

const PROJECTS = [
  { id: 1, project_name: 'Alpha Website' },
  { id: 2, project_name: 'Beta Portal' },
  { id: 3, project_name: 'Gamma website redesign' },
  { id: 4, project_name: 'Delta Mobile' },
];

type Call = { method: string; path: string; body: any };

let calls: Call[];
let clients: unknown[];
let container: HTMLDivElement;
let root: Root;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const route = async (request: Request): Promise<Response> => {
  const url = new URL(request.url);
  const body = request.method === 'GET' ? null : JSON.parse((await request.clone().text()) || 'null');
  calls.push({ method: request.method, path: url.pathname, body });

  if (url.pathname.endsWith('/clients/invitations') && request.method === 'POST') return json({ id: 1, status: 'pending' }, 201);
  if (url.pathname.endsWith('/clients') && request.method === 'GET') {
    return json({ items: clients, pagination: { page: 1, limit: 20, total: clients.length, total_pages: 1 } });
  }
  if (url.pathname.endsWith('/projects') && request.method === 'GET') {
    return json({ items: PROJECTS, pagination: { page: 1, limit: 100, total: PROJECTS.length, total_pages: 1 } });
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
    root.render(<Provider store={store}><AdminClients /></Provider>);
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

const setValue = async (element: HTMLInputElement, value: string) => {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(element, value);
  await act(async () => { element.dispatchEvent(new Event('input', { bubbles: true })); });
};

const searchBox = () => container.querySelector<HTMLInputElement>('input[aria-label="Search projects"]')!;
/** Everything the picker renders: the search box, the Select all row and the list. */
const picker = () => searchBox().parentElement!;
const selectAll = () => picker().querySelector<HTMLInputElement>('input[type="checkbox"]')!;
const projectBoxes = () => Array.from(picker().querySelectorAll<HTMLInputElement>('input[type="checkbox"]')).slice(1);
const rowNames = () => projectBoxes().map((box) => box.closest('label')!.textContent?.trim());
const countText = () => picker().querySelector('[aria-live="polite"]')!.textContent?.trim();
const tickedNames = () => projectBoxes().filter((box) => box.checked).map((box) => box.closest('label')!.textContent?.trim());

const openAddClient = async () => {
  await renderPage();
  await click(byText('button', '+ Add Client'));
};

beforeEach(() => {
  calls = [];
  clients = [];
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
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

describe('Add Client: the project picker', () => {
  it('lists every project with a search box, Select all and a count of none selected', async () => {
    await openAddClient();
    expect(searchBox()).toBeTruthy();
    expect(picker().textContent).toContain('Select all');
    expect(rowNames()).toEqual(['Alpha Website', 'Beta Portal', 'Gamma website redesign', 'Delta Mobile']);
    expect(countText()).toBe('0 of 4 selected');
    expect(selectAll().checked).toBe(false);
  });

  it('counts the projects as they are ticked', async () => {
    await openAddClient();
    await click(projectBoxes()[1]);
    expect(countText()).toBe('1 of 4 selected');
    await click(projectBoxes()[3]);
    expect(countText()).toBe('2 of 4 selected');
    await click(projectBoxes()[1]);
    expect(countText()).toBe('1 of 4 selected');
  });

  it('shows a mixed Select all when only some are ticked', async () => {
    await openAddClient();
    await click(projectBoxes()[0]);
    expect(selectAll().checked).toBe(false);
    expect(selectAll().indeterminate).toBe(true);
  });

  it('Select all ticks every project, and clicking it again clears them', async () => {
    await openAddClient();
    await click(selectAll());
    expect(tickedNames()).toHaveLength(4);
    expect(countText()).toBe('4 of 4 selected');
    expect(selectAll().checked).toBe(true);
    expect(selectAll().indeterminate).toBe(false);

    await click(selectAll());
    expect(tickedNames()).toHaveLength(0);
    expect(countText()).toBe('0 of 4 selected');
  });

  it('Select all completes a partial selection rather than clearing it', async () => {
    await openAddClient();
    await click(projectBoxes()[0]);
    await click(selectAll());
    expect(countText()).toBe('4 of 4 selected');
  });

  it('the search narrows the list by name, ignoring case', async () => {
    await openAddClient();
    await setValue(searchBox(), 'WEBSITE');
    expect(rowNames()).toEqual(['Alpha Website', 'Gamma website redesign']);
    await setValue(searchBox(), '  beta ');
    expect(rowNames()).toEqual(['Beta Portal']);
    await setValue(searchBox(), '');
    expect(rowNames()).toHaveLength(4);
  });

  it('says so when nothing matches, and Select all has nothing to select', async () => {
    await openAddClient();
    await setValue(searchBox(), 'zzz');
    expect(rowNames()).toEqual([]);
    expect(picker().textContent).toContain('No projects match your search.');
    expect(selectAll().disabled).toBe(true);
  });

  it('Select all only takes the projects the search shows', async () => {
    await openAddClient();
    await setValue(searchBox(), 'website');
    expect(picker().textContent).toContain('Select all results');
    await click(selectAll());
    expect(tickedNames()).toEqual(['Alpha Website', 'Gamma website redesign']);
    expect(countText()).toBe('2 of 4 selected');

    await setValue(searchBox(), '');
    expect(tickedNames()).toEqual(['Alpha Website', 'Gamma website redesign']);
    expect(selectAll().indeterminate).toBe(true);
  });

  it('keeps counting a selected project the search is hiding', async () => {
    await openAddClient();
    await click(projectBoxes()[3]); // Delta Mobile
    await setValue(searchBox(), 'alpha');
    expect(rowNames()).toEqual(['Alpha Website']);
    expect(countText()).toBe('1 of 4 selected');
    // Select all then adds to it; it does not replace it.
    await click(selectAll());
    expect(countText()).toBe('2 of 4 selected');
    // ...and unticking the visible results leaves the hidden one alone.
    await click(selectAll());
    expect(countText()).toBe('1 of 4 selected');
  });

  it('pressing Enter in the search box does not submit the form', async () => {
    await openAddClient();
    const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    await act(async () => { searchBox().dispatchEvent(enter); });
    expect(enter.defaultPrevented).toBe(true);
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
  });

  it('sends exactly the selected projects when the invitation is sent', async () => {
    await openAddClient();
    await setValue(container.querySelector<HTMLInputElement>('input[type="email"]')!, 'client@example.invalid');
    await setValue(searchBox(), 'website');
    await click(selectAll());
    await setValue(searchBox(), '');
    await click(projectBoxes()[3]); // Delta Mobile
    await act(async () => {
      container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    });
    await flush();

    const [sent] = calls.filter((call) => call.method === 'POST' && call.path.endsWith('/clients/invitations'));
    expect(sent.body.email).toBe('client@example.invalid');
    expect([...sent.body.project_ids].sort()).toEqual([1, 3, 4]);
  });

  it('still refuses to send with no project selected', async () => {
    await openAddClient();
    await setValue(container.querySelector<HTMLInputElement>('input[type="email"]')!, 'client@example.invalid');
    await act(async () => {
      container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    });
    await flush();
    expect(container.textContent).toContain('Select at least one project to share.');
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
  });
});

describe('Edit Access: the same picker', () => {
  it('starts with the client’s projects selected and counted', async () => {
    clients = [{
      id: 7, name: 'Acme', email: 'acme@example.invalid', status: 'active', created_at: '2026-09-01T00:00:00Z',
      projects: [{ id: 2, project_name: 'Beta Portal' }, { id: 4, project_name: 'Delta Mobile' }],
      permissions: { share_member_details: true, share_screenshots: false, share_tasks: true, share_timing: true, share_billing: false },
    }];
    await renderPage();
    // The client list and the project list are two requests; under a loaded
    // machine five ticks is not always enough for the table to appear, so wait
    // for what the test acts on rather than for a fixed number of ticks.
    for (let attempt = 0; attempt < 60 && !byText('button', 'Edit'); attempt += 1) {
      await flush();
    }
    await click(byText('button', 'Edit'));
    for (let attempt = 0; attempt < 60 && (!searchBox() || projectBoxes().length < 4); attempt += 1) {
      await flush();
    }
    expect(searchBox()).toBeTruthy();
    expect(tickedNames()).toEqual(['Beta Portal', 'Delta Mobile']);
    expect(countText()).toBe('2 of 4 selected');
  });
});
