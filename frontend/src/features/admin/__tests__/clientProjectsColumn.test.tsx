// @vitest-environment jsdom
/**
 * The Projects column on the admin Clients page.
 *
 * A client can be given hundreds of projects. The column used to join every name
 * into one comma-separated paragraph, which made that client's row taller than
 * the screen. A row now shows the first few names as chips and a "See all (N)"
 * button; pressing it shows every project (in a scrollable box) and the button
 * becomes "Show less". A client with only a few projects shows them all, with no
 * button.
 *
 * The real page is rendered against a real RTK Query store with only `fetch`
 * stubbed.
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

const PERMISSIONS = {
  share_member_details: false, share_tasks: false, share_time_entries: false, share_screenshots: false,
  share_billing: false, share_reports: false,
};

const projectsNamed = (count: number, prefix = 'Project') =>
  Array.from({ length: count }, (_, index) => ({ id: index + 1, project_name: `${prefix} ${index + 1}` }));

const client = (id: number, name: string, projects: { id: number; project_name: string }[]) => ({
  id,
  name,
  email: `${name.toLowerCase().replace(/\s+/g, '')}@example.invalid`,
  status: 'active',
  projects,
  permissions: PERMISSIONS,
  created_at: '2026-09-23T00:00:00Z',
});

type Call = { method: string; path: string };

let calls: Call[];
let clients: unknown[];
let container: HTMLDivElement;
let root: Root;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const route = async (request: Request): Promise<Response> => {
  const url = new URL(request.url);
  calls.push({ method: request.method, path: url.pathname });
  if (url.pathname.endsWith('/clients') && request.method === 'GET') {
    return json({ items: clients, pagination: { page: 1, limit: 20, total: clients.length, total_pages: 1 } });
  }
  if (url.pathname.endsWith('/projects') && request.method === 'GET') {
    return json({ items: [], pagination: { page: 1, limit: 100, total: 0, total_pages: 1 } });
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
  // Wait for the table itself, not for a fixed number of ticks: when the whole
  // suite runs in parallel the clients request can take longer than that.
  for (let attempt = 0; attempt < 80 && container.querySelectorAll('tbody tr').length < clients.length; attempt += 1) {
    await flush();
  }
};

const click = async (element: Element | undefined) => {
  expect(element, 'element to click').toBeTruthy();
  await act(async () => { (element as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  await flush();
};

/** One client's table row, found by the name in its first cell. */
const row = (name: string) =>
  Array.from(container.querySelectorAll('tbody tr')).find((tr) => tr.querySelector('td')?.textContent === name) as HTMLTableRowElement;
const chips = (name: string) => Array.from(row(name).querySelectorAll('li')).map((li) => li.textContent);
const toggle = (name: string) =>
  Array.from(row(name).querySelectorAll('button')).find((b) => /^(See all|Show less)/.test(b.textContent?.trim() ?? '')) as HTMLButtonElement | undefined;
const toggleLabel = (name: string) => toggle(name)?.textContent?.trim();
const projectList = (name: string) => row(name).querySelector('ul') as HTMLUListElement;

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

