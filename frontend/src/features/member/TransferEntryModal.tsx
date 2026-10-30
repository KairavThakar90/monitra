import React, { useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useSplitTimeEntryMutation, useTransferTimeEntryMutation } from "../../store/api/timeEntryApi";
import type { TimeEntry } from "../../store/api/timeEntryApi";
import { useFeedback } from "../../components/FeedbackProvider";
import { formatHMS, formatISTTime } from "../../utils/duration";
import { formatApiError } from "../../api/utils";
import { validateDescription, validateInteger } from "../../validation";

/**
 * Move one already-recorded entry to a different project/task -- all of it, or
 * only part of it.
 *
 * **Move the whole entry** is the original "Change Project": only which
 * project/task the session counts against changes. **Split between projects**
 * divides the session instead: the entry keeps the earlier part of its
 * interval and a new entry takes the later part on the destination, so the two
 * together are exactly the time that was recorded. Neither mode can add or
 * remove tracked time: the payloads have no start, end or duration field to
 * send (a split sends only *how much* to move, and the backend cuts the
 * interval -- `docs/TIMING_MODEL.md`).
 *
 * The destination picker mirrors the manual-entry drawer's own cascading
 * project -> task selects, so the flows feel like one feature.
 *
 * Shared by "My Time Tracking" and "Time Entries"; it used to be a copy in each.
 */

export type ProjectOption = {
  id: number;
  project_name: string;
  tasks: { id: number; name: string }[] | null;
};

const projectNameById = (projects: ProjectOption[], id: number) =>
  projects.find((project) => project.id === id)?.project_name ?? `Project #${id}`;

const taskNameById = (projects: ProjectOption[], projectId: number, taskId: number) =>
  projects.find((project) => project.id === projectId)?.tasks?.find((task) => task.id === taskId)?.name
  ?? `Task #${taskId}`;

type Mode = "whole" | "split";

const fieldLabel = "text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]";
const selectClass =
  "mt-1.5 w-full rounded-lg border border-[#E2E8F0] px-3 py-2.5 text-[13px] font-semibold text-[#0F172A] outline-none focus:border-[#2563EB]";
const unitInputClass =
  "w-full rounded-lg border border-[#E2E8F0] px-3 py-2.5 text-center text-[13px] font-semibold tabular-nums text-[#0F172A] outline-none focus:border-[#2563EB]";

/** `HH:MM:SS` parts of a whole number of seconds, as the strings the three boxes hold. */
const toParts = (seconds: number) => ({
  hours: String(Math.floor(seconds / 3600)),
  minutes: String(Math.floor((seconds % 3600) / 60)),
  seconds: String(seconds % 60),
});

/** The IST clock time `secondsBeforeEnd` before the entry's end, for the preview. */
const istTimeBeforeEnd = (end: string | null, secondsBeforeEnd: number) =>
  end ? formatISTTime(new Date(Date.parse(end) - secondsBeforeEnd * 1000).toISOString()) : "";

