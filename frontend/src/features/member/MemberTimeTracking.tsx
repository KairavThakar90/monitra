import React, { useMemo, useState } from "react";
import { MemberShell } from "./MemberShell";
import { Card, EmptyState, ErrorNote, Spinner, StatusPill } from "./MemberUi";
import {
  useGetTimeTrackingQuery,
  useGetTimeTrackingDetailsQuery,
} from "../../store/api/timeTrackingApi";
import type { TimeTrackingEntry } from "../../store/api/timeTrackingApi";
import { useGetAllProjectsQuery } from "../../store/api/projectsApi";
import {
  useCreateManualTimeEntryRequestMutation,
  useGetManualTimeEntryRequestsQuery,
  useDeleteManualTimeEntryRequestMutation,
} from "../../store/api/manualTimeEntryApi";
import { useAuth } from "../auth/authContext";
import { useFeedback } from "../../components/FeedbackProvider";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { DateRangeFilter, DEFAULT_RANGE } from "../dashboard/v2/filters";
import type { DateRange } from "../dashboard/v2/filters";
import { formatHMS, formatISTDate, formatISTTime, istWallClockToUtcISO } from "../../utils/duration";
import { series } from "../dashboard/v2/theme";

/**
 * The member's own time tracking.
 *
 * `/time-tracking` and `/manual-time-entry-requests` both pin a caller without
 * `time_entries:view_all` to their own rows, so nothing here filters by user
 * and nothing here can be widened from the address bar. The member id is still
 * sent to the *detail* endpoint because that route takes one in its path — the
 * service rejects any id but the caller's own.
 *
 * A member may raise and withdraw a manual entry request; approving one needs
 * `manual_time_entries:approve`, which they do not have, so no approve control
 * exists on this page.
 */

