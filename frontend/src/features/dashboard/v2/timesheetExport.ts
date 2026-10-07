import type { DetailedLogItem } from "../../../store/api/reportsApi";

/**
 * Builds the "timesheet" shape of the export: one row per
 * member x project x to-do, one column per calendar day in the range, and a
 * row total.
 *
 * The source is `/reports/detailed-logs`, which is the only endpoint that
 * returns tracked time at (date, member, project, task) grain. Every other
 * report endpoint has already collapsed one of those axes, so a pivot built
 * from them would have to guess how to split a total back across days — this
 * one never guesses.
 *
 * A day cell of `0:00:00` is a real measurement: the row existed in the range
 * and nothing was tracked against it that day. Cells are never blank, because
 * the grid is dense by construction — every row carries every day.
 */

/** Every calendar date from `from` to `to`, inclusive, as `YYYY-MM-DD`. */
export const datesInRange = (from: string, to: string): string[] => {
  const parse = (iso: string) => {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, (m || 1) - 1, d || 1));
  };
  const isoOf = (date: Date) => date.toISOString().slice(0, 10);

  const start = parse(from);
  const end = parse(to);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || start > end) return [];

  const dates: string[] = [];
  // A hard ceiling so a mistyped range cannot build a million-column sheet.
  for (let day = start; day <= end && dates.length < 400; day.setUTCDate(day.getUTCDate() + 1)) {
    dates.push(isoOf(day));
  }
  return dates;
};

/**
 * `H:MM:SS` with the hour left unpadded — the clock format the timesheet uses,
 * which is deliberately not `formatHMS`'s zero-padded `HH:MM:SS`.
 */
export const formatClock = (totalSeconds: number): string => {
  const seconds = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
};

export interface TimesheetRow {
  memberName: string;
  projectName: string;
  taskName: string;
  /** Date (`YYYY-MM-DD`) -> seconds tracked by this member on this to-do. */
  secondsByDate: Map<string, number>;
  totalSeconds: number;
}

/**
 * Collapses the log rows into one entry per member x project x to-do.
 *
 * Rows are keyed on ids where the API supplies them and on the displayed name
 * otherwise, so two genuinely different projects that happen to share a name
 * stay apart, while entries with no project or task still get one honest
 * bucket instead of being dropped.
 */
export const buildTimesheetRows = (logs: DetailedLogItem[]): TimesheetRow[] => {
  const rows = new Map<string, TimesheetRow>();

  logs.forEach((log) => {
    const projectName = log.project_name || "No project";
    const taskName = log.task_name || "No to-do";
    const key = [
      log.member_id,
      log.project_id ?? `name:${projectName}`,
      log.task_id ?? `name:${taskName}`,
    ].join("|");

    const row =
      rows.get(key) ??
      {
        memberName: log.member_name || "Unknown",
        projectName,
        taskName,
        secondsByDate: new Map<string, number>(),
        totalSeconds: 0,
      };

    const seconds = log.tracked_seconds || 0;
    // `date` is the IST calendar date the backend already bucketed the entry
    // into; re-deriving it here from a timestamp would risk a second answer.
    const date = String(log.date).slice(0, 10);
    row.secondsByDate.set(date, (row.secondsByDate.get(date) || 0) + seconds);
    row.totalSeconds += seconds;
    rows.set(key, row);
  });

  return [...rows.values()].sort(
    (a, b) =>
      a.memberName.localeCompare(b.memberName) ||
      a.projectName.localeCompare(b.projectName) ||
      a.taskName.localeCompare(b.taskName)
  );
};

/** Header row for the timesheet sheet, in file order. */
export const timesheetHeaders = (dates: string[]): string[] => [
  "Member",
  "Organization",
  "Time Zone",
  "Projects",
  "Task Summary",
  ...dates,
  "Total worked",
];

/** One CSV line per timesheet row, aligned to `dates`. */
export const timesheetBody = (
  rows: TimesheetRow[],
  dates: string[],
  organization: string,
  timeZone: string
): string[][] =>
  rows.map((row) => [
    row.memberName,
    organization,
    timeZone,
    row.projectName,
    row.taskName,
    ...dates.map((date) => formatClock(row.secondsByDate.get(date) || 0)),
    formatClock(row.totalSeconds),
  ]);

/* ------------------------------------------------------------------ */
/* The administrator's layout: one row per day                         */
/* ------------------------------------------------------------------ */

/**
 * The administrator's file puts the **date in a column** and writes one row per
 * day for each member x project x to-do, instead of giving every day its own
 * column as the layout above does.
 *
 * With the date as a column the sheet can be filtered by date, summed by member
 * or project, sorted and charted as it is, with nothing to reshape first; and a
 * day on which nobody worked simply has no row, rather than a run of `0:00:00`
 * cells. The client portal keeps the day-per-column layout above.
 *
 * Each row also carries that day's average activity.
 */

