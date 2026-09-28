import React, { useState } from "react";
import type { DetailedLogItem } from "../../../store/api/reportsApi";
import { formatHMS } from "../../../utils/duration";

export interface BreakdownItem {
  name: string;
  seconds: number;
}

export interface DateBreakdown {
  /** IST calendar date, `YYYY-MM-DD`. */
  date: string;
  seconds: number;
  items: BreakdownItem[];
}

export interface MemberItemBreakdown {
  member_id: number;
  member_name: string;
  seconds: number;
  dates: DateBreakdown[];
}

/**
 * Groups flat detailed-log rows into Member -> Date -> item, summing
 * `tracked_seconds` at every level -- one item type per tab: a project's
 * total that day on Projects, a task's on Tasks, an app's or site's on
 * Apps/URLs. `pick` reads whichever field names that item for the row
 * (`project_name`, `task_name`, `app` or `url`); rows where it comes back
 * null fall into one named bucket rather than being dropped.
 *
 * Members are ranked by total tracked seconds descending; within a member,
 * dates are most-recent-first; within a date, items are ranked by seconds
 * descending -- the same "biggest first" convention every ranked list on
 * this dashboard already uses.
 */
export function buildMemberItemBreakdown(
  rows: DetailedLogItem[],
  pick: (row: DetailedLogItem) => string | null,
  fallbackName: string
): MemberItemBreakdown[] {
  type MutableDate = { seconds: number; items: Map<string, BreakdownItem> };
  type MutableMember = { member_name: string; seconds: number; dates: Map<string, MutableDate> };
  const members = new Map<number, MutableMember>();

  for (const row of rows) {
    let member = members.get(row.member_id);
    if (!member) {
      member = { member_name: row.member_name, seconds: 0, dates: new Map() };
      members.set(row.member_id, member);
    }
    member.seconds += row.tracked_seconds;

    let date = member.dates.get(row.date);
    if (!date) {
      date = { seconds: 0, items: new Map() };
      member.dates.set(row.date, date);
    }
    date.seconds += row.tracked_seconds;

    const name = pick(row) ?? fallbackName;
    let item = date.items.get(name);
    if (!item) {
      item = { name, seconds: 0 };
      date.items.set(name, item);
    }
    item.seconds += row.tracked_seconds;
  }

  return Array.from(members.entries())
    .map(([member_id, member]) => ({
      member_id,
      member_name: member.member_name,
      seconds: member.seconds,
      dates: Array.from(member.dates.entries())
        .map(([date, d]) => ({
          date,
          seconds: d.seconds,
          items: Array.from(d.items.values()).sort((a, b) => b.seconds - a.seconds),
        }))
        // Most recent date first.
        .sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0)),
    }))
    .sort((a, b) => b.seconds - a.seconds);
}

const initials = (name: string) =>
  name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();

const formatDateLabel = (iso: string) =>
  new Date(`${iso}T00:00:00`).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });

const MemberRow: React.FC<{
  member: MemberItemBreakdown;
  isOpen: boolean;
  onToggle: () => void;
  accentColor: string;
}> = ({ member, isOpen, onToggle, accentColor }) => {
  const panelId = `member-breakdown-${member.member_id}`;
  const dayCount = member.dates.length;

  return (
    <li className="border-b border-[#F1F5F9] last:border-b-0">
      <button
        type="button"
        aria-expanded={isOpen}
        aria-controls={panelId}
        onClick={onToggle}
        className="flex w-full items-center gap-3 px-5 py-3.5 text-left transition hover:bg-[#F8FAFC]"
      >
        <span
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-[11px] font-bold text-white shadow-sm"
          style={{ backgroundColor: accentColor }}
        >
          {initials(member.member_name)}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[14px] font-bold text-[#0F172A]">{member.member_name}</span>
          <span className="block text-[11px] font-semibold text-[#94A3B8]">
            {dayCount} day{dayCount === 1 ? "" : "s"} tracked
          </span>
        </span>
        <span className="shrink-0 text-[14px] font-extrabold text-[#0F172A]">{formatHMS(member.seconds)}</span>
        <svg
          className={`h-4 w-4 shrink-0 text-[#CBD5E1] transition-transform duration-200 ${isOpen ? "rotate-180" : ""}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      <div id={panelId} hidden={!isOpen} className="space-y-3 bg-[#F8FAFC] px-5 pb-4 pt-1">
        {member.dates.map((d) => (
          <div key={d.date} className="overflow-hidden rounded-xl border border-[#E2E8F0] bg-white">
            <div className="flex items-center justify-between bg-[#F8FAFC] px-4 py-2">
              <span className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">
                {formatDateLabel(d.date)}
              </span>
              <span className="text-[11px] font-bold text-[#64748B]">{formatHMS(d.seconds)}</span>
            </div>
            <ul>
              {d.items.map((item) => (
                <li
                  key={item.name}
                  className="flex items-center justify-between gap-3 border-t border-[#F1F5F9] px-4 py-2"
                >
                  <span className="truncate text-[13px] font-semibold text-[#334155]">{item.name}</span>
                  <span className="shrink-0 text-[13px] font-bold text-[#0F172A]">{formatHMS(item.seconds)}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </li>
  );
};

export const MemberBreakdownAccordion: React.FC<{
  members: MemberItemBreakdown[];
  isLoading: boolean;
  isTruncated: boolean;
  emptyLabel: string;
  /** Singular, capitalized: "Project", "Task", "App", "Site". */
  itemLabel: string;
  /** Ties the section's accent to the tab it belongs to (matches REPORTS[reportId].color). */
  accentColor: string;
}> = ({ members, isLoading, isTruncated, emptyLabel, itemLabel, accentColor }) => {
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const toggle = (id: number) => setExpanded((current) => ({ ...current, [id]: !current[id] }));

  return (
    <section className="overflow-hidden rounded-2xl border border-[#E2E8F0] bg-white shadow-sm">
      <header className="border-b border-[#F1F5F9] px-6 py-5">
        <h2 className="text-[16px] font-bold tracking-tight text-[#0F172A]">Member Breakdown</h2>
        <p className="mt-0.5 text-[12px] text-[#94A3B8]">
          Click a member to see it day-by-day, by {itemLabel.toLowerCase()}
        </p>
      </header>

      {members.length === 0 ? (
        <p className="px-6 py-10 text-center text-[13px] text-[#94A3B8]">{isLoading ? "Loading…" : emptyLabel}</p>
      ) : (
        <ul>
          {members.map((member) => (
            <MemberRow
              key={member.member_id}
              member={member}
              isOpen={!!expanded[member.member_id]}
              onToggle={() => toggle(member.member_id)}
              accentColor={accentColor}
            />
          ))}
        </ul>
      )}

      {isTruncated && (
        <p className="border-t border-[#E2E8F0] bg-[#F8FAFC] px-6 py-3 text-[11px] text-[#94A3B8]">
          Showing the most tracked sessions in this range — narrow the date range or filters to see everything.
        </p>
      )}
    </section>
  );
};