const todayIso = () => {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(
    now.getDate()
  ).padStart(2, "0")}`;
};

/** "Mon, 31 Aug 2026" — the weekday is what makes a day row scannable. */
const longDayLabel = (iso: string) =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    weekday: "short",
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(new Date(iso));

/** Two initials for a project chip, e.g. "Hubstaff to Monitra" -> "HM". */
const initialsOf = (name: string) =>
  name
    .split(/\s+/)
    .filter((word) => /[a-z0-9]/i.test(word))
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() ?? "")
    .join("") || "?";

/**
 * One day of the log, and — once opened — that day's project/to-do rows.
 *
 * The breakdown is fetched per day rather than for the whole range, because
 * `/time-tracking/{id}` totals whatever window it is given: asking it for the
 * range would return range totals that could not be attributed to any one day.
 * The query is skipped until the row is expanded, so a closed accordion costs
 * nothing and an opened one is cached by RTK Query for the rest of the visit.
 */
const DayRow: React.FC<{
  entry: TimeTrackingEntry;
  employeeId: number;
  open: boolean;
  onToggle: () => void;
}> = ({ entry, employeeId, open, onToggle }) => {
  const { data, isFetching } = useGetTimeTrackingDetailsQuery(
    { employeeId, start_date: entry.date, end_date: entry.date },
    { skip: !open }
  );

  const projects = data?.projects ?? [];
  const todoCount = projects.reduce((sum, project) => sum + (project.tasks?.length ?? 0), 0);

  return (
    <>
      <tr
        onClick={onToggle}
        className={
          "cursor-pointer border-b border-[#E2E8F0] transition " +
          (open ? "bg-[#F8FAFC]" : "bg-white hover:bg-[#FBFCFE]")
        }
      >
        <td className="px-3 py-3">
          <div className="flex items-center gap-2">
            <span
              aria-hidden="true"
              className={"text-[#94A3B8] transition-transform " + (open ? "rotate-0" : "-rotate-90")}
            >
              <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" d="M19 9l-7 7-7-7" />
              </svg>
            </span>
            <span className="font-bold text-[#0F172A]">{longDayLabel(entry.date)}</span>
          </div>
        </td>

        {/* Project chips and the to-do count are only known once the day has
            been fetched. An unopened day says nothing rather than guessing. */}
        <td className="px-3 py-3">
          {open && projects.length > 0 ? (
            <div className="flex items-center -space-x-1.5">
              {projects.slice(0, 4).map((project, index) => (
                <span
                  key={project.id}
                  title={project.name}
                  className="flex h-6 w-6 items-center justify-center rounded-full border-2 border-white text-[9px] font-black text-white"
                  style={{ backgroundColor: series[index % series.length] }}
                >
                  {initialsOf(project.name)}
                </span>
              ))}
              {projects.length > 4 && (
                <span className="pl-3 text-[11px] font-bold text-[#94A3B8]">+{projects.length - 4}</span>
              )}
            </div>
          ) : (
            <span className="text-[#CBD5E1]">-</span>
          )}
        </td>

        <td className="px-3 py-3">
          {open && todoCount > 0 ? (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-[#EFF6FF] px-2.5 py-1 text-[11px] font-bold text-[#2563EB]">
              <span className="rounded-full bg-[#2563EB] px-1.5 text-white">{todoCount}</span>
              To-do{todoCount === 1 ? "" : "s"}
            </span>
          ) : (
            <span className="text-[#CBD5E1]">-</span>
          )}
        </td>

        <td className="px-3 py-3 text-[#64748B]">{formatISTTime(entry.start_time)}</td>
        <td className="px-3 py-3 text-[#64748B]">{formatISTTime(entry.end_time)}</td>
        <td className="px-3 py-3 text-right font-mono font-bold text-[#0F172A]">
          {entry.total_time || formatHMS(entry.total_seconds)}
        </td>
      </tr>

      {open && isFetching && (
        <tr className="border-b border-[#F1F5F9]">
          <td colSpan={6} className="px-3 py-4 text-center text-[12px] text-[#94A3B8]">
            Loading this day's breakdown...
          </td>
        </tr>
      )}

      {open && !isFetching && projects.length === 0 && (
        <tr className="border-b border-[#F1F5F9]">
          <td colSpan={6} className="px-3 py-4 text-center text-[12px] text-[#94A3B8]">
            This day's time was not recorded against any project.
          </td>
        </tr>
      )}

      {open &&
        !isFetching &&
        projects.map((project, projectIndex) =>
          // A project with no task rows still gets one line: its time is real
          // even when nothing tells us which to-do it belonged to.
          (project.tasks?.length
            ? project.tasks.map((task) => ({
                key: `${project.id}-${task.id}`,
                todo: task.name,
                status: task.status,
                time: task.total_time || formatHMS(task.total_seconds),
              }))
            : [
                {
                  key: `${project.id}-none`,
                  todo: null,
                  status: project.status,
                  time: project.total_time || formatHMS(project.total_seconds),
                },
              ]
          ).map((line, lineIndex) => (
            <tr key={line.key} className="border-b border-[#F1F5F9] bg-white text-[12.5px]">
              <td className="px-3 py-2.5" />
              <td className="px-3 py-2.5">
                {/* The project name is printed once per project block; repeating
                    it on every to-do line is what makes these tables unreadable. */}
                {lineIndex === 0 ? (
                  <div className="flex items-center gap-2">
                    <span
                      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[9px] font-black text-white"
                      style={{ backgroundColor: series[projectIndex % series.length] }}
                    >
                      {initialsOf(project.name)}
                    </span>
                    <span className="truncate font-semibold text-[#0F172A]" title={project.name}>
                      {project.name}
                    </span>
                  </div>
                ) : (
                  <span className="block border-l-2 border-[#F1F5F9] pl-[13px]">&nbsp;</span>
                )}
              </td>
              <td className="px-3 py-2.5">
                {line.todo ? (
                  <span className="text-[#334155]">{line.todo}</span>
                ) : (
                  <span className="italic text-[#94A3B8]">No to-do selected</span>
                )}
              </td>
              <td className="px-3 py-2.5" colSpan={2}>
                <StatusPill status={line.status} />
              </td>
              <td className="px-3 py-2.5 text-right font-mono font-semibold text-[#0F172A]">{line.time}</td>
            </tr>
          ))
        )}
    </>
  );
};

const APPROVAL_TONES: Record<string, string> = {
  pending: "bg-amber-50 text-amber-700",
  approved: "bg-emerald-50 text-emerald-700",
  rejected: "bg-rose-50 text-rose-700",
};

export const MemberTimeTracking: React.FC = () => {
  const { currentUser } = useAuth();
  const { showToast, confirmAction } = useFeedback();

  const [range, setRange] = useState<DateRange>(DEFAULT_RANGE);
  const [tab, setTab] = useState<"log" | "requests">("log");
  const [drawerOpen, setDrawerOpen] = useState(false);
  /** Which day rows are expanded. A Set, because several may be open at once. */
  const [openDays, setOpenDays] = useState<Set<string>>(new Set());

  const toggleDay = (date: string) =>
    setOpenDays((current) => {
      const next = new Set(current);
      if (next.has(date)) next.delete(date);
      else next.add(date);
      return next;
    });

  const { data: log, isLoading, isFetching, isError } = useGetTimeTrackingQuery({
    start_date: range.from,
    end_date: range.to,
    limit: 100,
  });

  const { data: detail } = useGetTimeTrackingDetailsQuery(
    { employeeId: currentUser?.id as number, start_date: range.from, end_date: range.to },
    { skip: !currentUser }
  );

  const { data: requests, isLoading: isRequestsLoading } = useGetManualTimeEntryRequestsQuery({
    start_date: range.from,
    end_date: range.to,
    limit: 100,
  });

  // The manual-entry form's task dropdown is filled from `project.tasks`, so
  // this is the other screen that needs them embedded.
  const { data: projects = [] } = useGetAllProjectsQuery({ includeTasks: true });
  const [createRequest, { isLoading: isSaving }] = useCreateManualTimeEntryRequestMutation();
  const [withdrawRequest] = useDeleteManualTimeEntryRequestMutation();

  // Manual entry form
  const [formProjectId, setFormProjectId] = useState("");
  const [formTaskId, setFormTaskId] = useState("");
  const [formDate, setFormDate] = useState(todayIso());
  const [formClockIn, setFormClockIn] = useState("09:00");
  const [formClockOut, setFormClockOut] = useState("18:00");
  const [formReason, setFormReason] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const formTasks = useMemo(
    () => projects.find((project) => project.id === Number(formProjectId))?.tasks ?? [],
    [projects, formProjectId]
  );

  const rows = log?.items ?? [];
  const totalSeconds = rows.reduce((sum, row) => sum + (row.total_seconds || 0), 0);
  const requestRows = requests?.items ?? [];
  const pendingCount = requestRows.filter((item) => item.approval_status === "pending").length;

  const openDrawer = () => {
    setFormProjectId("");
    setFormTaskId("");
    setFormDate(todayIso());
    setFormClockIn("09:00");
    setFormClockOut("18:00");
    setFormReason("");
    setFormError(null);
    setDrawerOpen(true);
  };

  const minutesBetween = () => {
    const [inH, inM] = formClockIn.split(":").map(Number);
    const [outH, outM] = formClockOut.split(":").map(Number);
    return outH * 60 + outM - (inH * 60 + inM);
  };

  const submitRequest = async () => {
    setFormError(null);
    if (!formProjectId || !formTaskId) {
      setFormError("Pick the project and task this time belongs to.");
      return;
    }
    const minutes = minutesBetween();
    if (minutes <= 0) {
      setFormError("The stop time must be after the start time.");
      return;
    }
    if (!formReason.trim()) {
      setFormError("Say why this time was not tracked automatically — an approver will read it.");
      return;
    }

    try {
      await createRequest({
        project_id: Number(formProjectId),
        task_id: Number(formTaskId),
        work_date: formDate,
        total_seconds: minutes * 60,
        // The member types IST wall-clock times; labelling them "Z" would
        // claim they were UTC and shift the entry by 5h30m.
        start_time: istWallClockToUtcISO(formDate, formClockIn),
        end_time: istWallClockToUtcISO(formDate, formClockOut),
        description: formReason.trim(),
        is_billable: true,
      }).unwrap();
      showToast("Request submitted for approval.", "success");
      setDrawerOpen(false);
      setTab("requests");
    } catch (error: any) {
      setFormError(error?.data?.detail?.message || error?.data?.detail || "Could not submit this request.");
    }
  };

  const withdraw = async (id: number) => {
    if (!(await confirmAction("Withdraw request", "This pending manual time request will be removed."))) return;
    try {
      await withdrawRequest(id).unwrap();
      showToast("Request withdrawn.", "success");
    } catch (error: any) {
      showToast(error?.data?.detail || "Could not withdraw this request.", "error");
    }
  };

  return (
    <MemberShell
      title="My Time Tracking"
      subtitle="Every day you tracked. Open a day for its projects and to-dos."
      actions={
        <>
          <InlineRefreshIndicator active={isFetching && !isLoading} />
          <button
            onClick={openDrawer}
            className="rounded-lg bg-[#0F172A] px-4 py-2 text-xs font-bold text-white shadow-sm transition hover:bg-[#1E293B]"
          >
            Request manual time
          </button>
        </>
      }
    >
      <div className="w-full space-y-6 pb-20">
        <Card>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <DateRangeFilter value={range} onChange={setRange} />
            <div className="flex items-center rounded-lg border border-[#E2E8F0] p-0.5">
              <button
                onClick={() => setTab("log")}
                className={
                  "rounded-[6px] px-4 py-2 text-xs font-bold transition " +
                  (tab === "log" ? "bg-[#2563EB] text-white shadow-sm" : "text-[#64748B] hover:text-[#0F172A]")
                }
              >
                Time log
              </button>
              <button
                onClick={() => setTab("requests")}
                className={
                  "rounded-[6px] px-4 py-2 text-xs font-bold transition " +
                  (tab === "requests" ? "bg-[#2563EB] text-white shadow-sm" : "text-[#64748B] hover:text-[#0F172A]")
                }
              >
                Manual requests
                {pendingCount > 0 && (
                  <span className="ml-1.5 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700">
                    {pendingCount}
                  </span>
                )}
              </button>
            </div>
          </div>
        </Card>

        {isError && <ErrorNote message="Your time log could not be loaded for this range." />}

        {tab === "log" ? (
          <>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <div className="rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
                <div className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">Total Tracked</div>
                <div className="mt-1 font-mono text-3xl font-extrabold text-[#0F172A]">
                  {detail?.summary?.total_time || formatHMS(totalSeconds)}
                </div>
              </div>
              <div className="rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
                <div className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">Days Tracked</div>
                <div className="mt-1 text-3xl font-extrabold text-[#0F172A]">{rows.length}</div>
              </div>
              <div className="rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
                <div className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">Projects</div>
                <div className="mt-1 text-3xl font-extrabold text-[#0F172A]">
                  {detail?.projects?.length ?? 0}
                </div>
              </div>
            </div>

            <Card title="Daily log">
              {isLoading ? (
                <Spinner label="Loading your time log..." />
              ) : rows.length === 0 ? (
                <EmptyState
                  message="You tracked no time in this range."
                  hint="Time recorded by the Monitra desktop app appears here."
                />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[760px] text-left text-[13px]">
                    <thead>
                      <tr className="border-b border-[#E2E8F0] bg-[#FBFCFE] text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">
                        <th className="px-3 py-3">Date</th>
                        <th className="px-3 py-3">Project</th>
                        <th className="px-3 py-3">To-do</th>
                        <th className="px-3 py-3">Start</th>
                        <th className="px-3 py-3">Stop</th>
                        <th className="px-3 py-3 text-right">Total hours</th>
                      </tr>
                    </thead>
                    <tbody>
                      {currentUser &&
                        rows.map((row) => (
                          <DayRow
                            key={row.date}
                            entry={row}
                            employeeId={currentUser.id}
                            open={openDays.has(row.date)}
                            onToggle={() => toggleDay(row.date)}
                          />
                        ))}
                    </tbody>
                    <tfoot>
                      <tr className="border-t-2 border-[#E2E8F0] text-[12px] font-bold text-[#0F172A]">
                        <td className="px-3 py-3" colSpan={5}>
                          {rows.length} day{rows.length === 1 ? "" : "s"} tracked
                        </td>
                        <td className="px-3 py-3 text-right font-mono">
                          {detail?.summary?.total_time || formatHMS(totalSeconds)}
                        </td>
                      </tr>
                    </tfoot>
                  </table>
                </div>
              )}
            </Card>

          </>
        ) : (
          <Card title="My manual time requests">
            {isRequestsLoading ? (
              <Spinner />
            ) : requestRows.length === 0 ? (
              <EmptyState
                message="You have no manual time requests in this range."
                hint="Use “Request manual time” when work was done but not tracked."
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[760px] text-left text-[13px]">
                  <thead>
                    <tr className="border-b border-[#E2E8F0] text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">
                      <th className="px-3 py-3">Date</th>
                      <th className="px-3 py-3">Project / Task</th>
                      <th className="px-3 py-3">Reason</th>
                      <th className="px-3 py-3 text-right">Duration</th>
                      <th className="px-3 py-3">Status</th>
                      <th className="px-3 py-3" />
                    </tr>
                  </thead>
                  <tbody>
                    {requestRows.map((item) => (
                      <tr key={item.id} className="border-b border-[#F1F5F9] last:border-0">
                        <td className="px-3 py-3 font-semibold text-[#0F172A]">{formatISTDate(item.work_date)}</td>
                        <td className="px-3 py-3 text-[#64748B]">
                          <div className="font-semibold text-[#0F172A]">{item.project_name}</div>
                          <div className="text-xs">{item.task_name}</div>
                        </td>
                        <td className="max-w-[220px] truncate px-3 py-3 text-[#64748B]" title={item.description}>
                          {item.description || "—"}
                        </td>
                        <td className="px-3 py-3 text-right font-mono font-bold text-[#0F172A]">
                          {formatHMS(item.total_seconds)}
                        </td>
                        <td className="px-3 py-3">
                          <span
                            className={`inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-bold capitalize ${
                              APPROVAL_TONES[item.approval_status] || "bg-slate-100 text-slate-600"
                            }`}
                          >
                            {item.approval_status}
                          </span>
                          {item.has_conflict && (
                            <span className="ml-1.5 text-[11px] font-bold text-rose-600" title="Overlaps tracked time">
                              conflict
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-3 text-right">
                          {item.approval_status === "pending" && (
                            <button
                              onClick={() => withdraw(item.id)}
                              className="rounded-lg border border-[#E2E8F0] px-3 py-1.5 text-[12px] font-bold text-rose-600 transition hover:bg-rose-50"
                            >
                              Withdraw
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        )}
      </div>

      {/* Slide-over drawer for requesting manual time (matches the admin/HR panel) */}
      <div className={`fixed inset-0 z-50 overflow-hidden ${drawerOpen ? "pointer-events-auto" : "pointer-events-none"}`}>
        <div
          className={`absolute inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity duration-300 ${drawerOpen ? "opacity-100" : "opacity-0"}`}
          onClick={() => setDrawerOpen(false)}
        />
        <div
          className={`absolute inset-y-0 right-0 w-full max-w-md bg-white shadow-2xl transition-transform duration-300 ease-in-out ${drawerOpen ? "translate-x-0" : "translate-x-full"}`}
        >
          <div className="flex h-full flex-col">
            <div className="flex items-start justify-between border-b border-slate-100 px-6 py-5">
              <div>
                <h3 className="text-lg font-bold text-slate-800">Request manual time</h3>
                <p className="mt-1 text-[13px] text-[#64748B]">
                  This is submitted for approval — it is not added to your tracked time until an approver accepts it.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setDrawerOpen(false)}
                className="ml-4 shrink-0 text-slate-400 hover:text-slate-600"
              >
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div className="flex-1 overflow-y-auto">
              <form
                id="member-manual-time-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  void submitRequest();
                }}
                className="space-y-4 p-6"
              >
                <div>
                  <label className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">Project</label>
                  <select
                    value={formProjectId}
                    onChange={(event) => {
                      setFormProjectId(event.target.value);
                      setFormTaskId("");
                    }}
                    className="mt-1.5 w-full rounded-lg border border-[#E2E8F0] px-3 py-2.5 text-[13px] font-semibold text-[#0F172A] outline-none focus:border-[#2563EB]"
                  >
                    <option value="">Select a project…</option>
                    {projects.map((project) => (
                      <option key={project.id} value={project.id}>
                        {project.project_name}
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">Task</label>
                  <select
                    value={formTaskId}
                    onChange={(event) => setFormTaskId(event.target.value)}
                    disabled={!formProjectId}
                    className="mt-1.5 w-full rounded-lg border border-[#E2E8F0] px-3 py-2.5 text-[13px] font-semibold text-[#0F172A] outline-none focus:border-[#2563EB] disabled:bg-[#F8FAFC]"
                  >
                    <option value="">{formProjectId ? "Select a task…" : "Pick a project first"}</option>
                    {formTasks.map((task) => (
                      <option key={task.id} value={task.id}>
                        {task.name}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="grid grid-cols-3 gap-3">
                  <div>
                    <label className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">Date</label>
                    <input
                      type="date"
                      value={formDate}
                      max={todayIso()}
                      onChange={(event) => setFormDate(event.target.value)}
                      className="mt-1.5 w-full rounded-lg border border-[#E2E8F0] px-3 py-2.5 text-[13px] font-semibold text-[#0F172A] outline-none focus:border-[#2563EB]"
                    />
                  </div>
                  <div>
                    <label className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">Start</label>
                    <input
                      type="time"
                      value={formClockIn}
                      onChange={(event) => setFormClockIn(event.target.value)}
                      className="mt-1.5 w-full rounded-lg border border-[#E2E8F0] px-3 py-2.5 text-[13px] font-semibold text-[#0F172A] outline-none focus:border-[#2563EB]"
                    />
                  </div>
                  <div>
                    <label className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">Stop</label>
                    <input
                      type="time"
                      value={formClockOut}
                      onChange={(event) => setFormClockOut(event.target.value)}
                      className="mt-1.5 w-full rounded-lg border border-[#E2E8F0] px-3 py-2.5 text-[13px] font-semibold text-[#0F172A] outline-none focus:border-[#2563EB]"
                    />
                  </div>
                </div>

                <div>
                  <label className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">Reason</label>
                  <textarea
                    value={formReason}
                    onChange={(event) => setFormReason(event.target.value)}
                    rows={3}
                    placeholder="Why was this time not tracked automatically?"
                    className="mt-1.5 w-full resize-none rounded-lg border border-[#E2E8F0] px-3 py-2.5 text-[13px] text-[#0F172A] outline-none focus:border-[#2563EB]"
                  />
                </div>

                <div className="flex items-center justify-between rounded-lg bg-slate-50 px-4 py-3">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-slate-500">Total logged</span>
                  <span className="font-mono text-sm font-black text-slate-800">
                    {minutesBetween() > 0 ? formatHMS(minutesBetween() * 60) : "-"}
                  </span>
                </div>

                {formError && (
                  <p className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-[13px] font-semibold leading-5 text-rose-700" role="alert">
                    {formError}
                  </p>
                )}
              </form>
            </div>

            <div className="border-t border-slate-100 bg-slate-50 p-6">
              <div className="flex gap-3">
                <button
                  type="button"
                  onClick={() => setDrawerOpen(false)}
                  className="flex-1 rounded-lg border border-slate-200 bg-white py-2.5 text-sm font-bold text-slate-600 transition hover:bg-slate-50"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  form="member-manual-time-form"
                  disabled={isSaving}
                  className="flex-1 rounded-lg bg-[#2563EB] py-2.5 text-sm font-bold text-white shadow-md transition hover:bg-[#1D4ED8] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {isSaving ? "Submitting…" : "Submit request"}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </MemberShell>
  );
};
