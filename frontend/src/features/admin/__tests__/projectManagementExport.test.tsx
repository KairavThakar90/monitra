// @vitest-environment jsdom
/**
 * The Project Management page's Export CSV.
 *
 * An export button was removed from this page on 2026-10-05 and the removal was
 * pinned by `projectManagementNoExport.test.tsx`. It was brought back on request,
 * restyled as the Reports page's export is (dark "Export CSV" button in the
 * header, "Export projects" dialog with a Cancel / Download CSV footer), and with
 * the new Organization column. That test is replaced by this one; the two things
 * it also protected -- the filters beside the button, and a page that asks the
 * API for one page of projects, not an export-sized walk -- are still pinned here.
 *
 * The real page is rendered against a real RTK Query store with only `fetch`
 * replaced. `exportToCsv` is a spy that records what it was given and then runs
 * the real writer, with the browser's download plumbing (`URL.createObjectURL`,
 * the anchor click) stubbed to capture the file -- so what is asserted about the
 * file is the file, not just the arguments that were meant to produce it.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

const exportToCsv = vi.hoisted(() => vi.fn());
/** The real `exportToCsv`, so the file that is actually produced can be inspected. */
const realExportToCsv = vi.hoisted(() => ({ fn: undefined as undefined | ((...args: unknown[]) => void) }));

vi.mock('../../dashboard/v2/filters', async (importOriginal) => {
  const original = await importOriginal<typeof import('../../dashboard/v2/filters')>();
  realExportToCsv.fn = original.exportToCsv as (...args: unknown[]) => void;
  return { ...original, exportToCsv };
});
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
import { csvText, EXPORT_COLUMNS } from '../projectExport';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const project = (id: number, over: Record<string, unknown> = {}) => ({
  id,
  project_name: `Project ${id}`,
  description: `About project ${id}`,
  status: { id: 1, name: 'Active', color: '#22C55E' },
  owner: { id: 701, name: 'Owen Owner', email: 'o@example.invalid', role: 'administrator' },
  leader: { id: 801, name: 'Lena Leader', email: 'l@example.invalid', role: 'leader' },
  employees: [
    { id: 11, name: 'Asha Patel', email: 'a@example.invalid', role: 'employee' },
    { id: 12, name: 'Ravi Shah', email: 'r@example.invalid', role: 'employee' },
  ],
  deadline: '2026-12-31',
  billing_type: 'fixed',
  fixed_hours: '100.00',
  category: null,
  organization_id: 1,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
  tasks: [],
  employee_count: 2,
  task_count: 7,
  ...over,
});

