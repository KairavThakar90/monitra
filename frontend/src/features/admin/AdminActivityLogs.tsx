import React, { useMemo, useState } from "react";
import { V2Shell } from "../dashboard/v2/V2Shell";
import { Card, EmptyState, ErrorNote, Spinner, initialsOf } from "../member/MemberUi";
import { SearchInput } from "../feedback/feedbackFilters";
import {
  DateRangeFilter,
  MemberMultiSelect,
  exportToCsv,
  rangeFor,
  type DateRange,
} from "../dashboard/v2/filters";
import { useGetActivityLogsQuery } from "../../store/api/activityLogsApi";
import type { ActivityLogEntry, ActivityLogMemberGroup } from "../../store/api/activityLogsApi";
import { useGetAllMembersQuery } from "../../store/api/membersApi";
import { useAuth } from "../auth/authContext";
import { isTeamScoped } from "../../utils/roles";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { formatISTDate, formatISTTime12 } from "../../utils/duration";
import { validateSearchTerm } from "../../validation";
import {
  LOG_CSV_HEADERS,
  entryActionLabel,
  contextLabel,
  countEntries,
  filterMembers,
  groupEntriesByDay,
  logsToCsvRows,
  moduleStyle,
  sourceLabel,
} from "./activityLogFormat";

/**
 * The Logs page: everything people did, employee by employee.
 *
 * Each employee is one collapsed row -- name, how many actions, when they were
 * last active -- that opens onto their actions for the chosen dates, newest
 * first and split by day: signing in and out, opening and closing the desktop
 * application, starting and stopping the timer, manual time requests and the
 * decisions on them, project and task changes (a status moving from one value
 * to another, who was assigned), and the directory, client, screenshot and
 * feedback decisions an administrator makes.
 *
 * The page reads and never writes. `GET /activity-logs` decides who is on it:
 * the whole organization for Admin and HR; for a leader their own team, plus
 * whatever anyone changed on a project they lead. The date range and the
 * search are sent to the server, because the trail is too dense to load whole;
 * the employee picker narrows what came back. None of them can widen it.
 *
 * Times are IST, the calendar every other screen reports against. An empty
 * result is shown as empty -- nothing here is ever filled in.
 */

/** How many of one employee's rows are rendered before "Show more". */
const ROWS_PER_STEP = 100;

const AVATAR_COLORS = ["bg-blue-500", "bg-rose-500", "bg-emerald-500", "bg-amber-500", "bg-purple-500", "bg-cyan-500"];

/** One recorded action. */
const LogRow: React.FC<{ entry: ActivityLogEntry }> = ({ entry }) => {
  const style = moduleStyle(entry.module);
  const source = sourceLabel(entry);
  const context = contextLabel(entry);
  return (
    <li className="grid grid-cols-[84px_1fr] gap-x-4 gap-y-1 px-5 py-3 sm:grid-cols-[84px_190px_1fr_auto] sm:items-center">
      <span className="text-[12px] font-bold tabular-nums text-[#334155]">{formatISTTime12(entry.created_at)}</span>

      <span className="flex min-w-0 items-center gap-2">
        <span
          className="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide"
          style={{ color: style.text, background: style.bg }}
        >
          {style.label}
        </span>
        <span className="truncate text-[12px] font-bold text-[#0F172A]" title={entryActionLabel(entry)}>
          {entryActionLabel(entry)}
        </span>
      </span>

      <span className="col-span-2 min-w-0 sm:col-span-1">
        <span className="block text-[13px] text-[#334155]">{entry.description || "—"}</span>
        {context && (
          <span className="mt-0.5 block truncate text-[11px] font-semibold text-[#94A3B8]" title={context}>
            {context}
          </span>
        )}
      </span>

      <span className="col-span-2 flex items-center gap-2 sm:col-span-1 sm:justify-end">
        {source && (
          <span
            className="rounded-md border border-[#E2E8F0] bg-[#F8FAFC] px-2 py-0.5 text-[10px] font-bold text-[#475569]"
            title={entry.ip_address ? `From ${entry.ip_address}` : undefined}
          >
            {source}
          </span>
        )}
      </span>
    </li>
  );
};

/**
 * One employee, collapsed to a summary row until opened.
 *
 * A closed section renders none of its rows, so a page holding thousands of
 * actions draws only the roster until somebody asks for a person's trail --
 * and then only the first hundred, with the rest one click away.
 */
