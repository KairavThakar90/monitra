// @vitest-environment jsdom
/**
 * Both export dialogs write one file: the timesheet.
 *
 * The administrator's Reports dialog used to offer a second, ranked-table
 * format with a column picker; the client portal's wrote a per-page table.
 * Both are now the same timesheet -- one row per member x project x to-do, one
 * column per day -- with no format choice and no column picker. These tests
 * render the real dialogs against a real store with only `fetch` stubbed, and
 * capture the file the download button writes.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

const currentUser = { id: 1, role_name: 'administrator', name: 'Admin' };
vi.mock('../../auth/authContext', () => ({ useAuth: () => ({ currentUser }) }));

// The file is captured instead of downloaded.
const written: { filename: string; headers: string[]; rows: (string | number)[][]; quoteAll: boolean }[] = [];
vi.mock('../../dashboard/v2/filters', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../dashboard/v2/filters')>();
  return {
    ...actual,
    exportToCsv: (filename: string, headers: string[], rows: (string | number)[][], _lead: unknown, quoteAll = false) => {
      written.push({ filename, headers, rows, quoteAll });
    },
  };
});

import { ClientExportDialog } from '../ClientExportDialog';
import { ExportDialog } from '../../dashboard/v2/ExportDialog';

const RANGE = { preset: 'custom' as const, from: '2026-09-29', to: '2026-09-30' };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

describe('export dialogs', () => {
  let container: HTMLDivElement;
  let root: Root;
  let requests: string[];
  let timesheet: unknown;

  const mount = async (node: React.ReactElement) => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(<Provider store={store}>{node}</Provider>);
    });
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const download = () =>
    Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.includes('Download CSV')) as HTMLButtonElement;
  const press = async (button: HTMLButtonElement) => {
    await act(async () => { button.click(); });
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    written.length = 0;
    requests = [];
    timesheet = {
      start_date: RANGE.from, end_date: RANGE.to, organization: 'Acme Co',
      permissions: { share_member_details: true, share_screenshots: false, share_tasks: true, share_timing: true, share_billing: false },
      items: [
        { date: '2026-09-29', member_id: 5, member_name: 'Alice', project_id: 1, project_name: 'Apollo', task_id: 9, task_name: 'Guidance', tracked_seconds: 3600 },
        { date: '2026-09-30', member_id: 5, member_name: 'Alice', project_id: 1, project_name: 'Apollo', task_id: 9, task_name: 'Guidance', tracked_seconds: 1800 },
      ],
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      requests.push(url);
      if (url.includes('/clients/me/timesheet')) return json(timesheet);
      if (url.includes('/reports/detailed-logs')) {
        return json({
          items: [
            { id: 'a', date: '2026-09-29', member_id: 5, member_name: 'Alice', project_id: 1, project_name: 'Apollo', task_id: 9, task_name: 'Guidance', tracked_seconds: 7200, tracked_hours: 2, tracked_time: '02:00:00' },
          ],
          pagination: { page: 1, limit: 200, total: 1, total_pages: 1 },
        });
      }
      return json({ member: { organization: { name: 'Acme Co' } } });
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

  it('offers the administrator only the timesheet: no format choice, no column picker', async () => {
    await mount(
      <ExportDialog open onClose={() => {}} range={RANGE} queryParams={{ start_date: RANGE.from, end_date: RANGE.to } as never} />,
    );
    const text = container.textContent ?? '';
    expect(text).toContain('Timesheet');
    expect(text).not.toContain('Project-Wise');
    expect(text).not.toContain('Select all');
    expect(text).not.toContain('Columns');
    expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(0);
  });

  it('writes the administrator a timesheet with one column per day', async () => {
    await mount(
      <ExportDialog open onClose={() => {}} range={RANGE} queryParams={{ start_date: RANGE.from, end_date: RANGE.to } as never} />,
    );
    await press(download());
    expect(written).toHaveLength(1);
    const file = written[0];
    expect(file.filename).toBe('timesheet_report_2026-09-29_to_2026-09-30.csv');
    expect(file.headers).toEqual([
      'Member', 'Organization', 'Time Zone', 'Projects', 'Task Summary', '2026-09-29', '2026-09-30', 'Total worked',
    ]);
    expect(file.rows[0].slice(0, 5)).toEqual(['Alice', 'Acme Co', 'Asia/Kolkata', 'Apollo', 'Guidance']);
    expect(file.quoteAll).toBe(true);
  });

  it('offers the client the same timesheet and nothing else', async () => {
    await mount(
      <ClientExportDialog open onClose={() => {}} range={RANGE} selectedProjectIds={[]} selectedMemberIds={[]} />,
    );
    const text = container.textContent ?? '';
    expect(text).toContain('Timesheet');
    expect(text).not.toContain('Select all');
    expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(0);
  });

  it('writes the client the identical file shape, from the portal endpoint and range', async () => {
    await mount(
      <ClientExportDialog open onClose={() => {}} range={RANGE} selectedProjectIds={['1']} selectedMemberIds={[]} defaultReport="tasks" />,
    );
    const call = requests.find((url) => url.includes('/clients/me/timesheet'))!;
    expect(call).toContain('start_date=2026-09-29');
    expect(call).toContain('end_date=2026-09-30');
    expect(call).toContain('project_ids=1');

    await press(download());
    const file = written[0];
    expect(file.headers).toEqual([
      'Member', 'Organization', 'Time Zone', 'Projects', 'Task Summary', '2026-09-29', '2026-09-30', 'Total worked',
    ]);
    // 1:00:00 on the 29th, 0:30:00 on the 30th, 1:30:00 in all.
    expect(file.rows[0]).toEqual(['Alice', 'Acme Co', 'Asia/Kolkata', 'Apollo', 'Guidance', '1:00:00', '0:30:00', '1:30:00']);
    expect(file.quoteAll).toBe(true);
  });

  it('prints "Not shared" for a member or to-do the admin withheld, never a guess', async () => {
    timesheet = {
      ...(timesheet as object),
      permissions: { share_member_details: false, share_screenshots: false, share_tasks: false, share_timing: true, share_billing: false },
      items: [{ date: '2026-09-29', member_id: 5, member_name: null, project_id: 1, project_name: 'Apollo', task_id: 9, task_name: null, tracked_seconds: 60 }],
    };
    await mount(
      <ClientExportDialog open onClose={() => {}} range={RANGE} selectedProjectIds={[]} selectedMemberIds={[]} />,
    );
    await press(download());
    expect(written[0].rows[0].slice(0, 5)).toEqual(['Not shared', 'Acme Co', 'Asia/Kolkata', 'Apollo', 'Not shared']);
  });

  it('does not let a client export when time tracking is not shared', async () => {
    timesheet = {
      ...(timesheet as object),
      permissions: { share_member_details: true, share_screenshots: false, share_tasks: true, share_timing: false, share_billing: false },
      items: [],
    };
    await mount(
      <ClientExportDialog open onClose={() => {}} range={RANGE} selectedProjectIds={[]} selectedMemberIds={[]} />,
    );
    expect(container.textContent).toContain('Time tracking is not shared');
    expect(download().disabled).toBe(true);
    expect(written).toHaveLength(0);
  });

  it('says so, and writes nothing, when the range holds no tracked time', async () => {
    timesheet = { ...(timesheet as object), items: [] };
    await mount(
      <ClientExportDialog open onClose={() => {}} range={RANGE} selectedProjectIds={[]} selectedMemberIds={[]} />,
    );
    await press(download());
    expect(container.textContent).toContain('nothing to export');
    expect(written).toHaveLength(0);
  });
});