describe('Project Management: Export CSV', () => {
  let container: HTMLDivElement;
  let root: Root;
  let projectRequests: URL[];
  /** Every file the page handed to the browser to download. */
  let downloads: Blob[];
  /** What `GET /projects` answers, by the request it is given. */
  let listReply: (url: URL) => Response | Promise<Response>;

  const flush = async () => {
    for (let i = 0; i < 40; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const buttons = () => Array.from(container.querySelectorAll('button'));
  const buttonText = (text: string) => buttons().find((button) => button.textContent?.trim() === text);
  const click = async (element: Element | undefined) => {
    expect(element, 'element to click').toBeTruthy();
    await act(async () => { (element as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    await flush();
  };
  const dialog = () => container.querySelector('[role="dialog"][aria-label="Export projects"]');
  const dialogCheckboxes = () => Array.from(dialog()!.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'));
  const columnBox = (label: string) =>
    dialogCheckboxes().find((box) => box.closest('label')?.textContent?.trim() === label)!;
  const download = () => Array.from(dialog()!.querySelectorAll('button')).find((b) => b.textContent?.includes('Download CSV')
    || b.textContent?.includes('Exporting')) as HTMLButtonElement;
  const exportRequests = () => projectRequests.filter((url) => url.searchParams.get('limit') === '100');
  const lastCall = () => exportToCsv.mock.calls.at(-1) as [string, string[], (string | number)[][], (string | number)[][]];
  const selectValue = async (element: HTMLSelectElement, value: string) => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(element, value);
    await act(async () => { element.dispatchEvent(new Event('change', { bubbles: true })); });
    await flush();
  };

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-06T10:00:00Z')); // 15:30 IST on the 6th
    exportToCsv.mockReset();
    exportToCsv.mockImplementation((...args: unknown[]) => realExportToCsv.fn!(...args));
    downloads = [];
    (URL as unknown as { createObjectURL: (blob: Blob) => string }).createObjectURL = (blob) => { downloads.push(blob); return 'blob:test'; };
    (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = () => undefined;
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    projectRequests = [];
    listReply = () => json({ items: [project(1)], pagination: { page: 1, limit: 20, total: 1, total_pages: 1 } });
    vi.stubGlobal('localStorage', {
      getItem: () => null, setItem: () => undefined, removeItem: () => undefined, clear: () => undefined,
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      if (url.pathname.endsWith('/project-management/metadata')) {
        return json({
          roles: [],
          project_statuses: [{ id: 1, project_status: 'Active', color: '#22C55E' }, { id: 2, project_status: 'On Hold', color: '#F59E0B' }],
          task_statuses: [],
        });
      }
      if (url.pathname.endsWith('/projects') && request.method === 'GET') {
        projectRequests.push(url);
        return listReply(url);
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
    vi.restoreAllMocks();
    delete (URL as unknown as { createObjectURL?: unknown }).createObjectURL;
    delete (URL as unknown as { revokeObjectURL?: unknown }).revokeObjectURL;
    vi.useRealTimers();
  });

  describe('the button', () => {
    it('is there, with the Reports page’s Export CSV styling and download icon', () => {
      const button = buttonText('Export CSV')!;
      expect(button).toBeTruthy();
      expect(button.className).toContain('bg-[#0F172A]');
      expect(button.className).toContain('hover:bg-[#1E293B]');
      expect(button.className).toContain('text-xs');
      expect(button.className).toContain('rounded-lg');
      expect(button.querySelector('svg path')?.getAttribute('d')).toBe('M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3');
    });

    it('sits in the header beside Create Project', () => {
      const labels = buttons().map((button) => button.textContent?.trim());
      expect(labels.indexOf('Export CSV')).toBeGreaterThan(-1);
      expect(labels.indexOf('Export CSV')).toBeLessThan(labels.indexOf('+ Create Project'));
    });

    it('asks the API for nothing extra until the dialog’s Download is pressed', async () => {
      const before = projectRequests.length;
      await click(buttonText('Export CSV'));
      expect(dialog()).not.toBeNull();
      expect(projectRequests).toHaveLength(before);
      expect(exportRequests()).toHaveLength(0);
    });
  });

  describe('the dialog', () => {
    beforeEach(async () => { await click(buttonText('Export CSV')); });

    it('is the Reports dialog’s shell: title, helper line, columns, Cancel and a dark Download CSV', () => {
      expect(dialog()!.querySelector('h2')!.textContent).toBe('Export projects');
      expect(dialog()!.textContent).toContain('Downloads every project matching the filters — not just what is on screen.');
      expect(download().className).toContain('bg-[#0F172A]');
      expect(download().textContent).toContain('Download CSV');
      expect(Array.from(dialog()!.querySelectorAll('button')).some((b) => b.textContent?.trim() === 'Cancel')).toBe(true);
    });

    it('offers every column, all ticked, Organization among them', () => {
      expect(dialogCheckboxes().map((box) => box.closest('label')!.textContent!.trim())).toEqual(EXPORT_COLUMNS.map((c) => c.label));
      expect(EXPORT_COLUMNS.map((c) => c.label)).toContain('Organization');
      expect(dialogCheckboxes().every((box) => box.checked)).toBe(true);
      expect(dialog()!.textContent).toContain('CSV · 11 columns');
    });

    it('closes on Cancel, on the close button and on Escape', async () => {
      await click(Array.from(dialog()!.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Cancel'));
      expect(dialog()).toBeNull();

      await click(buttonText('Export CSV'));
      await click(dialog()!.querySelector('button[aria-label="Close"]')!);
      expect(dialog()).toBeNull();

      await click(buttonText('Export CSV'));
      await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
      expect(dialog()).toBeNull();
      expect(exportToCsv).not.toHaveBeenCalled();
    });

    it('Clear all / Select all, and Download is off while nothing is ticked', async () => {
      await click(Array.from(dialog()!.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Clear all'));
      expect(dialogCheckboxes().every((box) => !box.checked)).toBe(true);
      expect(download().disabled).toBe(true);
      expect(dialog()!.textContent).toContain('CSV · 0 columns');

      await click(Array.from(dialog()!.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Select all'));
      expect(dialogCheckboxes().every((box) => box.checked)).toBe(true);
      expect(download().disabled).toBe(false);
    });
  });

  describe('downloading', () => {
    it('walks every page of the matching projects, 100 at a time, then writes one file', async () => {
      listReply = (url) => {
        const page = Number(url.searchParams.get('page'));
        const limit = Number(url.searchParams.get('limit'));
        if (limit !== 100) return json({ items: [project(1)], pagination: { page: 1, limit, total: 3, total_pages: 1 } });
        return json({
          items: page === 1 ? [project(1), project(2)] : [project(3)],
          pagination: { page, limit: 100, total: 3, total_pages: 2 },
        });
      };
      await click(buttonText('Export CSV'));
      await click(download());

      expect(exportRequests().map((url) => url.searchParams.get('page'))).toEqual(['1', '2']);
      expect(exportToCsv).toHaveBeenCalledTimes(1);
      const [filename, headers, rows, leading] = lastCall();
      expect(filename).toBe('projects_2026-10-06.csv');
      expect(headers).toEqual(EXPORT_COLUMNS.map((c) => c.label));
      expect(rows.map((row) => row[0])).toEqual(['Project 1', 'Project 2', 'Project 3']);
      expect(leading ?? []).toEqual([]); // nothing above the header: the table starts on row 1
      expect(dialog()).toBeNull(); // closed once the file was handed over
    });

    it('writes each project’s values in the table’s own terms', async () => {
      listReply = (url) => json({
        items: [project(1, { category: 'kyle', deadline: '2026-12-31T00:00:00', billing_type: 'free', fixed_hours: null })],
        pagination: { page: 1, limit: Number(url.searchParams.get('limit')), total: 1, total_pages: 1 },
      });
      await click(buttonText('Export CSV'));
      await click(download());

      const [, headers, rows] = lastCall();
      const row = Object.fromEntries(headers.map((h, i) => [h, rows[0][i]]));
      expect(row).toEqual({
        'Project': 'Project 1',
        'Organization': 'Kyle Project',
        'Description': 'About project 1',
        'Status': 'Active',
        'Owner': 'Owen Owner',
        'Leader': 'Lena Leader',
        'Team Members': 'Asha Patel; Ravi Shah',
        'Tasks': 7,
        'Billing': 'Free Time',
        'Created': '2026-09-01',
        'Deadline': '2026-12-31',
      });
    });

    it('Organization is ST Project, Kyle Project, or blank -- never a made-up label', async () => {
      listReply = (url) => json({
        items: [project(1, { category: 'kyle' }), project(2, { category: 'st' }), project(3, { category: null })],
        pagination: { page: 1, limit: Number(url.searchParams.get('limit')), total: 3, total_pages: 1 },
      });
      await click(buttonText('Export CSV'));
      await click(download());
      const column = lastCall()[1].indexOf('Organization');
      expect(lastCall()[2].map((row) => row[column])).toEqual(['Kyle Project', 'ST Project', '']);
    });

    it('unassigned people and a missing deadline read as the table says', async () => {
      listReply = (url) => json({
        items: [project(1, { owner: null, leader: null, employees: [], deadline: null, billing_type: 'non_billing' })],
        pagination: { page: 1, limit: Number(url.searchParams.get('limit')), total: 1, total_pages: 1 },
      });
      await click(buttonText('Export CSV'));
      await click(download());
      const [, headers, rows] = lastCall();
      const cell = (name: string) => rows[0][headers.indexOf(name)];
      expect([cell('Owner'), cell('Leader'), cell('Team Members'), cell('Deadline'), cell('Billing')])
        .toEqual(['Unassigned', 'Unassigned', '', 'No Deadline', 'Non Billing']);
    });

    it('only the ticked columns, in the table’s order', async () => {
      await click(buttonText('Export CSV'));
      await click(columnBox('Leader'));
      await click(columnBox('Description'));
      await click(columnBox('Billing'));
      await click(download());
      expect(lastCall()[1]).toEqual(['Project', 'Organization', 'Status', 'Owner', 'Team Members', 'Tasks', 'Created', 'Deadline']);
      expect(lastCall()[2][0]).toHaveLength(8);
    });

    it('asks for the very filters the table is showing', async () => {
      await selectValue(container.querySelector<HTMLSelectElement>('select[aria-label="Filter by organization"]')!, 'st');
      await selectValue(container.querySelector<HTMLSelectElement>('select[aria-label="Filter by project type"]')!, 'billing');
      await selectValue(container.querySelector<HTMLSelectElement>('select[aria-label="Filter by billing type"]')!, 'fixed');
      await click(buttonText('Export CSV'));
      await click(download());

      const walk = exportRequests()[0];
      expect(walk.searchParams.get('category')).toBe('st');
      expect(walk.searchParams.getAll('billing_type')).toEqual(['fixed']);
    });

    it('asks for both billed kinds when the project type is just Billing', async () => {
      await selectValue(container.querySelector<HTMLSelectElement>('select[aria-label="Filter by project type"]')!, 'billing');
      await click(buttonText('Export CSV'));
      await click(download());

      expect(exportRequests()[0].searchParams.getAll('billing_type')).toEqual(['fixed', 'free']);
    });

    it('asks for no billing filter at all when the project type is left on All', async () => {
      await click(buttonText('Export CSV'));
      await click(download());

      expect(exportRequests()[0].searchParams.has('billing_type')).toBe(false);
    });

    it('asks for the creation-date range the table is showing', async () => {
      await click(buttonText('All Time')); // the date picker's trigger
      await click(buttons().find((button) => button.textContent?.trim() === 'Last 7 days'));
      await click(buttonText('Export CSV'));
      await click(download());

      // The clock is frozen at 15:30 IST on 6 Oct 2026.
      expect(exportRequests()[0].searchParams.get('created_from')).toBe('2026-09-30');
      expect(exportRequests()[0].searchParams.get('created_to')).toBe('2026-10-06');
    });

    it('asks for no date limit when the picker is left on All Time', async () => {
      await click(buttonText('Export CSV'));
      await click(download());

      expect(exportRequests()[0].searchParams.has('created_from')).toBe(false);
      expect(exportRequests()[0].searchParams.has('created_to')).toBe(false);
    });

    it('says so and writes nothing when no project matches', async () => {
      listReply = (url) => json({ items: [], pagination: { page: 1, limit: Number(url.searchParams.get('limit')), total: 0, total_pages: 0 } });
      await click(buttonText('Export CSV'));
      await click(download());
      expect(dialog()!.textContent).toContain('No projects match the current filters, so there is nothing to export.');
      expect(exportToCsv).not.toHaveBeenCalled();
    });

    it('says why when the API refuses, and writes nothing', async () => {
      listReply = (url) => (Number(url.searchParams.get('limit')) === 100
        ? json({ detail: 'Not allowed to list projects.' }, 403)
        : json({ items: [project(1)], pagination: { page: 1, limit: 20, total: 1, total_pages: 1 } }));
      await click(buttonText('Export CSV'));
      await click(download());
      expect(dialog()!.textContent).toContain('Not allowed to list projects.');
      expect(exportToCsv).not.toHaveBeenCalled();
      expect(download().disabled).toBe(false); // it can be tried again
    });

    it('shows Exporting… and locks the dialog while the walk is in flight', async () => {
      let release!: (response: Response) => void;
      listReply = (url) => (Number(url.searchParams.get('limit')) === 100
        ? new Promise<Response>((resolve) => { release = resolve; })
        : json({ items: [project(1)], pagination: { page: 1, limit: 20, total: 1, total_pages: 1 } }));
      await click(buttonText('Export CSV'));
      await click(download());

      expect(download().textContent).toContain('Exporting…');
      expect(download().disabled).toBe(true);
      expect(dialog()!.textContent).toContain('Fetching page 1…');
      expect(dialog()!.querySelector<HTMLButtonElement>('button[aria-label="Close"]')!.disabled).toBe(true);

      await act(async () => { release(json({ items: [project(1)], pagination: { page: 1, limit: 100, total: 1, total_pages: 1 } })); });
      await flush();
      expect(exportToCsv).toHaveBeenCalledTimes(1);
    });
  });

  describe('the file that is downloaded', () => {
    const threeProjects = (url: URL) => json({
      items: [project(1, { category: 'kyle' }), project(2), project(3, { category: 'st' })],
      pagination: { page: 1, limit: Number(url.searchParams.get('limit')), total: 3, total_pages: 1 },
    });
    const exportNow = async () => {
      await click(buttonText('Export CSV'));
      await click(download());
    };
    const lastFile = async () => {
      const blob = downloads.at(-1)!;
      return { text: await blob.text(), bytes: new Uint8Array(await blob.arrayBuffer()) };
    };

    it('is exactly one file, handed to the browser to download', async () => {
      listReply = threeProjects;
      await exportNow();
      expect(downloads).toHaveLength(1);
    });

    it('starts with the header row, then one line per project -- nothing above the table', async () => {
      listReply = threeProjects;
      await exportNow();
      const lines = (await lastFile()).text.split('\n');

      expect(lines[0]).toBe(EXPORT_COLUMNS.map((column) => column.label).join(','));
      expect(lines).toHaveLength(4); // header + 3 projects, no summary lines, no blank lines
      expect(lines[1]).toBe('Project 1,Kyle Project,About project 1,Active,Owen Owner,Lena Leader,Asha Patel; Ravi Shah,7,100.00 Hours,2026-09-01,2026-12-31');
      expect(lines[2].startsWith('Project 2,,')).toBe(true);
      expect(lines[3].startsWith('Project 3,ST Project,')).toBe(true);
    });

    it('has the data on the first screen of a spreadsheet: row 1 is the header, row 2 the first project', async () => {
      listReply = threeProjects;
      await exportNow();
      const lines = (await lastFile()).text.split('\n');
      expect(lines[0].split(',')[0]).toBe('Project');
      expect(lines[1].split(',')[0]).toBe('Project 1');
    });

    it('carries only the ticked columns, header and rows alike', async () => {
      listReply = threeProjects;
      await click(buttonText('Export CSV'));
      await click(Array.from(dialog()!.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Clear all'));
      await click(columnBox('Project'));
      await click(columnBox('Tasks'));
      await click(download());
      expect((await lastFile()).text.split('\n')).toEqual(['Project,Tasks', 'Project 1,7', 'Project 2,7', 'Project 3,7']);
    });

    it('is UTF-8 with a byte-order mark, so Excel reads names with accents correctly', async () => {
      listReply = (url) => json({
        items: [project(1, { project_name: 'Café Münchën 東京' })],
        pagination: { page: 1, limit: Number(url.searchParams.get('limit')), total: 1, total_pages: 1 },
      });
      await exportNow();
      const { text, bytes } = await lastFile();
      expect(Array.from(bytes.slice(0, 3))).toEqual([0xef, 0xbb, 0xbf]);
      expect(text).toContain('Café Münchën 東京');
    });

    it('quotes a cell holding commas, quotes or line breaks, so the row still has one cell per column', async () => {
      listReply = (url) => json({
        items: [project(1, { description: 'Phase 1, "beta"\nsecond line' })],
        pagination: { page: 1, limit: Number(url.searchParams.get('limit')), total: 1, total_pages: 1 },
      });
      await exportNow();
      const { text } = await lastFile();
      expect(text).toContain('"Phase 1, ""beta""\nsecond line"');
      expect(text.split('\n')[0]).toBe(EXPORT_COLUMNS.map((column) => column.label).join(','));
    });

    it('writes no file at all when nothing matches', async () => {
      listReply = (url) => json({ items: [], pagination: { page: 1, limit: Number(url.searchParams.get('limit')), total: 0, total_pages: 0 } });
      await exportNow();
      expect(downloads).toHaveLength(0);
    });
  });

  describe('spreadsheet formulas', () => {
    it('a cell that would be evaluated is made inert; everything else is untouched', () => {
      for (const risky of ['=SUM(A1:A9)', '+1+1', '-2+3', '@cmd', '=HYPERLINK("http://example.invalid","x")', '\tpadded', '\rreturn']) {
        expect(csvText(risky), risky).toBe(`'${risky}`);
      }
      for (const safe of ['Website Redesign', 'A = B', 'R&D - phase 2', '2026-12-31', '', '  =leading space']) {
        expect(csvText(safe), safe).toBe(safe);
      }
    });

    it('applies to every text column of the file, not only the name', async () => {
      listReply = (url) => json({
        items: [project(1, {
          project_name: '=1+1',
          description: '+cmd|calc',
          owner: { id: 1, name: '@owner', email: '', role: 'administrator' },
          leader: { id: 2, name: '-leader', email: '', role: 'leader' },
          employees: [{ id: 3, name: '=member', email: '', role: 'employee' }],
        })],
        pagination: { page: 1, limit: Number(url.searchParams.get('limit')), total: 1, total_pages: 1 },
      });
      await click(buttonText('Export CSV'));
      await click(download());
      const [, headers, rows] = lastCall();
      const cell = (name: string) => rows[0][headers.indexOf(name)];
      expect([cell('Project'), cell('Description'), cell('Owner'), cell('Leader'), cell('Team Members')])
        .toEqual(["'=1+1", "'+cmd|calc", "'@owner", "'-leader", "'=member"]);
      expect(cell('Tasks')).toBe(7); // numbers stay numbers
    });
  });

  describe('what the page still does', () => {
    it('asks the API for one page of projects on load, never for an export-sized walk', () => {
      expect(projectRequests.length).toBeGreaterThan(0);
      expect(projectRequests.every((url) => url.searchParams.get('limit') !== '100')).toBe(true);
    });

    it('keeps the filters that sit beside the button', () => {
      expect(container.querySelector('input[placeholder*="earch"]')).not.toBeNull();
      expect(container.querySelector('select[aria-label="Filter by project type"]')).not.toBeNull();
      expect(container.querySelector('select[aria-label="Filter by organization"]')).not.toBeNull();
      expect(container.textContent).toContain('All Statuses');
      expect(container.textContent).toContain('Columns');
    });

    it('has no Members filter, and never asks the API to narrow by member', async () => {
      // The "All members" picker was removed from this toolbar on request.
      expect(container.textContent).not.toContain('All members');
      for (const url of projectRequests) expect(url.searchParams.has('employee_ids')).toBe(false);

      await click(buttonText('Export CSV'));
      await click(download());
      expect(exportRequests().length).toBeGreaterThan(0);
      for (const url of exportRequests()) expect(url.searchParams.has('employee_ids')).toBe(false);
    });
  });
});
