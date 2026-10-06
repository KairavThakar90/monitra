/**
 * Dummy data for the V2 dashboard and its report pages.
 *
 * Everything is generated deterministically from a string hash, so the numbers
 * are stable across reloads while still varying by month / project / member.
 * Swap `getDashboardData` and `reportRows` for API calls once the reporting
 * endpoints land — the shapes here are what the components expect.
 */

import { IST_TIME_ZONE } from "../../../utils/duration";

/**
 * "Today" on the organisation's calendar.
 *
 * The reports backend interprets every date filter as an IST calendar date, so
 * a range built from the browser's local day would ask for the wrong day for
 * anyone outside IST. This resolves the current IST date through the real zone
 * and rebuilds it as a local-midnight `Date`, so every helper below can keep
 * using plain `getFullYear`/`getMonth`/`setDate` arithmetic and still be
 * talking about the same calendar the server is.
 */
export const istToday = () => {
  const [year, month, day] = new Intl.DateTimeFormat('en-CA', {
    timeZone: IST_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  })
    .format(new Date())
    .split('-')
    .map(Number);
  return new Date(year, month - 1, day);
};

export const TODAY = istToday();

/* ------------------------------------------------------------------ */
/* Deterministic pseudo-random helpers                                 */
/* ------------------------------------------------------------------ */

const hash = (input: string) => {
  let h = 2166136261;
  for (let i = 0; i < input.length; i++) {
    h = Math.imul(h ^ input.charCodeAt(i), 16777619);
  }
  return h >>> 0;
};

/** Stable 0..1 for a key. */
const rnd = (key: string) => (hash(key) % 100000) / 100000;

/** Stable value in [min, max] for a key. */
const between = (key: string, min: number, max: number) => min + rnd(key) * (max - min);

/* ------------------------------------------------------------------ */
/* Entities                                                            */
/* ------------------------------------------------------------------ */

export interface Member {
  id: string;
  name: string;
  email: string;
  role: string;
}

const MEMBER_SEED: [string, string][] = [
  ["Aarav Sharma", "Frontend Engineer"],
  ["Priya Nair", "Product Designer"],
  ["Rohan Verma", "Backend Engineer"],
  ["Sara Khan", "QA Analyst"],
  ["Vikram Patel", "DevOps Engineer"],
  ["Neha Gupta", "Project Manager"],
  ["Arjun Mehta", "Data Analyst"],
  ["Isha Reddy", "Frontend Engineer"],
  ["Karan Singh", "Support Lead"],
  ["Divya Menon", "Content Strategist"],
  ["Rahul Joshi", "Backend Engineer"],
  ["Ananya Iyer", "UX Researcher"],
  ["Manav Desai", "Solutions Architect"],
  ["Pooja Rane", "QA Engineer"],
  ["Siddharth Rao", "Mobile Engineer"],
  ["Meera Pillai", "Product Manager"],
  ["Nikhil Bose", "Data Engineer"],
  ["Tanvi Shah", "Frontend Engineer"],
  ["Aditya Kulkarni", "Security Engineer"],
  ["Riya Chatterjee", "Marketing Lead"],
  ["Harsh Malhotra", "Sales Engineer"],
  ["Sneha Kapoor", "HR Business Partner"],
  ["Yash Agarwal", "Backend Engineer"],
  ["Lakshmi Nandan", "Finance Analyst"],
];

export const members: Member[] = MEMBER_SEED.map(([name, role], i) => ({
  id: `m${i + 1}`,
  name,
  email: name.toLowerCase().split(" ")[0] + "@monitra.io",
  role,
}));

const PROJECT_NAMES = [
  "Monitra Web Platform",
  "Mobile Time Tracker",
  "Payroll Integration",
  "Client Portal Revamp",
  "Screenshot Pipeline",
  "Reporting & Analytics",
  "Onboarding Automation",
  "Billing Service",
  "Desktop Agent",
  "Marketing Website",
  "Attendance & Leave",
  "Notification Service",
  "Data Warehouse Sync",
  "Chrome Extension",
  "Customer Support Hub",
  "Invoice Automation",
  "Access Control Suite",
  "Performance Reviews",
];

export const projects = PROJECT_NAMES.map((name, i) => ({ id: `p${i + 1}`, name }));