/** A running total used to average activity over time. */
export interface ActivityTally {
  /** Sum of (activity % x seconds) over the entries that carry an activity figure. */
  weighted: number;
  /** Seconds that carry an activity figure: the weight the average is taken over. */
  seconds: number;
}

/**
 * Adds one entry to a tally.
 *
 * Only an entry that actually has an activity figure counts. A manual entry, or
 * one with no recorded activity windows, has `null`, and treating that as 0%
 * would drag a real average down for time nobody measured -- its seconds still
 * count toward the row's total, just not toward the average. A duration that is
 * zero or negative (idle time discarded from a session can net it below zero)
 * has no weight, so it cannot pull the average either way.
 */
export const tallyActivity = (
  tally: ActivityTally,
  activity: number | null | undefined,
  seconds: number
): void => {
  if (typeof activity === "number" && Number.isFinite(activity) && seconds > 0) {
    tally.weighted += activity * seconds;
    tally.seconds += seconds;
  }
};

/**
 * The average activity (0-100), weighted by tracked time, or `null` when none of
 * the time has an activity figure. Weighting by seconds means a long session
 * counts for more than a short one, which is what "how active was this person on
 * this to-do" means.
 */
export const averageActivity = (tally: ActivityTally): number | null =>
  tally.seconds > 0 ? tally.weighted / tally.seconds : null;

/** `63%`, or an honest blank when there is no figure. 0% is a real answer and prints as `0%`. */
export const formatActivity = (percent: number | null): string => {
  if (percent === null || !Number.isFinite(percent)) return "";
  return `${Math.min(100, Math.max(0, Math.round(percent)))}%`;
};

export interface DailyRow {
  /** The IST calendar date, `YYYY-MM-DD`. */
  date: string;
  memberName: string;
  projectName: string;
  taskName: string;
  totalSeconds: number;
  activity: ActivityTally;
}

/**
 * Collapses the log rows into one entry per date x member x project x to-do,
 * oldest day first, then by member, project and to-do.
 *
 * Keyed like `buildTimesheetRows`: on ids where the API supplies them and on the
 * displayed name otherwise, so two projects that share a name stay apart and an
 * entry with no project or to-do still gets one honest row instead of being
 * dropped. Two sessions on the same day for the same to-do are one row, their
 * time added and their activity averaged by time.
 */
export const buildDailyRows = (logs: DetailedLogItem[]): DailyRow[] => {
  const rows = new Map<string, DailyRow>();

  logs.forEach((log) => {
    const projectName = log.project_name || "No project";
    const taskName = log.task_name || "No to-do";
    // The IST date the backend already bucketed the entry into; see above.
    const date = String(log.date).slice(0, 10);
    const key = [
      date,
      log.member_id,
      log.project_id ?? `name:${projectName}`,
      log.task_id ?? `name:${taskName}`,
    ].join("|");

    const row =
      rows.get(key) ??
      {
        date,
        memberName: log.member_name || "Unknown",
        projectName,
        taskName,
        totalSeconds: 0,
        activity: { weighted: 0, seconds: 0 },
      };

    const seconds = log.tracked_seconds || 0;
    row.totalSeconds += seconds;
    tallyActivity(row.activity, log.activity_percentage, seconds);
    rows.set(key, row);
  });

  return [...rows.values()].sort(
    (a, b) =>
      // `YYYY-MM-DD` sorts as text in calendar order.
      a.date.localeCompare(b.date) ||
      a.memberName.localeCompare(b.memberName) ||
      a.projectName.localeCompare(b.projectName) ||
      a.taskName.localeCompare(b.taskName)
  );
};

/**
 * `2026-09-28` -> `28-09-2026`, the way the Date column reads in the sheet.
 * Text in, text out: no `Date` object is involved, so no time zone can shift the
 * day. Anything that is not a `YYYY-MM-DD` date is passed through untouched
 * rather than guessed at.
 */
export const formatSheetDate = (iso: string): string => {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  return match ? `${match[3]}-${match[2]}-${match[1]}` : iso;
};

/** Header row of the administrator's file, in file order. */
export const dailyHeaders = (): string[] => [
  "Date",
  "Member",
  "Organization",
  "Time Zone",
  "Projects",
  "Task Summary",
  "Total worked",
  "Activity %",
];

/** One CSV line per day row, aligned to `dailyHeaders`. */
export const dailyBody = (rows: DailyRow[], organization: string, timeZone: string): string[][] =>
  rows.map((row) => [
    formatSheetDate(row.date),
    row.memberName,
    organization,
    timeZone,
    row.projectName,
    row.taskName,
    formatClock(row.totalSeconds),
    formatActivity(averageActivity(row.activity)),
  ]);