export const TransferEntryModal: React.FC<{
  entry: TimeEntry;
  projects: ProjectOption[];
  onClose: () => void;
}> = ({ entry, projects, onClose }) => {
  const { showToast } = useFeedback();
  const [transfer, { isLoading: isTransferring }] = useTransferTimeEntryMutation();
  const [split, { isLoading: isSplitting }] = useSplitTimeEntryMutation();
  const isLoading = isTransferring || isSplitting;

  const [mode, setMode] = useState<Mode>("whole");
  const [toProjectId, setToProjectId] = useState("");
  const [toTaskId, setToTaskId] = useState("");
  const [reason, setReason] = useState("");
  const [hours, setHours] = useState("0");
  const [minutes, setMinutes] = useState("0");
  const [seconds, setSeconds] = useState("0");
  const [error, setError] = useState<string | null>(null);

  const toTasks = useMemo(
    () => projects.find((project) => project.id === Number(toProjectId))?.tasks ?? [],
    [projects, toProjectId]
  );

  const currentProjectName = projectNameById(projects, entry.project_id);
  const currentTaskName = taskNameById(projects, entry.project_id, entry.task_id);
  const destinationProjectName = toProjectId ? projectNameById(projects, Number(toProjectId)) : "";
  const destinationTaskName = toProjectId && toTaskId ? taskNameById(projects, Number(toProjectId), Number(toTaskId)) : "";

  // Some time must stay on the entry, and time already deducted from it (idle time
  // thrown away) stays with it too, so what can move is capped at its net time.
  const mostMovable = Math.max(0, Math.min(entry.total_seconds - 1, entry.net_seconds));
  const cannotSplit = mostMovable < 1 ? "This entry is too short to split." : null;

  const parsed = useMemo(() => {
    const h = validateInteger(hours, { fieldLabel: "Hours", minimum: 0 });
    const m = validateInteger(minutes, { fieldLabel: "Minutes", minimum: 0, maximum: 59 });
    const s = validateInteger(seconds, { fieldLabel: "Seconds", minimum: 0, maximum: 59 });
    if (!h.ok) return { ok: false as const, error: h.error };
    if (!m.ok) return { ok: false as const, error: m.error };
    if (!s.ok) return { ok: false as const, error: s.error };
    return { ok: true as const, total: h.value * 3600 + m.value * 60 + s.value };
  }, [hours, minutes, seconds]);

  const moveSeconds = parsed.ok ? parsed.total : 0;
  const splitIsValid = parsed.ok && moveSeconds >= 1 && moveSeconds <= mostMovable;
  const staysSeconds = entry.total_seconds - moveSeconds;

  const setMoveSeconds = (value: number) => {
    const next = toParts(value);
    setHours(next.hours);
    setMinutes(next.minutes);
    setSeconds(next.seconds);
  };

  const submit = async () => {
    setError(null);
    if (!toProjectId || !toTaskId) {
      setError("Choose a destination project and task.");
      return;
    }
    if (Number(toProjectId) === entry.project_id && Number(toTaskId) === entry.task_id) {
      setError(
        mode === "split"
          ? "Choose a different project or task to move that time to."
          : "That is the project and task this entry already has."
      );
      return;
    }
    // The reason is prose another person reads: the same rule the backend enforces, checked here so a
    // refused value is explained at the field instead of arriving as a rejected request.
    const checkedReason = validateDescription(reason, { fieldLabel: "Reason" });
    if (!checkedReason.ok) {
      setError(checkedReason.error);
      return;
    }

    if (mode === "split") {
      if (!parsed.ok) {
        setError(parsed.error);
        return;
      }
      if (moveSeconds < 1) {
        setError("Enter how much time to move to the other project.");
        return;
      }
      if (moveSeconds > mostMovable) {
        setError(
          `You can move at most ${formatHMS(mostMovable)}: some time has to stay on ${currentProjectName}.`
        );
        return;
      }
      try {
        await split({
          id: entry.id,
          to_project_id: Number(toProjectId),
          to_task_id: Number(toTaskId),
          move_seconds: moveSeconds,
          reason: checkedReason.value || undefined,
        }).unwrap();
        showToast(
          `Moved ${formatHMS(moveSeconds)} to ${destinationProjectName} / ${destinationTaskName}.`,
          "success"
        );
        onClose();
      } catch (err: any) {
        setError(formatApiError(err?.data, "Could not split this time entry."));
      }
      return;
    }

    try {
      await transfer({
        id: entry.id,
        to_project_id: Number(toProjectId),
        to_task_id: Number(toTaskId),
        reason: checkedReason.value || undefined,
      }).unwrap();
      showToast("Time entry transferred.", "success");
      onClose();
    } catch (err: any) {
      setError(formatApiError(err?.data, "Could not transfer this time entry."));
    }
  };

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Change Project"
        className="max-h-[92vh] w-full max-w-md overflow-y-auto rounded-xl bg-white p-6 shadow-xl"
      >
        <h3 className="text-lg font-bold text-slate-800">Change Project</h3>
        <p className="mt-1 text-[13px] text-[#64748B]">
          {mode === "whole" ? (
            <>
              Moves this {entry.elapsed_time || formatHMS(entry.total_seconds)} entry
              ({formatISTTime(entry.start_time)}–{formatISTTime(entry.end_time)}) to a
              different project or task. The recorded time and duration do not change.
            </>
          ) : (
            <>
              Divides this {entry.elapsed_time || formatHMS(entry.total_seconds)} entry
              ({formatISTTime(entry.start_time)}–{formatISTTime(entry.end_time)}) between two
              projects or tasks. Together they add up to exactly the time you recorded.
            </>
          )}
        </p>

        <div
          role="radiogroup"
          aria-label="How to move this entry"
          className="mt-4 grid grid-cols-2 gap-1 rounded-lg bg-[#F1F5F9] p-1"
        >
          {(
            [
              ["whole", "Move whole entry"],
              ["split", "Split between projects"],
            ] as const
          ).map(([value, label]) => {
            const selected = mode === value;
            const disabled = value === "split" && cannotSplit !== null;
            return (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={selected}
                disabled={disabled}
                title={disabled ? cannotSplit ?? undefined : undefined}
                onClick={() => {
                  setMode(value);
                  setError(null);
                }}
                className={
                  "rounded-md px-3 py-2 text-[12.5px] font-bold transition disabled:cursor-not-allowed disabled:opacity-50 " +
                  (selected ? "bg-white text-[#2563EB] shadow-sm" : "text-[#64748B] hover:text-[#0F172A]")
                }
              >
                {label}
              </button>
            );
          })}
        </div>
        {cannotSplit && <p className="mt-2 text-[12px] text-[#94A3B8]">{cannotSplit}</p>}

        <div className="mt-4 rounded-lg bg-[#F8FAFC] px-3 py-2 text-[12.5px] text-[#475569]">
          Currently: <span className="font-semibold text-[#0F172A]">{currentProjectName}</span>
          {" / "}
          <span className="font-semibold text-[#0F172A]">{currentTaskName}</span>
        </div>

        <div className="mt-4 space-y-4">
          <div>
            <label className={fieldLabel}>Destination project</label>
            <select
              value={toProjectId}
              onChange={(event) => {
                setToProjectId(event.target.value);
                setToTaskId("");
              }}
              className={selectClass}
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
            <label className={fieldLabel}>Destination task</label>
            <select
              value={toTaskId}
              onChange={(event) => setToTaskId(event.target.value)}
              disabled={!toProjectId}
              className={selectClass + " disabled:bg-[#F8FAFC]"}
            >
              <option value="">{toProjectId ? "Select a task…" : "Pick a project first"}</option>
              {toTasks.map((task) => (
                <option key={task.id} value={task.id}>
                  {task.name}
                </option>
              ))}
            </select>
          </div>

          {mode === "split" && (
            <div>
              <div className="flex items-center justify-between">
                <label className={fieldLabel}>Time to move to the destination</label>
                <button
                  type="button"
                  onClick={() => setMoveSeconds(Math.min(mostMovable, Math.floor(entry.total_seconds / 2)))}
                  className="text-[11px] font-bold uppercase tracking-wider text-[#2563EB] hover:underline"
                >
                  Half
                </button>
              </div>
              <div className="mt-1.5 grid grid-cols-3 gap-2">
                {(
                  [
                    ["Hours", hours, setHours, "hours"],
                    ["Minutes", minutes, setMinutes, "min"],
                    ["Seconds", seconds, setSeconds, "sec"],
                  ] as const
                ).map(([name, value, setter, unit]) => (
                  <div key={name}>
                    <input
                      type="text"
                      inputMode="numeric"
                      autoComplete="off"
                      aria-label={`${name} to move`}
                      value={value}
                      onChange={(event) => setter(event.target.value)}
                      className={unitInputClass}
                    />
                    <div className="mt-1 text-center text-[10.5px] font-semibold uppercase tracking-wider text-[#94A3B8]">
                      {unit}
                    </div>
                  </div>
                ))}
              </div>

              <div className="mt-3 space-y-1.5 rounded-lg border border-[#E2E8F0] px-3 py-2.5 text-[12.5px]" aria-live="polite">
                {splitIsValid ? (
                  <>
                    <div className="flex items-center justify-between gap-3">
                      <span className="min-w-0 truncate text-[#475569]">
                        Stays on <span className="font-semibold text-[#0F172A]">{currentProjectName} / {currentTaskName}</span>
                      </span>
                      <span className="shrink-0 font-mono font-bold text-[#0F172A]">
                        {formatHMS(staysSeconds)}
                        <span className="ml-2 font-sans font-medium text-[#94A3B8]">
                          {formatISTTime(entry.start_time)}–{istTimeBeforeEnd(entry.end_time, moveSeconds)}
                        </span>
                      </span>
                    </div>
                    <div className="flex items-center justify-between gap-3">
                      <span className="min-w-0 truncate text-[#475569]">
                        Moves to{" "}
                        <span className="font-semibold text-[#0F172A]">
                          {destinationProjectName
                            ? `${destinationProjectName}${destinationTaskName ? ` / ${destinationTaskName}` : ""}`
                            : "the destination"}
                        </span>
                      </span>
                      <span className="shrink-0 font-mono font-bold text-[#2563EB]">
                        {formatHMS(moveSeconds)}
                        <span className="ml-2 font-sans font-medium text-[#94A3B8]">
                          {istTimeBeforeEnd(entry.end_time, moveSeconds)}–{formatISTTime(entry.end_time)}
                        </span>
                      </span>
                    </div>
                  </>
                ) : (
                  <span className="text-[#94A3B8]">
                    {!parsed.ok
                      ? parsed.error
                      : moveSeconds > mostMovable
                        ? `At most ${formatHMS(mostMovable)} can be moved; some time has to stay on this project.`
                        : "Enter how much time to move to the destination."}
                  </span>
                )}
              </div>
            </div>
          )}

          <div>
            <label className={fieldLabel}>Reason (optional)</label>
            <input
              type="text"
              value={reason}
              maxLength={500}
              onChange={(event) => setReason(event.target.value)}
              placeholder={
                mode === "split"
                  ? "e.g. spent the second half helping on another project"
                  : "e.g. tracked against the wrong project by mistake"
              }
              className="mt-1.5 w-full rounded-lg border border-[#E2E8F0] px-3 py-2.5 text-[13px] text-[#0F172A] outline-none focus:border-[#2563EB]"
            />
          </div>
        </div>

        {error && <p className="mt-3 text-[12.5px] font-semibold text-rose-600">{error}</p>}

        <div className="mt-6 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-[#E2E8F0] px-4 py-2 text-[13px] font-bold text-[#475569] hover:bg-[#F8FAFC]"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={isLoading}
            className="rounded-lg bg-[#2563EB] px-4 py-2 text-[13px] font-bold text-white hover:bg-[#1D4ED8] disabled:opacity-60"
          >
            {mode === "split"
              ? isLoading ? "Splitting…" : "Confirm split"
              : isLoading ? "Transferring…" : "Confirm transfer"}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
};