const TASK_SEED: [string, number][] = [
  ["Build reporting dashboard", 0],
  ["Screenshot upload service", 4],
  ["Timesheet approval flow", 2],
  ["Offline sync for mobile", 1],
  ["Design system tokens", 3],
  ["Invoice PDF generation", 7],
  ["Idle-time detection", 8],
  ["Role based access control", 16],
  ["Automated welcome emails", 6],
  ["SEO landing pages", 9],
  ["Leave balance calculation", 10],
  ["Push notification retries", 11],
  ["Nightly warehouse ETL", 12],
  ["Tab activity capture", 13],
  ["Ticket routing rules", 14],
  ["Recurring invoice engine", 15],
  ["SSO with Azure AD", 16],
  ["Review cycle templates", 17],
  ["Activity heatmap widget", 5],
  ["Blurred screenshot privacy mode", 4],
  ["Payroll export to CSV", 2],
  ["Deep link handling", 1],
  ["Client billing summary", 3],
  ["Webhook delivery logs", 11],
  ["Query performance tuning", 12],
  ["Extension auto-update", 13],
  ["Canned reply library", 14],
  ["Tax rule configuration", 15],
  ["Session timeout policy", 16],
  ["360 feedback collection", 17],
];

export const tasks = TASK_SEED.map(([name, projectIndex], i) => ({
  id: `t${i + 1}`,
  name,
  project: PROJECT_NAMES[projectIndex],
}));

export type UsageCategory = "Productive" | "Neutral" | "Unproductive";

const APP_SEED: [string, UsageCategory][] = [
  ["Visual Studio Code", "Productive"],
  ["Google Chrome", "Neutral"],
  ["Figma", "Productive"],
  ["Slack", "Neutral"],
  ["Postman", "Productive"],
  ["Microsoft Excel", "Productive"],
  ["Zoom", "Neutral"],
  ["Notion", "Productive"],
  ["Spotify", "Unproductive"],
  ["YouTube", "Unproductive"],
  ["IntelliJ IDEA", "Productive"],
  ["Terminal", "Productive"],
  ["Microsoft Teams", "Neutral"],
  ["Jira", "Productive"],
  ["Outlook", "Neutral"],
  ["Docker Desktop", "Productive"],
  ["Photoshop", "Productive"],
  ["WhatsApp Desktop", "Unproductive"],
  ["Steam", "Unproductive"],
  ["DBeaver", "Productive"],
  ["Sublime Text", "Productive"],
  ["Discord", "Unproductive"],
];

const URL_SEED: [string, UsageCategory][] = [
  ["github.com", "Productive"],
  ["app.monitra.io", "Productive"],
  ["stackoverflow.com", "Productive"],
  ["figma.com", "Productive"],
  ["mail.google.com", "Neutral"],
  ["docs.google.com", "Productive"],
  ["linkedin.com", "Neutral"],
  ["chatgpt.com", "Productive"],
  ["youtube.com", "Unproductive"],
  ["instagram.com", "Unproductive"],
  ["atlassian.net", "Productive"],
  ["npmjs.com", "Productive"],
  ["developer.mozilla.org", "Productive"],
  ["aws.amazon.com", "Productive"],
  ["calendar.google.com", "Neutral"],
  ["notion.so", "Productive"],
  ["reddit.com", "Unproductive"],
  ["x.com", "Unproductive"],
  ["medium.com", "Neutral"],
  ["figjam.com", "Productive"],
  ["vercel.com", "Productive"],
  ["netflix.com", "Unproductive"],
];

/* ------------------------------------------------------------------ */
/* Months                                                              */
/* ------------------------------------------------------------------ */

export interface MonthOption {
  key: string; // "2026-08"
  label: string; // "August 2026"
  short: string; // "Aug 2026"
  year: number;
  monthIndex: number; // 0-11
  from: string; // first day, ISO
  to: string; // last day (or today for the current month), ISO
  days: number; // days with data
}

const iso = (d: Date) => {
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
};

const MONTH_LABELS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const buildMonth = (year: number, monthIndex: number): MonthOption => {
  const first = new Date(year, monthIndex, 1);
  const lastOfMonth = new Date(year, monthIndex + 1, 0);
  const isCurrent = year === TODAY.getFullYear() && monthIndex === TODAY.getMonth();
  const last = isCurrent ? TODAY : lastOfMonth;
  return {
    key: `${year}-${String(monthIndex + 1).padStart(2, "0")}`,
    label: `${MONTH_LABELS[monthIndex]} ${year}`,
    short: `${MONTH_LABELS[monthIndex].slice(0, 3)} ${year}`,
    year,
    monthIndex,
    from: iso(first),
    to: iso(last),
    days: last.getDate(),
  };
};

