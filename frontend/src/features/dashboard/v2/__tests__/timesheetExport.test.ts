/**
 * The two timesheet layouts built from `/reports/detailed-logs`.
 *
 * - The **day-per-column** layout (one column for each day in the range) is the
 *   client portal's. It is pinned to a real exported file -- two rows of the
 *   sample the product owner shared, `timesheet_report_2026-09-30_to_2026-10-06`
 *   -- so it cannot drift.
 * - The **one-row-per-day** layout is the administrator's: the date is a column
 *   (as in the spreadsheet the product owner shared), every member x project x
 *   to-do gets one row per day it was worked, and each row carries that day's
 *   average activity. The same two sample rows are re-expressed in it here.
 */
import { describe, expect, it } from 'vitest';

import type { DetailedLogItem } from '../../../../store/api/reportsApi';
import {
  averageActivity,
  buildDailyRows,
  buildTimesheetRows,
  dailyBody,
  dailyHeaders,
  datesInRange,
  formatActivity,
  formatClock,
  formatSheetDate,
  tallyActivity,
  timesheetBody,
  timesheetHeaders,
} from '../timesheetExport';

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
  tracked_seconds: 0,
  tracked_hours: 0,
  tracked_time: '',
  activity_percentage: null,
  ...over,
});

const RANGE = datesInRange('2026-09-30', '2026-10-06');
const AKSHAR = [
  log({ date: '2026-10-02', tracked_seconds: 2 * 3600 + 9 * 60 + 34 }),
  log({ id: 'te-2', date: '2026-10-06', tracked_seconds: 14 * 60 + 37 }),
];
const HARDIK = {
  member_id: 2, member_name: 'Hardik Raval', project_id: 20, project_name: 'WFPM-MONITRA-V1', task_id: 200, task_name: 'monitra-01-1104',
};

describe('the day-per-column layout (the client portal’s) is unchanged', () => {
  it('has the sample’s seven day columns, 30 Sep to 6 Oct', () => {
    expect(RANGE).toEqual([
      '2026-09-30', '2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04', '2026-10-05', '2026-10-06',
    ]);
  });

  it('starts with the sample’s header, in the sample’s order, and has no date or activity column', () => {
    expect(timesheetHeaders(RANGE)).toEqual([
      'Member', 'Organization', 'Time Zone', 'Projects', 'Task Summary',
      '2026-09-30', '2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04', '2026-10-05', '2026-10-06',
      'Total worked',
    ]);
  });

  it('writes a sample row exactly as the sample has it (Akshar Solanki, 2:09:34 + 0:14:37)', () => {
    expect(timesheetBody(buildTimesheetRows(AKSHAR), RANGE, '', 'Asia/Kolkata')).toEqual([[
      'Akshar Solanki', '', 'Asia/Kolkata', 'CVIN – Hubstaff – Replit App', 'Move New Feature to Live Build',
      '0:00:00', '0:00:00', '2:09:34', '0:00:00', '0:00:00', '0:00:00', '0:14:37', '2:24:11',
    ]]);
  });

  it('writes another sample row exactly (Hardik Raval, 1:21:19 + 0:02:10)', () => {
    const rows = buildTimesheetRows([
      log({ ...HARDIK, date: '2026-10-01', tracked_seconds: 3600 + 21 * 60 + 19 }),
      log({ ...HARDIK, id: 'te-2', date: '2026-10-02', tracked_seconds: 2 * 60 + 10 }),
    ]);
    expect(timesheetBody(rows, RANGE, '', 'Asia/Kolkata')[0]).toEqual([
      'Hardik Raval', '', 'Asia/Kolkata', 'WFPM-MONITRA-V1', 'monitra-01-1104',
      '0:00:00', '1:21:19', '0:02:10', '0:00:00', '0:00:00', '0:00:00', '0:00:00', '1:23:29',
    ]);
  });

  it('carries no activity figure even when the logs have one', () => {
    const rows = buildTimesheetRows([log({ tracked_seconds: 60, activity_percentage: 55 })]);
    expect(timesheetHeaders(RANGE)).toHaveLength(13);
    expect(timesheetBody(rows, RANGE, '', 'Asia/Kolkata')[0]).toHaveLength(13);
  });
});

