// @vitest-environment jsdom
/**
 * The Project Management page's Created column and creation-date filter.
 *
 * Created is shown by default and Started is hidden (the Columns menu toggles
 * either); hiding a column never hides or disables its filter.
 *
 * The date filter is the picker the Assign Tasks page has, with "All Time" on
 * offer. Unlike Assign Tasks -- which filters what it has already loaded -- this
 * table is paged by the server, so the range has to travel as `created_from` /
 * `created_to` or it would only ever narrow the current page. The days are IST
 * calendar days: the column shows the IST day a project was created, and the
 * filter compares on the same one.
 *
 * The page opens on All Time (no date parameters), not a last-7-days window:
 * this table has always listed every project, and a bounded default would make
 * most of them vanish until someone found the picker.
 *
 * The clock is frozen at 15:30 IST on 6 Oct 2026, so the presets resolve to
 * known days.
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
  useAuth: () => ({ currentUser: { id: 1, role_name: 'administrator', name: 'Admin', permissions: {} } }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminProjectManagement } from '../AdminProjectManagement';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const project = (id: number, name: string, createdAt: string | null) => ({
  id,
  project_name: name,
  description: '',
  status: { id: 1, name: 'Active', color: '#22C55E' },
  owner: null,
  leader: null,
  employees: [],
  deadline: null,
  billing_type: 'free',
  fixed_hours: null,
  category: null,
  organization_id: 1,
  created_at: createdAt,
  updated_at: createdAt,
  tasks: [],
  employee_count: 0,
  task_count: 0,
});

describe('Project Management: Created column and date filter', () => {
  let container: HTMLDivElement;
  let root: Root;
  let projectUrls: URL[];

  const flush = async () => {
    for (let i = 0; i < 8; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-06T10:00:00Z')); // 15:30 IST on the 6th
    projectUrls = [];
    vi.stubGlobal('localStorage', {
      getItem: () => null, setItem: () => undefined, removeItem: () => undefined, clear: () => undefined,
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      if (url.pathname.endsWith('/project-management/metadata')) {
        return json({ roles: [], project_statuses: [], task_statuses: [] });
      }
      if (url.pathname.endsWith('/projects') && request.method === 'GET') {
        projectUrls.push(url);
        // The first row names the range the response was for, because a range
        // already fetched is served from cache with no new request: what is on
        // screen, not the last request, says which filter is applied.
        const from = url.searchParams.get('created_from');
        const to = url.searchParams.get('created_to');
        return json({
          items: [
            // 18:30:00 UTC is exactly midnight IST: the 6th by the clock in India, the 5th in UTC.
            project(1, `range:${from || '-'}..${to || '-'}`, '2026-10-05T18:30:00Z'),
            project(2, 'Last second', '2026-10-05T18:29:59Z'),
            project(3, 'Undated', null),
          ],
          pagination: { page: Number(url.searchParams.get('page')), limit: 20, total: 50, total_pages: 3 },
        });
      }
      if (url.pathname.endsWith('/members')) return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
      if (url.pathname.includes('/projects/hours-summary')) return json({ items: [] });
      if (url.pathname.includes('/projects/')) return json([]);
      return json({}, 404);
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(<Provider store={store}><AdminProjectManagement /></Provider>);
    });
    await flush();
  });

  afterEach(async () => {
    await act(async () => { root.unmount(); });
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  const click = async (element: Element | undefined) => {
    expect(element, 'element to click').toBeTruthy();
    await act(async () => { (element as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    await flush();
  };
  const buttonWithText = (text: string) =>
    Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.includes(text));
  /**
   * The date picker's trigger: the button showing the current range's name. It
   * comes before the panel's preset buttons in the document, so the first match
   * is the trigger whether or not the panel is open.
   */
  const RANGE_NAME = /^(All Time|Today|Yesterday|Last (7 days|week|2 weeks|30 days|month)|This month|Custom range)/;
  const pickerTrigger = () =>
    Array.from(container.querySelectorAll('button')).find((b) => RANGE_NAME.test(b.textContent?.trim() ?? ''));
  const pickPreset = async (label: string) => {
    await click(pickerTrigger());
    // The panel's rail: presets are the buttons whose whole text is the label.
    const preset = Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.trim() === label);
    await click(preset);
  };
  const headers = () => Array.from(container.querySelectorAll('thead th')).map((th) => th.textContent?.trim());
  const createdCells = () => Array.from(container.querySelectorAll('[data-testid="created-cell"]')).map((td) => td.textContent?.trim());
  const lastUrl = () => projectUrls[projectUrls.length - 1];
  /** The range the rows on screen were fetched with. */
  const applied = () => /range:([-0-9.]+)/.exec(container.textContent ?? '')?.[1];

  describe('the Created and Started columns', () => {
    // Created is shown. Started starts hidden, like Organization and Tasks: it is
    // not what this table is usually opened for, and the Columns menu turns it on.
    const menuOpen = () => (container.textContent ?? '').includes('Visible Columns');
    const columnBox = (label: string) =>
      Array.from(container.querySelectorAll<HTMLInputElement>('label input[type="checkbox"]'))
        .find((input) => input.closest('label')?.textContent?.trim() === label)!;
    /** Ticks or unticks a column in the Columns menu, opening the menu first if it is shut. */
    const toggleColumn = async (label: string) => {
      if (!menuOpen()) await click(buttonWithText('Columns'));
      await click(columnBox(label));
    };

    it('Created is shown by default, and Started is not', () => {
      expect(headers()).toContain('Created');
      expect(headers()).not.toContain('Started');
      expect(createdCells()).toHaveLength(3);
      expect(container.textContent).not.toContain('Not Started Yet');
    });

    it('Created sits just after Remaining Hours, where the Started column would otherwise follow', () => {
      const labels = headers();
      expect(labels.indexOf('Created')).toBe(labels.indexOf('Remaining Hours') + 1);
    });

    it('shows the IST day each project was created, in the table’s date format', () => {
      // Midnight IST on the 6th is 18:30 UTC on the 5th; the column must say the 6th.
      expect(createdCells()[0]).toBe('06 Oct 2026');
      // One second earlier is still the 5th.
      expect(createdCells()[1]).toBe('05 Oct 2026');
    });

    it('shows a dash, not an invented day, when the API sends no creation time', () => {
      expect(createdCells()[2]).toBe('—');
    });

    it('are both offered in the Columns menu: Created ticked, Started unticked', async () => {
      await click(buttonWithText('Columns'));
      expect(columnBox('Created').checked).toBe(true);
      expect(columnBox('Started').checked).toBe(false);
    });

    it('Created can be hidden from the Columns menu, and shown again', async () => {
      await toggleColumn('Created');
      expect(headers()).not.toContain('Created');
      expect(createdCells()).toEqual([]);
      await toggleColumn('Created');
      expect(headers()).toContain('Created');
      expect(createdCells()[0]).toBe('06 Oct 2026');
    });

    it('Started appears once turned on, after Created', async () => {
      await toggleColumn('Started');
      const labels = headers();
      expect(labels).toContain('Started');
      expect(labels.indexOf('Created')).toBe(labels.indexOf('Started') - 1);
      expect(container.textContent).toContain('Not Started Yet');
    });

    it('Started can be turned off again', async () => {
      await toggleColumn('Started');
      await toggleColumn('Started');
      expect(headers()).not.toContain('Started');
    });

    it('hiding Created does not touch Started, nor any other column', async () => {
      const before = headers();
      await toggleColumn('Created');
      expect(headers()).not.toContain('Started');
      expect(headers()).toEqual(before.filter((label) => label !== 'Created'));
    });

    it('hiding the Created column does not hide the creation-date filter, which still filters', async () => {
      await toggleColumn('Created');
      expect(headers()).not.toContain('Created');
      expect(pickerTrigger()).toBeTruthy();
      await pickPreset('Last 7 days');
      expect(lastUrl().searchParams.get('created_from')).toBe('2026-09-30');
      expect(lastUrl().searchParams.get('created_to')).toBe('2026-10-06');
    });
  });

  describe('the date filter', () => {
    it('is the Assign Tasks picker, opening on All Time', () => {
      expect(pickerTrigger()?.textContent).toContain('All Time');
    });

    it('sends no date parameters until a range is picked, so every project is listed', () => {
      expect(projectUrls.length).toBeGreaterThan(0);
      for (const url of projectUrls) {
        expect(url.searchParams.has('created_from')).toBe(false);
        expect(url.searchParams.has('created_to')).toBe(false);
      }
      expect(applied()).toBe('-..-');
    });

    it('offers All Time and the usual presets', async () => {
      await click(pickerTrigger());
      const labels = Array.from(container.querySelectorAll('button')).map((b) => b.textContent?.trim());
      for (const label of ['All Time', 'Today', 'Yesterday', 'Last 7 days', 'Last 30 days', 'This month', 'Last month']) {
        expect(labels).toContain(label);
      }
    });

    it('asks the server for the IST days of a preset, both ends inclusive', async () => {
      await pickPreset('Last 7 days');
      expect(lastUrl().searchParams.get('created_from')).toBe('2026-09-30');
      expect(lastUrl().searchParams.get('created_to')).toBe('2026-10-06');
      expect(applied()).toBe('2026-09-30..2026-10-06');
    });

    it('asks for one day when Today is picked', async () => {
      await pickPreset('Today');
      expect(lastUrl().searchParams.get('created_from')).toBe('2026-10-06');
      expect(lastUrl().searchParams.get('created_to')).toBe('2026-10-06');
    });

    it('drops the range again when All Time is picked', async () => {
      await pickPreset('Last 30 days');
      // 30 days counting both ends: 7-30 Sep is 24 days, 1-6 Oct is 6 more.
      expect(applied()).toBe('2026-09-07..2026-10-06');
      await pickPreset('All Time');
      expect(applied()).toBe('-..-');
      expect(pickerTrigger()?.textContent).toContain('All Time');
    });

    it('goes back to page 1 when the range changes', async () => {
      const goToPage = async (label: string) => {
        await click(Array.from(container.querySelectorAll('button')).find((b) => b.textContent === label));
      };
      await goToPage('2');
      expect(lastUrl().searchParams.get('page')).toBe('2');
      await pickPreset('Last 7 days');
      expect(lastUrl().searchParams.get('page')).toBe('1');
    });

    it('sits beside the other filters rather than replacing any of them', async () => {
      expect(container.querySelector('select[aria-label="Filter by project type"]')).not.toBeNull();
      expect(container.querySelector('select[aria-label="Filter by organization"]')).not.toBeNull();
      await pickPreset('Last 7 days');
      expect(container.querySelector('select[aria-label="Filter by project type"]')).not.toBeNull();
      expect(container.querySelector('input[placeholder*="earch"]')).not.toBeNull();
    });

    it('combines with another filter in the same request', async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(
        container.querySelector('select[aria-label="Filter by organization"]'), 'kyle',
      );
      await act(async () => {
        container.querySelector('select[aria-label="Filter by organization"]')!.dispatchEvent(new Event('change', { bubbles: true }));
      });
      await flush();
      await pickPreset('Today');
      expect(lastUrl().searchParams.get('category')).toBe('kyle');
      expect(lastUrl().searchParams.get('created_from')).toBe('2026-10-06');
    });
  });
});