/** Newest first — index 0 is the current month. */
export const MONTHS: MonthOption[] = Array.from({ length: 12 }, (_, i) => {
  const d = new Date(TODAY.getFullYear(), TODAY.getMonth() - i, 1);
  return buildMonth(d.getFullYear(), d.getMonth());
});

export const CURRENT_MONTH = MONTHS[0].key;

export const monthByKey = (key: string) => MONTHS.find((m) => m.key === key) ?? MONTHS[0];

/* ------------------------------------------------------------------ */
/* Derived dashboard data                                              */
/* ------------------------------------------------------------------ */

export interface ActivityRow {
  id: string;
  name: string;
  hours: number;
  activity: number; // percent 0-100
  meta: string;
}

export interface UsageRow {
  id: string;
  name: string;
  minutes: number;
  category: UsageCategory;
  users: number;
}

export interface DashboardData {
  month: MonthOption;
  /** "Today" for the current month, otherwise the month label. */
  usageScope: string;
  kpis: {
    monthlyActivity: { value: number; deltaPct: number; trend: number[] };
    totalProductivity: { value: number; deltaPct: number; trend: number[] };
    totalProjects: { value: number; deltaPct: number; trend: number[] };
    totalEmployees: { value: number; deltaPct: number; trend: number[] };
  };
  trend: { labels: string[]; tracked: number[]; manual: number[] };
  projects: ActivityRow[];
  members: ActivityRow[];
  tasks: ActivityRow[];
  apps: UsageRow[];
  urls: UsageRow[];
}

const round2 = (n: number) => Number(n.toFixed(2));

const projectRows = (monthKey: string): ActivityRow[] =>
  projects
    .map((p, i) => ({
      id: p.id,
      name: p.name,
      hours: round2(between(`proj|${p.id}|${monthKey}`, 40, 520) * (1 - i * 0.012)),
      activity: Math.round(between(`proja|${p.id}|${monthKey}`, 58, 95)),
      meta: `${Math.round(between(`projm|${p.id}|${monthKey}`, 2, 14))} members`,
    }))
    .sort((a, b) => b.hours - a.hours);

const memberRows = (monthKey: string): ActivityRow[] =>
  members
    .map((m) => ({
      id: m.id,
      name: m.name,
      hours: round2(between(`mem|${m.id}|${monthKey}`, 28, 178)),
      activity: Math.round(between(`mema|${m.id}|${monthKey}`, 55, 96)),
      meta: m.role,
    }))
    .sort((a, b) => b.hours - a.hours);

const taskRows = (monthKey: string): ActivityRow[] =>
  tasks
    .map((t) => ({
      id: t.id,
      name: t.name,
      hours: round2(between(`task|${t.id}|${monthKey}`, 6, 104)),
      activity: Math.round(between(`taska|${t.id}|${monthKey}`, 60, 94)),
      meta: t.project,
    }))
    .sort((a, b) => b.hours - a.hours);

const usageRows = (seed: [string, UsageCategory][], prefix: string, scopeKey: string, scale: number): UsageRow[] =>
  seed
    .map(([name, category], i) => ({
      id: `${prefix}${i + 1}`,
      name,
      minutes: Math.round(between(`${prefix}|${name}|${scopeKey}`, 25, 780) * scale),
      category,
      users: Math.round(between(`${prefix}u|${name}|${scopeKey}`, 6, 124)),
    }))
    .sort((a, b) => b.minutes - a.minutes);

const dailyTrend = (month: MonthOption) => {
  const labels: string[] = [];
  const tracked: number[] = [];
  const manual: number[] = [];

  for (let day = 1; day <= month.days; day++) {
    const date = new Date(month.year, month.monthIndex, day);
    const weekend = date.getDay() === 0 || date.getDay() === 6;
    const key = `${month.key}-${day}`;
    labels.push(`${MONTH_LABELS[month.monthIndex].slice(0, 3)} ${String(day).padStart(2, "0")}`);
    tracked.push(Math.round(between(`tr|${key}`, weekend ? 70 : 290, weekend ? 160 : 420)));
    manual.push(Math.round(between(`mn|${key}`, weekend ? 8 : 30, weekend ? 26 : 68)));
  }
  return { labels, tracked, manual };
};

/** Twelve trailing points for a KPI sparkline, ending on the selected month. */
const kpiTrend = (metric: string, monthKey: string, min: number, max: number) => {
  const index = MONTHS.findIndex((m) => m.key === monthKey);
  return Array.from({ length: 12 }, (_, i) => {
    const m = MONTHS[Math.min(MONTHS.length - 1, index + 11 - i)];
    return Math.round(between(`${metric}|${m.key}`, min, max));
  });
};