describe('Clients: the Projects column', () => {
  describe('a client with few projects', () => {
    it('shows a dash, and no button, when there are none', async () => {
      clients = [client(1, 'Nobody', [])];
      await renderPage();
      expect(row('Nobody').querySelectorAll('td')[2].textContent).toBe('—');
      expect(toggle('Nobody')).toBeUndefined();
      expect(chips('Nobody')).toEqual([]);
    });

    it.each([1, 2, 3])('shows all %i projects and has no button', async (count) => {
      clients = [client(1, 'Few', projectsNamed(count))];
      await renderPage();
      expect(chips('Few')).toEqual(projectsNamed(count).map((p) => p.project_name));
      expect(toggle('Few')).toBeUndefined();
    });
  });

  describe('a client with many projects', () => {
    it('shows only the first three, in order, with a See all button that says how many there are', async () => {
      clients = [client(1, 'Many', projectsNamed(150))];
      await renderPage();
      expect(chips('Many')).toEqual(['Project 1', 'Project 2', 'Project 3']);
      expect(toggleLabel('Many')).toBe('See all (150)');
    });

    it('starts collapsing at four: the fourth project is behind See all (4)', async () => {
      clients = [client(1, 'Four', projectsNamed(4))];
      await renderPage();
      expect(chips('Four')).toEqual(['Project 1', 'Project 2', 'Project 3']);
      expect(toggleLabel('Four')).toBe('See all (4)');
    });

    it('does not print the names as one comma-separated paragraph any more', async () => {
      clients = [client(1, 'Many', projectsNamed(150))];
      await renderPage();
      expect(container.textContent).not.toContain('Project 1, Project 2');
      expect(container.textContent).not.toContain('Project 150');
    });

    it('See all shows every project, in the order the server sent them, and offers Show less', async () => {
      clients = [client(1, 'Many', projectsNamed(150))];
      await renderPage();
      await click(toggle('Many'));

      expect(chips('Many')).toEqual(projectsNamed(150).map((p) => p.project_name));
      expect(toggleLabel('Many')).toBe('Show less');
    });

    it('Show less goes back to the first three', async () => {
      clients = [client(1, 'Many', projectsNamed(150))];
      await renderPage();
      await click(toggle('Many'));
      await click(toggle('Many'));

      expect(chips('Many')).toEqual(['Project 1', 'Project 2', 'Project 3']);
      expect(toggleLabel('Many')).toBe('See all (150)');
    });

    it('keeps the row a sensible height when opened: the full list is a scrollable box', async () => {
      clients = [client(1, 'Many', projectsNamed(150))];
      await renderPage();
      expect(projectList('Many').className).not.toContain('overflow-y-auto');

      await click(toggle('Many'));
      expect(projectList('Many').className).toContain('max-h-56');
      expect(projectList('Many').className).toContain('overflow-y-auto');
    });

    it('tells assistive technology whether it is open, and what the button controls', async () => {
      clients = [client(1, 'Many', projectsNamed(10))];
      await renderPage();
      const button = toggle('Many')!;
      expect(button.getAttribute('aria-expanded')).toBe('false');
      expect(button.getAttribute('aria-controls')).toBe(projectList('Many').id);
      expect(button.getAttribute('type')).toBe('button');

      await click(button);
      expect(toggle('Many')!.getAttribute('aria-expanded')).toBe('true');
    });
  });

  describe('several clients', () => {
    it('opening one leaves the others collapsed', async () => {
      clients = [
        client(1, 'First', projectsNamed(6, 'A')),
        client(2, 'Second', projectsNamed(7, 'B')),
      ];
      await renderPage();
      await click(toggle('First'));

      expect(chips('First')).toHaveLength(6);
      expect(chips('Second')).toEqual(['B 1', 'B 2', 'B 3']);
      expect(toggleLabel('Second')).toBe('See all (7)');
    });

    it('each can be opened and closed on its own', async () => {
      clients = [
        client(1, 'First', projectsNamed(6, 'A')),
        client(2, 'Second', projectsNamed(7, 'B')),
      ];
      await renderPage();
      await click(toggle('First'));
      await click(toggle('Second'));
      await click(toggle('First'));

      expect(chips('First')).toHaveLength(3);
      expect(chips('Second')).toHaveLength(7);
    });

    it('gives each row’s list its own id, so each button controls its own list', async () => {
      clients = [
        client(1, 'First', projectsNamed(5, 'A')),
        client(2, 'Second', projectsNamed(5, 'B')),
      ];
      await renderPage();
      const ids = [projectList('First').id, projectList('Second').id];
      expect(ids[0]).not.toBe(ids[1]);
      expect(toggle('First')!.getAttribute('aria-controls')).toBe(ids[0]);
      expect(toggle('Second')!.getAttribute('aria-controls')).toBe(ids[1]);
    });

    it('lists each client’s own projects, never another’s', async () => {
      clients = [
        client(1, 'First', projectsNamed(5, 'A')),
        client(2, 'Second', projectsNamed(5, 'B')),
      ];
      await renderPage();
      await click(toggle('First'));
      await click(toggle('Second'));

      expect(chips('First').every((name) => name!.startsWith('A '))).toBe(true);
      expect(chips('Second').every((name) => name!.startsWith('B '))).toBe(true);
    });
  });

  describe('long names', () => {
    const LONG = 'wealthexponential-Kyle Projects-SEO and a name that goes on well past any sensible column width for a chip';

    it('are cut with an ellipsis and carry the full name as a tooltip', async () => {
      clients = [client(1, 'Wordy', [{ id: 1, project_name: LONG }])];
      await renderPage();
      const chip = row('Wordy').querySelector('li')!;
      expect(chip.className).toContain('truncate');
      expect(chip.className).toContain('max-w-');
      expect(chip.getAttribute('title')).toBe(LONG);
      expect(chip.textContent).toBe(LONG);
    });
  });

  describe('the rest of the page', () => {
    it('pressing See all makes no request and opens nothing', async () => {
      clients = [client(1, 'Many', projectsNamed(10))];
      await renderPage();
      const before = calls.length;
      await click(toggle('Many'));

      expect(calls).toHaveLength(before);
      expect(container.textContent).not.toContain('Shared Projects');
      expect(container.textContent).not.toContain('Select Projects');
    });

    it('keeps the other columns and the row actions', async () => {
      clients = [client(1, 'Many', projectsNamed(10))];
      await renderPage();
      const headers = Array.from(container.querySelectorAll('thead th')).map((th) => th.textContent);
      expect(headers).toEqual(['Client Name', 'Email', 'Projects', 'Status', 'Created Date', 'Actions']);
      const labels = Array.from(row('Many').querySelectorAll('button')).map((b) => b.textContent?.trim());
      expect(labels).toContain('Edit');
      expect(labels).toContain('Deactivate');
    });

    it('stays collapsed after the row’s actions are used elsewhere in the table', async () => {
      clients = [client(1, 'Many', projectsNamed(10)), client(2, 'Other', projectsNamed(2))];
      await renderPage();
      await click(toggle('Many'));
      expect(chips('Many')).toHaveLength(10);
      expect(chips('Other')).toHaveLength(2);
    });
  });
});
