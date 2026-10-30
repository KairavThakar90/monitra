import React, { useMemo, useState } from "react";
import { MemberShell } from "./MemberShell";
import { TransferEntryModal } from "./TransferEntryModal";
import { Card, EmptyState, ErrorNote, Spinner } from "./MemberUi";
import { useGetAllProjectsQuery } from "../../store/api/projectsApi";
import { useGetTimeEntriesQuery } from "../../store/api/timeEntryApi";
import type { TimeEntry } from "../../store/api/timeEntryApi";
import { useAuth } from "../auth/authContext";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { formatHMS, formatISTTime, istTodayISO } from "../../utils/duration";

/**
 * Time Entries: one day's individually recorded sessions, each movable to a
 * different project/task.
 *
 * Deliberately separate from `MemberTimeTracking` ("My Time Tracking"),
 * which only ever shows pre-aggregated project/task totals -- several
 * sessions in the same project fold into one line there, so there is no way
 * to pick "just the 4-5pm one" for a transfer from that page. This page
 * reads the raw `time_entries` rows instead, one row per real tracked
 * session, which is what a transfer actually needs to target.
 *
 * Defaults to today, as asked, with simple day-at-a-time navigation --
 * a transfer is almost always "I just noticed this looks wrong", not a
 * historical audit, so the common case needs no picking at all.
 */

const todayIso = () => istTodayISO();

/** The calendar day after `iso` (`YYYY-MM-DD`) -- `getTimeEntries`'s
 * `end_date` is exclusive, so one day's entries need the next day as the
 * upper bound, not the day itself. */
const nextDayIso = (iso: string) => {
  const [year, month, day] = iso.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + 1)).toISOString().slice(0, 10);
};

const addDaysIso = (iso: string, delta: number) => {
  const [year, month, day] = iso.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + delta)).toISOString().slice(0, 10);
};

/** "Mon, 31 Aug 2026" */
const longDayLabel = (iso: string) =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    weekday: "short",
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(new Date(`${iso}T00:00:00Z`));

type ProjectOption = { id: number; project_name: string; tasks: { id: number; name: string }[] | null };

const projectNameById = (projects: ProjectOption[], id: number) =>
  projects.find((project) => project.id === id)?.project_name ?? `Project #${id}`;

const taskNameById = (projects: ProjectOption[], projectId: number, taskId: number) =>
  projects.find((project) => project.id === projectId)?.tasks?.find((task) => task.id === taskId)?.name
  ?? `Task #${taskId}`;

const EntryRow: React.FC<{ entry: TimeEntry; projects: ProjectOption[]; onTransfer: () => void }> = ({
  entry,
  projects,
  onTransfer,
}) => (
  <tr className="border-b border-[#F1F5F9] text-[13px]">
    <td className="px-4 py-3">
      <div className="font-semibold text-[#0F172A]">{projectNameById(projects, entry.project_id)}</div>
      <div className="text-[12px] text-[#64748B]">{taskNameById(projects, entry.project_id, entry.task_id)}</div>
    </td>
    <td className="px-4 py-3 text-[#64748B]">{formatISTTime(entry.start_time)}</td>
    <td className="px-4 py-3 text-[#64748B]">
      {entry.is_running ? (
        entry.liveness === 'stale' ? (
          <span
            className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2.5 py-1 text-[11px] font-bold text-amber-700"
            title="Monitra has stopped reporting in for this timer, so the time shown stops at the last signal. It is ended automatically if that continues."
          >
            No signal
          </span>
        ) : (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-1 text-[11px] font-bold text-emerald-700">
            Tracking now
          </span>
        )
      ) : (
        formatISTTime(entry.end_time)
      )}
    </td>
    <td className="px-4 py-3 text-right font-mono font-bold text-[#0F172A]">
      {entry.elapsed_time || formatHMS(entry.total_seconds)}
    </td>
    <td className="px-4 py-3 text-right">
      {entry.is_running ? (
        <span className="text-[12px] text-[#94A3B8]" title="Stop the timer before transferring this session.">
          Still running
        </span>
      ) : (
        <button
          type="button"
          onClick={onTransfer}
          className="rounded-md border border-[#E2E8F0] px-3 py-1.5 text-[12px] font-bold text-[#2563EB] hover:bg-[#EFF6FF]"
        >
          Change Project
        </button>
      )}
    </td>
  </tr>
);

