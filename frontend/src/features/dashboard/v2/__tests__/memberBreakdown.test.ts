import { describe, expect, it } from "vitest";

import { buildMemberItemBreakdown } from "../MemberBreakdownAccordion";
import type { DetailedLogItem } from "../../../../store/api/reportsApi";

const row = (overrides: Partial<DetailedLogItem>): DetailedLogItem => ({
  id: "te-1",
  date: "2026-09-22",
  member_id: 1,
  member_name: "Ada",
  role: "employee",
  project_id: 10,
  project_name: "Alpha",
  task_id: 100,
  task_name: "Design",
  app: null,
  url: null,
  tracked_seconds: 3600,
  tracked_hours: 1,
  tracked_time: "01:00:00",
  activity_percentage: 80,
  ...overrides,
});

const byProject = (r: DetailedLogItem) => r.project_name;
const byTask = (r: DetailedLogItem) => r.task_name;
const byApp = (r: DetailedLogItem) => r.app;
const byUrl = (r: DetailedLogItem) => r.url;

describe("buildMemberItemBreakdown", () => {
  it("sums seconds at every level: member, date and item", () => {
    const rows = [
      row({ id: "te-1", tracked_seconds: 3600 }),
      // A second session, same member/date/project -- must be summed, not
      // overwritten or kept as a separate leaf.
      row({ id: "te-2", tracked_seconds: 1800 }),
    ];
    const [member] = buildMemberItemBreakdown(rows, byProject, "Unassigned");
    expect(member.member_name).toBe("Ada");
    expect(member.seconds).toBe(5400);
    expect(member.dates).toHaveLength(1);
    expect(member.dates[0].seconds).toBe(5400);
    expect(member.dates[0].items).toEqual([{ name: "Alpha", seconds: 5400 }]);
  });

  it("keeps different dates and items as separate branches", () => {
    const rows = [
      row({ id: "te-1", date: "2026-09-22", project_name: "Alpha" }),
      row({ id: "te-2", date: "2026-09-23", project_name: "Alpha" }),
      row({ id: "te-3", date: "2026-09-22", project_name: "Beta", tracked_seconds: 7200 }),
    ];
    const [member] = buildMemberItemBreakdown(rows, byProject, "Unassigned");
    expect(member.dates).toHaveLength(2);
    const sep22 = member.dates.find((d) => d.date === "2026-09-22")!;
    // Ranked biggest-first within the date.
    expect(sep22.items).toEqual([
      { name: "Beta", seconds: 7200 },
      { name: "Alpha", seconds: 3600 },
    ]);
  });

  it("groups separate members independently, most-tracked member first", () => {
    const rows = [
      row({ id: "te-1", member_id: 1, member_name: "Ada", tracked_seconds: 3600 }),
      row({ id: "te-2", member_id: 2, member_name: "Bo", tracked_seconds: 7200 }),
    ];
    const members = buildMemberItemBreakdown(rows, byProject, "Unassigned");
    expect(members.map((m) => m.member_name)).toEqual(["Bo", "Ada"]);
  });

  it("orders each member's own dates most-recent-first", () => {
    const rows = [
      row({ id: "te-1", date: "2026-09-20" }),
      row({ id: "te-2", date: "2026-09-25" }),
      row({ id: "te-3", date: "2026-09-22" }),
    ];
    const [member] = buildMemberItemBreakdown(rows, byProject, "Unassigned");
    expect(member.dates.map((d) => d.date)).toEqual(["2026-09-25", "2026-09-22", "2026-09-20"]);
  });

  it("switches which field names the item via the pick function", () => {
    const rows = [row({ task_name: "Review", tracked_seconds: 900 })];
    const [member] = buildMemberItemBreakdown(rows, byTask, "No task");
    expect(member.dates[0].items).toEqual([{ name: "Review", seconds: 900 }]);
  });

  it("reads app/url fields the same way for the usage dimensions", () => {
    const appRows = [row({ app: "Slack", url: null, tracked_seconds: 300 })];
    expect(buildMemberItemBreakdown(appRows, byApp, "Unknown application")[0].dates[0].items).toEqual([
      { name: "Slack", seconds: 300 },
    ]);

    const urlRows = [row({ app: null, url: "github.com", tracked_seconds: 300 })];
    expect(buildMemberItemBreakdown(urlRows, byUrl, "Unknown site")[0].dates[0].items).toEqual([
      { name: "github.com", seconds: 300 },
    ]);
  });

  it("falls back to a named bucket for a null field rather than dropping the row", () => {
    const rows = [row({ project_name: null, tracked_seconds: 1200 })];
    const [member] = buildMemberItemBreakdown(rows, byProject, "Unassigned");
    expect(member.seconds).toBe(1200);
    expect(member.dates[0].items).toEqual([{ name: "Unassigned", seconds: 1200 }]);
  });

  it("returns an empty list for no rows, rather than an empty member", () => {
    expect(buildMemberItemBreakdown([], byProject, "Unassigned")).toEqual([]);
  });
});