describe('the one-row-per-day layout (the administrator’s)', () => {
  describe('the Date column', () => {
    it('is the first column, ahead of the member', () => {
      expect(dailyHeaders()[0]).toBe('Date');
      expect(dailyHeaders()).toEqual([
        'Date', 'Member', 'Organization', 'Time Zone', 'Projects', 'Task Summary', 'Total worked', 'Activity %',
      ]);
    });

    it('has no column per day: the header does not grow with the range', () => {
      expect(dailyHeaders()).toHaveLength(8);
      for (const day of RANGE) expect(dailyHeaders()).not.toContain(day);
    });

    it('writes the date as day-month-year, the way the shared spreadsheet shows it', () => {
      expect(formatSheetDate('2026-09-28')).toBe('28-09-2026');
      expect(formatSheetDate('2026-10-06')).toBe('06-10-2026');
      expect(formatSheetDate('2027-01-01')).toBe('01-01-2027');
    });

    it('passes a value that is not a plain date through untouched rather than guessing', () => {
      expect(formatSheetDate('')).toBe('');
      expect(formatSheetDate('28/09/2026')).toBe('28/09/2026');
      expect(formatSheetDate('not a date')).toBe('not a date');
    });

    it('takes the date the backend already bucketed, even when it arrives with a time on it', () => {
      const rows = buildDailyRows([log({ date: '2026-10-06T00:00:00', tracked_seconds: 60 })]);
      expect(rows[0].date).toBe('2026-10-06');
      expect(dailyBody(rows, '', 'Asia/Kolkata')[0][0]).toBe('06-10-2026');
    });
  });

  describe('one row per day', () => {
    it('turns the sample’s one wide row into one row for each day it was worked (Akshar Solanki)', () => {
      expect(dailyBody(buildDailyRows(AKSHAR), '', 'Asia/Kolkata')).toEqual([
        ['02-10-2026', 'Akshar Solanki', '', 'Asia/Kolkata', 'CVIN – Hubstaff – Replit App', 'Move New Feature to Live Build', '2:09:34', ''],
        ['06-10-2026', 'Akshar Solanki', '', 'Asia/Kolkata', 'CVIN – Hubstaff – Replit App', 'Move New Feature to Live Build', '0:14:37', ''],
      ]);
    });

    it('does the same for another sample row (Hardik Raval)', () => {
      const rows = buildDailyRows([
        log({ ...HARDIK, date: '2026-10-01', tracked_seconds: 3600 + 21 * 60 + 19 }),
        log({ ...HARDIK, id: 'te-2', date: '2026-10-02', tracked_seconds: 2 * 60 + 10 }),
      ]);
      expect(dailyBody(rows, '', 'Asia/Kolkata').map((row) => [row[0], row[6]])).toEqual([
        ['01-10-2026', '1:21:19'],
        ['02-10-2026', '0:02:10'],
      ]);
    });

    it('writes no row for a day nobody worked, not a row of zeros', () => {
      const rows = buildDailyRows(AKSHAR);
      expect(rows).toHaveLength(2);
      expect(dailyBody(rows, '', 'Asia/Kolkata').flat()).not.toContain('0:00:00');
    });

    it('adds up two sessions on the same day for the same to-do into one row', () => {
      const rows = buildDailyRows([
        log({ tracked_seconds: 3600 }),
        log({ id: 'te-2', tracked_seconds: 1800 }),
      ]);
      expect(rows).toHaveLength(1);
      expect(formatClock(rows[0].totalSeconds)).toBe('1:30:00');
    });

    it('keeps the same to-do on different days as different rows', () => {
      const rows = buildDailyRows([
        log({ date: '2026-10-02', tracked_seconds: 100 }),
        log({ id: 'te-2', date: '2026-10-03', tracked_seconds: 200 }),
      ]);
      expect(rows.map((row) => [row.date, row.totalSeconds])).toEqual([['2026-10-02', 100], ['2026-10-03', 200]]);
    });

    it('keeps different members, projects and to-dos on the same day apart', () => {
      const rows = buildDailyRows([
        log({ tracked_seconds: 10 }),
        log({ id: 'a', task_id: 101, task_name: 'Other task', tracked_seconds: 20 }),
        log({ id: 'b', project_id: 11, project_name: 'Other project', tracked_seconds: 30 }),
        log({ id: 'c', member_id: 9, member_name: 'Someone Else', tracked_seconds: 40 }),
      ]);
      expect(rows).toHaveLength(4);
      expect(rows.map((row) => row.totalSeconds).sort((a, b) => a - b)).toEqual([10, 20, 30, 40]);
    });

    it('keeps two different projects that share a name apart, by id', () => {
      const rows = buildDailyRows([
        log({ project_id: 10, project_name: 'Same Name', tracked_seconds: 10 }),
        log({ id: 'b', project_id: 11, project_name: 'Same Name', tracked_seconds: 20 }),
      ]);
      expect(rows).toHaveLength(2);
    });

    it('still gives an entry with no project or to-do its own honest row', () => {
      const rows = buildDailyRows([log({ project_id: null, project_name: null, task_id: null, task_name: null, tracked_seconds: 5 })]);
      expect(rows).toHaveLength(1);
      expect([rows[0].projectName, rows[0].taskName]).toEqual(['No project', 'No to-do']);
    });

    it('fills Organization and Time Zone on every row, from the caller', () => {
      const body = dailyBody(buildDailyRows(AKSHAR), 'Acme Co', 'Asia/Kolkata');
      for (const row of body) expect([row[2], row[3]]).toEqual(['Acme Co', 'Asia/Kolkata']);
    });

    it('gives every row exactly as many cells as the header has', () => {
      const body = dailyBody(buildDailyRows(AKSHAR), '', 'Asia/Kolkata');
      for (const row of body) expect(row).toHaveLength(dailyHeaders().length);
    });
  });

  describe('the order of the rows', () => {
    it('is oldest day first, so the sheet reads as a calendar', () => {
      const rows = buildDailyRows([
        log({ date: '2026-10-06', tracked_seconds: 1 }),
        log({ id: 'b', date: '2026-09-30', tracked_seconds: 1 }),
        log({ id: 'c', date: '2026-10-02', tracked_seconds: 1 }),
      ]);
      expect(rows.map((row) => row.date)).toEqual(['2026-09-30', '2026-10-02', '2026-10-06']);
    });

    it('sorts across a month boundary by the real date, not the printed day number', () => {
      const rows = buildDailyRows([
        log({ date: '2026-10-01', tracked_seconds: 1 }),
        log({ id: 'b', date: '2026-09-30', tracked_seconds: 1 }),
      ]);
      // Printed as 01-10-2026 and 30-09-2026, "01" would sort first as text.
      expect(dailyBody(rows, '', 'Asia/Kolkata').map((row) => row[0])).toEqual(['30-09-2026', '01-10-2026']);
    });

    it('goes by member, then project, then to-do within a day', () => {
      const rows = buildDailyRows([
        log({ member_id: 2, member_name: 'Zed', project_name: 'A', task_name: 'a', tracked_seconds: 1 }),
        log({ id: 'b', member_id: 1, member_name: 'Amy', project_id: 12, project_name: 'B', task_id: 2, task_name: 'b', tracked_seconds: 1 }),
        log({ id: 'c', member_id: 1, member_name: 'Amy', project_id: 11, project_name: 'A', task_id: 3, task_name: 'z', tracked_seconds: 1 }),
        log({ id: 'd', member_id: 1, member_name: 'Amy', project_id: 11, project_name: 'A', task_id: 4, task_name: 'a', tracked_seconds: 1 }),
      ]);
      expect(rows.map((row) => `${row.memberName}|${row.projectName}|${row.taskName}`)).toEqual([
        'Amy|A|a', 'Amy|A|z', 'Amy|B|b', 'Zed|A|a',
      ]);
    });
  });

  describe('the Activity % column', () => {
    const activityOf = (logs: DetailedLogItem[]) => dailyBody(buildDailyRows(logs), '', 'Asia/Kolkata')[0][7];

    it('is the last column, after the time worked', () => {
      expect(dailyHeaders().slice(-2)).toEqual(['Total worked', 'Activity %']);
    });

    it('shows a single session’s figure as a whole percent', () => {
      expect(activityOf([log({ tracked_seconds: 3600, activity_percentage: 64 })])).toBe('64%');
    });

    it('weights each session on the day by its tracked time', () => {
      // (50 x 3600 + 80 x 1800) / 5400 = 60 -- a plain mean of 50 and 80 would say 65.
      expect(activityOf([
        log({ tracked_seconds: 3600, activity_percentage: 50 }),
        log({ id: 'b', tracked_seconds: 1800, activity_percentage: 80 }),
      ])).toBe('60%');
    });

    it('gives each day its own figure, never a blend across days', () => {
      const body = dailyBody(buildDailyRows([
        log({ date: '2026-10-02', tracked_seconds: 3600, activity_percentage: 90 }),
        log({ id: 'b', date: '2026-10-03', tracked_seconds: 3600, activity_percentage: 10 }),
      ]), '', 'Asia/Kolkata');
      expect(body.map((row) => [row[0], row[7]])).toEqual([['02-10-2026', '90%'], ['03-10-2026', '10%']]);
    });

    it('gives each member and each to-do its own figure', () => {
      const rows = buildDailyRows([
        log({ tracked_seconds: 3600, activity_percentage: 90 }),
        log({ id: 'b', task_id: 101, task_name: 'Other task', tracked_seconds: 3600, activity_percentage: 10 }),
        log({ id: 'c', member_id: 9, member_name: 'Someone Else', tracked_seconds: 3600, activity_percentage: 40 }),
      ]);
      const byKey = Object.fromEntries(rows.map((row) => [`${row.memberName}|${row.taskName}`, formatActivity(averageActivity(row.activity))]));
      expect(byKey).toEqual({
        'Akshar Solanki|Move New Feature to Live Build': '90%',
        'Akshar Solanki|Other task': '10%',
        'Someone Else|Move New Feature to Live Build': '40%',
      });
    });

    it('is blank, not 0%, for time with no activity figure (a manual entry)', () => {
      expect(activityOf([log({ id: 'mte-1', tracked_seconds: 3600, activity_percentage: null })])).toBe('');
    });

    it('does not let an unmeasured entry drag a measured average down, while its time still counts', () => {
      const rows = buildDailyRows([
        log({ tracked_seconds: 3600, activity_percentage: 70 }),
        log({ id: 'mte-2', tracked_seconds: 7200, activity_percentage: null }),
      ]);
      expect(rows[0].totalSeconds).toBe(10800);
      expect(formatActivity(averageActivity(rows[0].activity))).toBe('70%');
    });

    it('prints a measured 0% as 0%: no activity is an answer, not measured is not', () => {
      expect(activityOf([log({ tracked_seconds: 600, activity_percentage: 0 })])).toBe('0%');
    });

    it('is not skewed by a session whose time nets out at or below zero after adjustments', () => {
      expect(activityOf([
        log({ tracked_seconds: 3600, activity_percentage: 40 }),
        log({ id: 'b', tracked_seconds: -600, activity_percentage: 100 }),
        log({ id: 'c', tracked_seconds: 0, activity_percentage: 100 }),
      ])).toBe('40%');
    });

    it('ignores a figure that is not a number rather than letting it poison the row', () => {
      expect(activityOf([
        log({ tracked_seconds: 3600, activity_percentage: 60 }),
        log({ id: 'b', tracked_seconds: 3600, activity_percentage: Number.NaN as unknown as number }),
        log({ id: 'c', tracked_seconds: 3600, activity_percentage: '80' as unknown as number }),
      ])).toBe('60%');
    });

    it('rounds to a whole percent and never leaves 0-100', () => {
      expect(formatActivity(63.4)).toBe('63%');
      expect(formatActivity(63.5)).toBe('64%');
      expect(formatActivity(99.6)).toBe('100%');
      expect(formatActivity(120)).toBe('100%');
      expect(formatActivity(-5)).toBe('0%');
      expect(formatActivity(Number.NaN)).toBe('');
      expect(formatActivity(null)).toBe('');
    });

    it('has no average at all for a tally that has seen no measured time', () => {
      const tally = { weighted: 0, seconds: 0 };
      tallyActivity(tally, null, 3600);
      tallyActivity(tally, undefined, 3600);
      expect(averageActivity(tally)).toBeNull();
    });
  });
});