export const MemberTimeEntries: React.FC = () => {
  const { currentUser } = useAuth();
  const [date, setDate] = useState(todayIso());
  const [transferring, setTransferring] = useState<TimeEntry | null>(null);

  const { data: projects = [] } = useGetAllProjectsQuery({ includeTasks: true });
  const {
    data: entries = [],
    isLoading,
    isFetching,
    error,
  } = useGetTimeEntriesQuery(
    { start_date: date, end_date: nextDayIso(date), user_id: currentUser?.id },
    { skip: !currentUser }
  );

  const sorted = useMemo(
    () => [...entries].sort((a, b) => a.start_time.localeCompare(b.start_time)),
    [entries]
  );
  const totalSeconds = entries.reduce((sum, entry) => sum + entry.elapsed_seconds, 0);

  return (
    <MemberShell
      title="Time Entries"
      subtitle="Every session you tracked, one row each — move one to a different project if it landed on the wrong one."
      actions={<InlineRefreshIndicator active={isFetching && !isLoading} />}
    >
      <div className="w-full space-y-6 pb-20">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#E2E8F0] bg-white p-3 shadow-sm">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setDate((current) => addDaysIso(current, -1))}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-[#E2E8F0] text-[#475569] hover:bg-[#F8FAFC]"
              aria-label="Previous day"
            >
              ‹
            </button>
            <input
              type="date"
              value={date}
              max={todayIso()}
              onChange={(event) => event.target.value && setDate(event.target.value)}
              className="rounded-lg border border-[#E2E8F0] px-3 py-1.5 text-[13px] font-semibold text-[#0F172A] outline-none focus:border-[#2563EB]"
            />
            <button
              type="button"
              onClick={() => setDate((current) => addDaysIso(current, 1))}
              disabled={date >= todayIso()}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-[#E2E8F0] text-[#475569] hover:bg-[#F8FAFC] disabled:opacity-40"
              aria-label="Next day"
            >
              ›
            </button>
            {date !== todayIso() && (
              <button
                type="button"
                onClick={() => setDate(todayIso())}
                className="rounded-lg px-2.5 py-1.5 text-[12px] font-bold text-[#2563EB] hover:bg-[#EFF6FF]"
              >
                Today
              </button>
            )}
          </div>
          <div className="text-[13px] font-bold text-[#0F172A]">{longDayLabel(date)}</div>
        </div>

        <Card
          title="Recorded time entries"
          action={
            <span className="font-mono text-[13px] font-bold text-[#0F172A]">
              {formatHMS(totalSeconds)} total
            </span>
          }
        >
          {isLoading ? (
            <Spinner />
          ) : error ? (
            <ErrorNote message="Could not load this day's time entries." />
          ) : sorted.length === 0 ? (
            <EmptyState
              message={date === todayIso() ? "Nothing tracked yet today." : "Nothing was tracked on this day."}
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left">
                <thead>
                  <tr className="border-b border-[#E2E8F0] text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">
                    <th className="px-4 py-2">Project / Task</th>
                    <th className="px-4 py-2">Start</th>
                    <th className="px-4 py-2">Stop</th>
                    <th className="px-4 py-2 text-right">Duration</th>
                    <th className="px-4 py-2 text-right">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((entry) => (
                    <EntryRow
                      key={entry.id}
                      entry={entry}
                      projects={projects}
                      onTransfer={() => setTransferring(entry)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {transferring && (
        <TransferEntryModal
          entry={transferring}
          projects={projects}
          onClose={() => setTransferring(null)}
        />
      )}
    </MemberShell>
  );
};
