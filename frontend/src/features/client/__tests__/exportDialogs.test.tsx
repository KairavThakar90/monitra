// @vitest-environment jsdom
/**
 * Both export dialogs write one file: the timesheet.
 *
 * The administrator's Reports dialog used to offer a second, ranked-table
 * format with a column picker; the client portal's wrote a per-page table.
 * Both write one timesheet with no format choice and no column picker. The
 * administrator's has the **date in a column** -- one row per day for each
 * member x project x to-do, with the time worked and the average activity; the
 * client's keeps one column per day, because the portal's data has no activity
 * figures. These tests render the real dialogs against a real store with only
 * `fetch` stubbed, and capture the file the download button writes.
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
  /** What `/reports/detailed-logs` answers with: one session of 2h at 75% activity unless a test says otherwise. */
  let detailedLogs: Record<string, unknown>[];

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
    // Wait until the button is usable rather than for a fixed number of ticks: on a
    // loaded machine (the whole suite running in parallel) the dialog's data may
    // still be on its way, and a click on a disabled button writes nothing.
    for (let attempt = 0; attempt < 80 && (download() ?? button).disabled; attempt += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
    await act(async () => { (download() ?? button).click(); });
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    written.length = 0;
    requests = [];
    detailedLogs = [
      { id: 'a', date: '2026-09-29', member_id: 5, member_name: 'Alice', project_id: 1, project_name: 'Apollo', task_id: 9, task_name: 'Guidance', tracked_seconds: 7200, tracked_hours: 2, tracked_time: '02:00:00', activity_percentage: 75 },
    ];
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
          items: detailedLogs,
          pagination: { page: 1, limit: 200, total: detailedLogs.length, total_pages: 1 },
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

  const adminExport = async () => {
    await mount(
      <ExportDialog open onClose={() => {}} range={RANGE} queryParams={{ start_date: RANGE.from, end_date: RANGE.to } as never} />,
    );
    await press(download());
    return written[0];
  };
  const session = (over: Record<string, unknown>) => ({
    id: 's', date: '2026-09-29', member_id: 5, member_name: 'Alice', project_id: 1, project_name: 'Apollo', task_id: 9,
    task_name: 'Guidance', tracked_seconds: 3600, tracked_hours: 1, tracked_time: '01:00:00', activity_percentage: null, ...over,
  });

  const DAILY_HEADERS = ['Date', 'Member', 'Organization', 'Time Zone', 'Projects', 'Task Summary', 'Total worked', 'Activity %'];

  it('writes the administrator a timesheet with the date in a column, one row per day', async () => {
    const file = await adminExport();
    expect(written).toHaveLength(1);
    expect(file.filename).toBe('timesheet_report_2026-09-29_to_2026-09-30.csv');
    expect(file.headers).toEqual(DAILY_HEADERS);
    // 2:00:00 on the 29th at 75% activity.
    expect(file.rows).toEqual([['29-09-2026', 'Alice', 'Acme Co', 'Asia/Kolkata', 'Apollo', 'Guidance', '2:00:00', '75%']]);
    expect(file.quoteAll).toBe(true);
  });

  it('has no column per day: a date is a row, not a heading', async () => {
    detailedLogs = [
      session({ date: '2026-09-29', tracked_seconds: 3600 }),
      session({ id: 'b', date: '2026-09-30', tracked_seconds: 1800 }),
    ];
    const file = await adminExport();
    expect(file.headers).toEqual(DAILY_HEADERS);
    expect(file.headers).not.toContain('2026-09-29');
    expect(file.headers).not.toContain('2026-09-30');
    // The same to-do on two days is two rows, each with its own date and time.
    expect(file.rows.map((row) => [row[0], row[6]])).toEqual([['29-09-2026', '1:00:00'], ['30-09-2026', '0:30:00']]);
  });

  it('lists the days oldest first, whoever worked them', async () => {
    detailedLogs = [
      session({ id: 'a', date: '2026-09-30', member_id: 5, member_name: 'Alice' }),
      session({ id: 'b', date: '2026-09-29', member_id: 6, member_name: 'Zed' }),
    ];
    const file = await adminExport();
    expect(file.rows.map((row) => [row[0], row[1]])).toEqual([['29-09-2026', 'Zed'], ['30-09-2026', 'Alice']]);
  });

  it('writes no row for a day nobody worked', async () => {
    detailedLogs = [session({ date: '2026-09-29' })];
    const file = await adminExport();
    // The range covers two days; only one has time on it.
    expect(file.rows).toHaveLength(1);
    expect(file.rows.flat()).not.toContain('30-09-2026');
    expect(file.rows.flat()).not.toContain('0:00:00');
  });

  it('weights a day’s activity by the time behind each session', async () => {
    detailedLogs = [
      session({ id: 'a', tracked_seconds: 3600, activity_percentage: 50 }),
      session({ id: 'b', tracked_seconds: 1800, activity_percentage: 80 }),
    ];
    const file = await adminExport();
    expect(file.rows).toHaveLength(1);
    // (50 x 3600 + 80 x 1800) / 5400 = 60
    expect(file.rows[0].slice(-2)).toEqual(['1:30:00', '60%']);
  });

  it('gives each member and each to-do its own activity, never a blend', async () => {
    detailedLogs = [
      session({ id: 'a', tracked_seconds: 3600, activity_percentage: 90 }),
      session({ id: 'b', task_id: 10, task_name: 'Review', tracked_seconds: 3600, activity_percentage: 10 }),
      session({ id: 'c', member_id: 6, member_name: 'Bob', tracked_seconds: 3600, activity_percentage: 40 }),
    ];
    const file = await adminExport();
    const activityOf = (member: string, task: string) =>
      file.rows.find((row) => row[1] === member && row[5] === task)!.slice(-1)[0];
    expect(activityOf('Alice', 'Guidance')).toBe('90%');
    expect(activityOf('Alice', 'Review')).toBe('10%');
    expect(activityOf('Bob', 'Guidance')).toBe('40%');
  });

  it('leaves the activity blank, not 0%, for time that has no activity figure (such as a manual entry)', async () => {
    detailedLogs = [session({ id: 'mte-1', activity_percentage: null })];
    const file = await adminExport();
    expect(file.rows[0]).toEqual(['29-09-2026', 'Alice', 'Acme Co', 'Asia/Kolkata', 'Apollo', 'Guidance', '1:00:00', '']);
  });

  it('does not let a manual entry drag a measured average down', async () => {
    detailedLogs = [
      session({ id: 'te-1', tracked_seconds: 3600, activity_percentage: 70 }),
      session({ id: 'mte-2', tracked_seconds: 7200, activity_percentage: null }),
    ];
    const file = await adminExport();
    // All 3 hours are in the total, but only the measured hour has an activity figure.
    expect(file.rows[0].slice(-2)).toEqual(['3:00:00', '70%']);
  });

  it('prints a real 0% as 0%, because "no activity" is an answer and "not measured" is not', async () => {
    detailedLogs = [session({ tracked_seconds: 3600, activity_percentage: 0 })];
    const file = await adminExport();
    expect(file.rows[0].slice(-1)).toEqual(['0%']);
  });

  it('tells the administrator the date is a column and the file carries activity', async () => {
    await mount(
      <ExportDialog open onClose={() => {}} range={RANGE} queryParams={{ start_date: RANGE.from, end_date: RANGE.to } as never} />,
    );
    expect(container.textContent).toContain('One row per day');
    expect(container.textContent).toContain('date in its own column');
    expect(container.textContent).toContain('activity');
  });

  it('says how many days the range covers instead of promising a column for each', async () => {
    await mount(
      <ExportDialog open onClose={() => {}} range={RANGE} queryParams={{ start_date: RANGE.from, end_date: RANGE.to } as never} />,
    );
    expect(container.textContent).toContain('CSV · 2 days selected');
    expect(container.textContent).not.toContain('day column');
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