const MemberAccordion: React.FC<{
  member: ActivityLogMemberGroup;
  open: boolean;
  onToggle: () => void;
}> = ({ member, open, onToggle }) => {
  const [shown, setShown] = useState(ROWS_PER_STEP);
  const visible = useMemo(() => member.entries.slice(0, shown), [member.entries, shown]);
  const days = useMemo(() => groupEntriesByDay(visible), [visible]);
  const color = AVATAR_COLORS[member.user_id % AVATAR_COLORS.length];
  const remaining = member.entries.length - visible.length;
  const subtitle = [member.designation || member.role_name, member.email].filter(Boolean).join(" · ");

  return (
    <section className="overflow-hidden rounded-xl border border-[#E2E8F0] bg-white shadow-sm">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-5 py-4 text-left transition hover:bg-[#F8FAFC]"
      >
        <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-[12px] font-bold text-white ${color}`}>
          {initialsOf(member.name)}
        </span>

        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="truncate text-[14px] font-bold text-[#0F172A]">{member.name}</span>
            <span className="rounded-full bg-[#F1F5F9] px-2 py-0.5 text-[10px] font-bold text-[#334155]">
              {member.entries.length} action{member.entries.length === 1 ? "" : "s"}
            </span>
          </span>
          {subtitle && <span className="mt-0.5 block truncate text-[11px] font-medium text-[#94A3B8]">{subtitle}</span>}
        </span>

        <span className="hidden shrink-0 text-right sm:block">
          <span className="block text-[10px] font-bold uppercase tracking-wider text-[#94A3B8]">Last activity</span>
          <span className="block text-[12px] font-semibold text-[#334155]">
            {formatISTDate(member.last_activity_at)}, {formatISTTime12(member.last_activity_at)}
          </span>
        </span>

        <svg
          className={"h-4 w-4 shrink-0 text-[#94A3B8] transition-transform duration-200 " + (open ? "rotate-180" : "")}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="border-t border-[#F1F5F9]">
          {days.map((group) => (
            <div key={group.day}>
              <div className="flex items-center gap-3 border-b border-[#F1F5F9] bg-[#F8FAFC] px-5 py-2">
                <h4 className="text-[12px] font-bold text-[#0F172A]">{formatISTDate(`${group.day}T12:00:00Z`)}</h4>
                <span className="text-[11px] font-semibold text-[#94A3B8]">
                  {group.entries.length} action{group.entries.length === 1 ? "" : "s"}
                </span>
              </div>
              <ul className="divide-y divide-[#F1F5F9]">
                {group.entries.map((entry) => (
                  <LogRow key={entry.id} entry={entry} />
                ))}
              </ul>
            </div>
          ))}
          {remaining > 0 && (
            <div className="border-t border-[#F1F5F9] px-5 py-3 text-center">
              <button
                type="button"
                onClick={() => setShown((current) => current + ROWS_PER_STEP)}
                className="rounded-lg px-3 py-1.5 text-[12px] font-bold text-[#2563EB] transition hover:bg-[#EFF6FF]"
              >
                Show {Math.min(ROWS_PER_STEP, remaining)} more ({remaining} remaining)
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  );
};

/**
 * What the Logs page opens on, and what Reset returns to: today (IST).
 *
 * Not the shared `DEFAULT_RANGE` (the last seven days), which the dashboard and
 * reports still use -- the trail is read day by day, and a week of everyone's
 * activity is a long page to open on. Computed when asked rather than once at
 * load, so a tab left open past midnight resets to the new day, not the old one.
 */
const todayRange = (): DateRange => rangeFor("today", { preset: "today", from: "", to: "" });

export const AdminActivityLogs: React.FC = () => {
  const { currentUser } = useAuth();
  const teamScoped = isTeamScoped(currentUser);

  const [range, setRange] = useState<DateRange>(todayRange);
  const [search, setSearch] = useState("");
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);

  /**
   * Which sections are open. `baseOpen` is what Expand all / Collapse all set;
   * `toggled` holds the sections the reader flipped since. Until either button
   * is pressed the default applies: a single employee opens, a roster stays
   * closed.
   */
  const [baseOpen, setBaseOpen] = useState<boolean | null>(null);
  const [toggled, setToggled] = useState<Set<number>>(new Set());

  // Only a term the validation catalogue accepts is sent; an invalid one is
  // reported under the box by SearchInput and the last good answer stays up.
  const debouncedSearch = useDebouncedValue(search);
  const checkedSearch = validateSearchTerm(debouncedSearch, { fieldLabel: "Search" });
  const searchTerm = checkedSearch.ok ? checkedSearch.value : "";

  const { data, isLoading, isFetching, isError } = useGetActivityLogsQuery(
    { start: range.from, end: range.to, search: searchTerm },
    // A trail is only useful if it is current: cached rows paint at once and
    // are always re-read behind, rather than trusted for the default minute.
    { refetchOnMountOrArgChange: true },
  );

  /** Already narrowed to the caller's team server-side, for a leader. */
  const { data: members } = useGetAllMembersQuery();

  const groups = useMemo(() => data?.members ?? [], [data]);
  const visible = useMemo(() => filterMembers(groups, selectedMembers), [groups, selectedMembers]);
  const shownActions = useMemo(() => countEntries(visible), [visible]);

  const defaultOpen = baseOpen ?? visible.length === 1;
  const isOpen = (userId: number) => defaultOpen !== toggled.has(userId);
  const toggle = (userId: number) =>
    setToggled((current) => {
      const next = new Set(current);
      if (next.has(userId)) next.delete(userId);
      else next.add(userId);
      return next;
    });
  const setAll = (open: boolean) => {
    setBaseOpen(open);
    setToggled(new Set());
  };
  const allOpen = visible.length > 0 && visible.every((member) => isOpen(member.user_id));

  const isDirty =
    search !== "" || selectedMembers.length > 0 ||
    range.from !== todayRange().from || range.to !== todayRange().to;

  const resetFilters = () => {
    setSearch("");
    setSelectedMembers([]);
    setRange(todayRange());
  };

  const exportCsv = () =>
    exportToCsv(
      `activity-logs_${range.from}_to_${range.to}.csv`,
      LOG_CSV_HEADERS,
      logsToCsvRows(visible),
      [
        ["Activity logs"],
        ["Dates (IST)", `${range.from} to ${range.to}`],
        ...(searchTerm ? [["Search", searchTerm]] : []),
        [],
      ],
    );

  return (
    <V2Shell
      title="Logs"
      subtitle={
        teamScoped
          ? "Everything your team did in Monitra, and every change made to the projects you lead — sign-ins, the desktop app, the timer, requests, assignments and status changes — employee by employee."
          : "Everything people did in Monitra — sign-ins, the desktop app, the timer, requests, projects, assignments, clients and screenshots — employee by employee."
      }
      actions={
        <>
          <InlineRefreshIndicator active={isFetching && !isLoading} />
          <button
            type="button"
            onClick={exportCsv}
            disabled={shownActions === 0}
            className="flex items-center gap-1.5 rounded-lg bg-[#0F172A] px-4 py-2 text-xs font-bold text-white shadow-sm transition hover:bg-[#1E293B] disabled:cursor-not-allowed disabled:opacity-50"
          >
            <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" />
            </svg>
            Export CSV
          </button>
        </>
      }
    >
      <div className="w-full space-y-4 pb-20">
        <div className="rounded-xl border border-[#E2E8F0] bg-white p-4 shadow-sm">
          <div className="flex flex-wrap items-center gap-3">
            <SearchInput value={search} onChange={setSearch} subject="logs" />
            <DateRangeFilter value={range} onChange={setRange} />
            <MemberMultiSelect members={members ?? []} selected={selectedMembers} onChange={setSelectedMembers} />
            {isDirty && (
              <button
                type="button"
                onClick={resetFilters}
                className="rounded-lg px-3 py-2 text-[12px] font-bold text-[#64748B] transition hover:bg-[#F1F5F9] hover:text-[#0F172A]"
              >
                Reset
              </button>
            )}
            {/* The same button, in the same place, as on Assign Tasks. */}
            {visible.length > 0 && (
              <button
                type="button"
                onClick={() => setAll(!allOpen)}
                className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-bold text-slate-500 transition hover:bg-slate-50 hover:text-slate-700"
              >
                {allOpen ? "Collapse All" : "Expand All"}
              </button>
            )}
          </div>
        </div>

        {isError && <ErrorNote message="The logs could not be loaded. Please try again." />}

        {data?.truncated && (
          <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-[13px] font-semibold text-amber-800">
            These dates hold more than {data.total.toLocaleString()} actions, so only the most recent{" "}
            {data.total.toLocaleString()} are shown. Choose a shorter range, an employee or a search to see the rest.
          </div>
        )}

        {isLoading ? (
          <Spinner label="Loading logs…" />
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-3 px-1">
              <p className="text-[12px] font-semibold text-[#64748B]">
                {shownActions.toLocaleString()} action{shownActions === 1 ? "" : "s"} by {visible.length}{" "}
                {visible.length === 1 ? "employee" : "employees"}
              </p>
            </div>

            {visible.length === 0 ? (
              !isError && (
                <Card>
                  <EmptyState
                    message={isDirty ? "No logs match these filters." : "No activity recorded in the last 7 days."}
                    hint={
                      isDirty
                        ? "Try a wider date range, a different employee, or clear the search."
                        : "Actions appear here as people sign in, track time and make changes."
                    }
                  />
                </Card>
              )
            ) : (
              <div className={`space-y-3 transition-opacity ${isFetching ? "opacity-70" : ""}`}>
                {visible.map((member) => (
                  <MemberAccordion
                    key={member.user_id}
                    member={member}
                    open={isOpen(member.user_id)}
                    onToggle={() => toggle(member.user_id)}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </V2Shell>
  );
};
