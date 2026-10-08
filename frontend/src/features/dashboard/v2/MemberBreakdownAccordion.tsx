import React, { useRef, useState } from "react";
import type { DetailedLogItem } from "../../../store/api/reportsApi";
import { formatHMS } from "../../../utils/duration";
import { FloatingCard, useHoverAnchor } from "./charts";
import { describeActivity, type MemberActivity } from "./memberActivity";

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
  activity?: MemberActivity;
}> = ({ member, isOpen, onToggle, accentColor, activity }) => {
  // Drawn `fixed`, so the section's rounded, clipping edge cannot cut it off.
  const { rect, bind } = useHoverAnchor();
  const activityText = activity ? describeActivity(activity, member.member_id) : null;
  // Hovering anywhere on the member's row -- avatar, name or hours -- shows the card, but the card
  // sits on the *name*, where the eye already is, not centred across the whole row.
  const nameRef = useRef<HTMLSpanElement>(null);
  const rowHover = activityText
    ? {
        onMouseEnter: () => {
          if (nameRef.current) bind.onMouseEnter({ currentTarget: nameRef.current } as React.MouseEvent<HTMLElement>);
        },
        onMouseLeave: bind.onMouseLeave,
      }
    : {};
  const panelId = `member-breakdown-${member.member_id}`;
  const dayCount = member.dates.length;

  // Each day is an accordion of its own. The most recent day opens with the
  // member, so what someone most often wants is already on screen; an explicit
  // click (or Expand / Collapse all) overrides that for any day.
  const [dayOverrides, setDayOverrides] = useState<Record<string, boolean>>({});
  const isDayOpen = (date: string, index: number) => dayOverrides[date] ?? index === 0;
  const toggleDay = (date: string, index: number) =>
    setDayOverrides((current) => ({ ...current, [date]: !isDayOpen(date, index) }));
  const allDaysOpen = member.dates.every((d, index) => isDayOpen(d.date, index));
  const setAllDays = (open: boolean) =>
    setDayOverrides(Object.fromEntries(member.dates.map((d) => [d.date, open])));

  return (
    <li className="border-b border-[#F1F5F9] last:border-b-0">
      <button
        type="button"
        aria-expanded={isOpen}
        aria-controls={panelId}
        onClick={onToggle}
        className="flex w-full items-center gap-3 px-5 py-3.5 text-left transition hover:bg-[#F8FAFC]"
        {...rowHover}
      >
        <span
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-[11px] font-bold text-white shadow-sm"
          style={{ backgroundColor: accentColor }}
        >
          {initials(member.member_name)}
        </span>
        <span className="min-w-0 flex-1">
          <span
            ref={nameRef}
            data-testid="member-name"
            // As wide as the name itself, not the column: the card is centred on this box.
            className="inline-block max-w-full truncate align-top text-[14px] font-bold text-[#0F172A]"
          >
            {member.member_name}
            {activityText && <span className="sr-only">, {activityText.toLowerCase()}</span>}
            {activityText && (
              <FloatingCard rect={rect}>
                <div role="tooltip" className="whitespace-nowrap rounded-md border border-slate-200 bg-white px-2 py-1 text-xs shadow-sm">
                  <span className="font-medium text-slate-800">{member.member_name}</span>
                  <span className="ml-2 text-slate-500">{activityText}</span>
                </div>
              </FloatingCard>
            )}
          </span>
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
        {dayCount > 1 && (
          <div className="flex justify-end">
            <button
              type="button"
              onClick={() => setAllDays(!allDaysOpen)}
              className="text-[11px] font-bold uppercase tracking-wider text-[#2563EB] hover:underline"
            >
              {allDaysOpen ? "Collapse all days" : "Expand all days"}
            </button>
          </div>
        )}
        {member.dates.map((d, index) => {
          const open = isDayOpen(d.date, index);
          const dayPanelId = `${panelId}-${d.date}`;
          return (
            <div key={d.date} className="overflow-hidden rounded-xl border border-[#E2E8F0] bg-white">
              <button
                type="button"
                aria-expanded={open}
                aria-controls={dayPanelId}
                onClick={() => toggleDay(d.date, index)}
                className="flex w-full items-center justify-between gap-3 bg-[#F8FAFC] px-4 py-2 text-left transition hover:bg-[#F1F5F9]"
              >
                <span className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">
                  {formatDateLabel(d.date)}
                </span>
                <span className="flex items-center gap-3">
                  <span className="text-[11px] font-bold text-[#64748B]">{formatHMS(d.seconds)}</span>
                  <svg
                    className={`h-3.5 w-3.5 shrink-0 text-[#94A3B8] transition-transform duration-200 ${open ? "rotate-180" : ""}`}
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M19 9l-7 7-7-7" />
                  </svg>
                </span>
              </button>
              <ul id={dayPanelId} hidden={!open}>
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
          );
        })}
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
  /** When given, hovering a member's name shows their average activity. */
  activity?: MemberActivity;
}> = ({ members, isLoading, isTruncated, emptyLabel, itemLabel, accentColor, activity }) => {
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const toggle = (id: number) => setExpanded((current) => ({ ...current, [id]: !current[id] }));

  return (
    <section className="overflow-hidden rounded-2xl border border-[#E2E8F0] bg-white shadow-sm">
      <header className="border-b border-[#F1F5F9] px-6 py-5">
        <h2 className="text-[16px] font-bold tracking-tight text-[#0F172A]">Member Breakdown</h2>
        <p className="mt-0.5 text-[12px] text-[#94A3B8]">
          Click a member, then a day, to see it by {itemLabel.toLowerCase()}
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
              activity={activity}
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