const cache = new Map<string, DashboardData>();

export const getDashboardData = (monthKey: string): DashboardData => {
  const cached = cache.get(monthKey);
  if (cached) return cached;

  const month = monthByKey(monthKey);
  const index = MONTHS.findIndex((m) => m.key === month.key);
  const prev = MONTHS[Math.min(MONTHS.length - 1, index + 1)];
  const isCurrent = month.key === CURRENT_MONTH;

  const projectList = projectRows(month.key);
  const memberList = memberRows(month.key);
  const taskList = taskRows(month.key);

  const avg = (rows: ActivityRow[]) => Math.round(rows.reduce((s, r) => s + r.activity, 0) / rows.length);

  const activityNow = avg(memberList);
  const activityPrev = avg(memberRows(prev.key));

  const productivityNow = Math.round(between(`prod|${month.key}`, 72, 91));
  const productivityPrev = Math.round(between(`prod|${prev.key}`, 72, 91));

  const activeProjects = projectList.filter((p) => p.hours > 60).length;
  const prevActiveProjects = projectRows(prev.key).filter((p) => p.hours > 60).length;

  const headcount = members.length;
  const prevHeadcount = headcount - Math.round(between(`hc|${month.key}`, 0, 3));

  const delta = (now: number, before: number) =>
    Number(before === 0 ? 0 : (((now - before) / before) * 100).toFixed(1));

  // App / URL usage is a "today" figure on the current month and a full-month
  // total on any past month, so the card never claims today's data for August.
  const scale = isCurrent ? 1 : month.days * 0.8;

  const data: DashboardData = {
    month,
    usageScope: isCurrent ? "Today" : month.short,
    kpis: {
      monthlyActivity: {
        value: activityNow,
        deltaPct: delta(activityNow, activityPrev),
        trend: kpiTrend("act", month.key, 62, 84),
      },
      totalProductivity: {
        value: productivityNow,
        deltaPct: delta(productivityNow, productivityPrev),
        trend: kpiTrend("prod", month.key, 72, 91),
      },
      totalProjects: {
        value: activeProjects,
        deltaPct: delta(activeProjects, prevActiveProjects),
        trend: kpiTrend("proj", month.key, 9, 18),
      },
      totalEmployees: {
        value: headcount,
        deltaPct: delta(headcount, prevHeadcount),
        trend: kpiTrend("hc", month.key, 18, 24),
      },
    },
    trend: dailyTrend(month),
    projects: projectList,
    members: memberList,
    tasks: taskList,
    apps: usageRows(APP_SEED, "a", isCurrent ? "today" : month.key, scale),
    urls: usageRows(URL_SEED, "u", isCurrent ? "today" : month.key, scale),
  };

  cache.set(monthKey, data);
  return data;
};

/* ------------------------------------------------------------------ */
/* Detailed report rows                                                */
/* ------------------------------------------------------------------ */

export interface ReportRow {
  id: string;
  date: string;
  memberId: string;
  member: string;
  role: string;
  project: string;
  task: string;
  hours: number;
  activity: number;
  app: string;
  url: string;
  category: UsageCategory;
}

/** One row per member per working day, for the last six months. */
const buildReportRows = (): ReportRow[] => {
  const rows: ReportRow[] = [];
  const start = new Date(TODAY.getFullYear(), TODAY.getMonth() - 5, 1);

  for (let d = new Date(start); d <= TODAY; d.setDate(d.getDate() + 1)) {
    const weekend = d.getDay() === 0 || d.getDay() === 6;
    if (weekend) continue;
    const date = iso(d);

    members.forEach((m) => {
      const key = `${date}|${m.id}`;
      // Not everyone logs time every day.
      if (rnd(`present|${key}`) < 0.12) return;

      const task = tasks[Math.floor(rnd(`task|${key}`) * tasks.length)];
      const app = APP_SEED[Math.floor(rnd(`app|${key}`) * APP_SEED.length)];
      const url = URL_SEED[Math.floor(rnd(`url|${key}`) * URL_SEED.length)];

      rows.push({
        id: key,
        date,
        memberId: m.id,
        member: m.name,
        role: m.role,
        project: task.project,
        task: task.name,
        hours: round2(between(`hrs|${key}`, 2.5, 9.2)),
        activity: Math.round(between(`act|${key}`, 52, 97)),
        app: app[0],
        url: url[0],
        category: app[1],
      });
    });
  }

  return rows;
};

export const reportRows: ReportRow[] = buildReportRows();

export const projectNames = PROJECT_NAMES;
