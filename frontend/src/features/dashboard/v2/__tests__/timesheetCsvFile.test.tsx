// @vitest-environment jsdom
/**
 * The administrator's timesheet as a real file: the layout builders handed to the
 * real CSV writer, and the bytes it produces read back.
 *
 * `exportDialogs.test.tsx` captures what the dialog *passes to* the writer; this
 * checks what the writer *makes of it* -- that the date really is the first
 * column of the first data line, that the header is the first line of the file
 * with nothing above it, that quoting survives awkward names, and that the file
 * starts with the byte-order mark that makes Excel read the dashes in project
 * names as dashes (`CVIN – Hubstaff`) instead of `â€“`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { DetailedLogItem } from '../../../../store/api/reportsApi';
import { exportToCsv } from '../filters';
import { buildDailyRows, dailyBody, dailyHeaders } from '../timesheetExport';

const log = (over: Partial<DetailedLogItem>): DetailedLogItem => ({
  id: 'te-1',
  date: '2026-10-02',
  member_id: 1,
  member_name: 'Akshar Solanki',
  role: 'employee',
  project_id: 10,
  project_name: 'CVIN – Hubstaff – Replit App',
  task_id: 100,
  task_name: 'Move New Feature to Live Build',
  app: null,
  url: null,
  tracked_seconds: 2 * 3600 + 9 * 60 + 34,
  tracked_hours: 2.16,
  tracked_time: '02:09:34',
  activity_percentage: 64,
  ...over,
});

describe('the administrator’s timesheet file, as written', () => {
  let blobs: Blob[];
  const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  let clickSpy: ReturnType<typeof vi.spyOn>;

  const write = (logs: DetailedLogItem[], organization = '') => {
    exportToCsv(
      'timesheet_report_2026-09-30_to_2026-10-06.csv',
      dailyHeaders(),
      dailyBody(buildDailyRows(logs), organization, 'Asia/Kolkata'),
      [],
      true,
    );
    return blobs[blobs.length - 1];
  };
  const textOf = async (blob: Blob) => (await blob.text()).replace(/^\uFEFF/, '');

  beforeEach(() => {
    blobs = [];
    URL.createObjectURL = vi.fn((blob: Blob) => { blobs.push(blob); return 'blob:test'; }) as never;
    URL.revokeObjectURL = vi.fn() as never;
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
  });
  afterEach(() => {
    clickSpy.mockRestore();
    URL.createObjectURL = original.create;
    URL.revokeObjectURL = original.revoke;
  });

  it('puts the header on the first line, the date first in it, with nothing above the table', async () => {
    const lines = (await textOf(write([log({})]))).split('\n');
    expect(lines[0]).toBe('"Date","Member","Organization","Time Zone","Projects","Task Summary","Total worked","Activity %"');
    expect(lines).toHaveLength(2); // header + one day row; no summary or filter lines, no blank line
  });

  it('puts the date in the first cell of each data line, day-month-year', async () => {
    const lines = (await textOf(write([
      log({ date: '2026-10-02' }),
      log({ id: 'te-2', date: '2026-10-06', tracked_seconds: 14 * 60 + 37, activity_percentage: null }),
    ]))).split('\n');
    expect(lines[1]).toBe('"02-10-2026","Akshar Solanki","","Asia/Kolkata","CVIN – Hubstaff – Replit App","Move New Feature to Live Build","2:09:34","64%"');
    expect(lines[2]).toBe('"06-10-2026","Akshar Solanki","","Asia/Kolkata","CVIN – Hubstaff – Replit App","Move New Feature to Live Build","0:14:37",""');
  });

  it('has the same number of cells on every line, so the columns line up in a spreadsheet', async () => {
    const lines = (await textOf(write([
      log({ date: '2026-10-02' }),
      log({ id: 'b', date: '2026-10-03', member_id: 2, member_name: 'Hardik Raval' }),
    ]))).split('\n');
    // Every cell is quoted and none of these contains a quoted comma, so quote pairs count cells.
    const cells = (line: string) => (line.match(/"/g) ?? []).length / 2;
    expect(new Set(lines.map(cells)).size).toBe(1);
    expect(cells(lines[0])).toBe(8);
  });

  it('keeps a quotation mark inside a name intact, doubled the way CSV requires', async () => {
    const lines = (await textOf(write([
      log({ task_name: 'Create the "Why choose us" section' }),
    ]))).split('\n');
    expect(lines[1]).toContain('"Create the ""Why choose us"" section"');
  });

  it('keeps a comma inside a name in one cell, not two', async () => {
    const lines = (await textOf(write([log({ project_name: 'Acme, Inc. – Website' })]))).split('\n');
    expect(lines[1]).toContain('"Acme, Inc. – Website"');
  });

  it('starts with the byte-order mark, so Excel reads the file as UTF-8', async () => {
    const bytes = new Uint8Array(await write([log({})]).arrayBuffer());
    expect([...bytes.slice(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
  });

  it('round-trips an en dash and a curly apostrophe exactly', async () => {
    const decoded = new TextDecoder('utf-8').decode(
      new Uint8Array(await write([log({ task_name: 'Polymeruk-Seo & PPC Activity’s' })]).arrayBuffer()),
    );
    expect(decoded).toContain('CVIN – Hubstaff – Replit App');
    expect(decoded).toContain('Polymeruk-Seo & PPC Activity’s');
    expect(decoded).not.toContain('â€“');
  });

  it('is one file, handed to the browser as text/csv', async () => {
    write([log({})]);
    expect(blobs).toHaveLength(1);
    expect(blobs[0].type).toContain('text/csv');
    expect(clickSpy).toHaveBeenCalledTimes(1);
  });
});
