import React, { useEffect, useMemo, useRef, useState } from "react";
import { ErrorNote, Spinner } from "../member/MemberUi";
import { useFeedback } from "../../components/FeedbackProvider";
import { Pagination } from "../../components/Pagination";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { formatApiError } from "../../api/utils";
import { FieldError, SEARCH_MAX_LENGTH, validateInteger, validateSearchTerm } from "../../validation";
import { useGetAllMembersQuery, useUpdateMemberMutation, type Member } from "../../store/api/membersApi";
import { isCapturableMember, idleIsOn } from "../../store/api/screenshotSettingsApi";

/**
 * Screenshot Management -> Idle management.
 *
 * One row per active member: how often a screenshot is taken for that member
 * (`capture_frequency`, minutes), whether the desktop watches them for
 * inactivity (`idle_enabled`) and after how many minutes of it (`idle_minutes`).
 * All three already live on the member and are saved through the ordinary member
 * update (`PATCH /members/{id}`). Idle detection is set only here -- User
 * Management no longer edits it. The screenshot frequency is the same field User
 * Management's edit drawer writes, so either place may change it. A save sends
 * only the field(s) that changed.
 *
 * The 1-120 minute idle range is the server's (`IdleMinutes`), and the capture
 * frequency is any whole number of minutes from 1 (`CaptureFrequencyMinutes`, no
 * ceiling); the page refuses only what the server would refuse. Clients and the release service account are never
 * listed. Absent `idle_enabled` / `idle_minutes` mean the defaults (on, 5).
 */

const MIN_IDLE_MINUTES = 1;
const MAX_IDLE_MINUTES = 120;
const DEFAULT_IDLE_MINUTES = 5;
const MIN_CAPTURE_MINUTES = 1;
const DEFAULT_CAPTURE_MINUTES = 10;

const inputClass =
  "w-full rounded-lg border bg-white px-3 py-2 text-sm text-slate-800 outline-none transition focus:border-[#38bdf8] focus:ring-2 focus:ring-[#38bdf8]/15";


const errorData = (error: unknown) => (error as { data?: unknown } | undefined)?.data;
const serverMinutes = (member: Member) => member.idle_minutes ?? DEFAULT_IDLE_MINUTES;
const serverCapture = (member: Member) => member.capture_frequency ?? DEFAULT_CAPTURE_MINUTES;

const IdleSwitch: React.FC<{ name: string; on: boolean; busy: boolean; onChange: (next: boolean) => void }> = ({
  name,
  on,
  busy,
  onChange,
}) => (
  <div className="flex items-center gap-2">
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={`Idle detection for ${name}`}
      disabled={busy}
      onClick={() => onChange(!on)}
      className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500/40 disabled:cursor-not-allowed disabled:opacity-60 ${
        on ? "bg-blue-600" : "bg-slate-300"
      }`}
    >
      <span
        className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
          on ? "translate-x-5" : "translate-x-0.5"
        }`}
      />
    </button>
    <span className={"text-xs font-bold " + (on ? "text-blue-700" : "text-slate-500")}>{on ? "On" : "Off"}</span>
  </div>
);

const IdleRow: React.FC<{ member: Member }> = ({ member }) => {
  const { showToast } = useFeedback();
  const [updateMember] = useUpdateMemberMutation();

  const savedEnabled = idleIsOn(member);
  const savedMinutes = serverMinutes(member);
  const savedCapture = serverCapture(member);

  const [enabled, setEnabled] = useState(savedEnabled);
  const [minutes, setMinutes] = useState(String(savedMinutes));
  const [capture, setCapture] = useState(String(savedCapture));
  const [busy, setBusy] = useState(false);

  // What the server holds is the truth: after a save, a rollback, or another
  // administrator's change the row shows that. Keyed on the values, so a
  // refetch that returns the same thing never overwrites typing.
  const serverKey = `${savedEnabled}|${savedMinutes}|${savedCapture}`;
  const syncedKey = useRef(serverKey);
  useEffect(() => {
    if (syncedKey.current === serverKey) return;
    syncedKey.current = serverKey;
    setEnabled(savedEnabled);
    setMinutes(String(savedMinutes));
    setCapture(String(savedCapture));
  }, [serverKey, savedEnabled, savedMinutes, savedCapture]);

  const check = validateInteger(minutes, {
    fieldLabel: "Idle time",
    minimum: MIN_IDLE_MINUTES,
    maximum: MAX_IDLE_MINUTES,
  });
  const captureCheck = validateInteger(capture, { fieldLabel: "Screenshot frequency", minimum: MIN_CAPTURE_MINUTES });
  const enabledChanged = enabled !== savedEnabled;
  // Compared as text so a stored value outside the range is "unchanged" until it is edited.
  const minutesChanged = minutes.trim() !== String(savedMinutes);
  const captureChanged = capture.trim() !== String(savedCapture);
  const minutesError = minutesChanged && !check.ok ? check.error : null;
  const captureError = captureChanged && !captureCheck.ok ? captureCheck.error : null;
  const dirty =
    (enabledChanged || minutesChanged || captureChanged) &&
    (!minutesChanged || check.ok) &&
    (!captureChanged || captureCheck.ok);

  const save = async () => {
    if (!dirty || busy) return;
    const body: Partial<Member> = {};
    if (enabledChanged) body.idle_enabled = enabled;
    if (minutesChanged && check.ok) body.idle_minutes = check.value;
    if (captureChanged && captureCheck.ok) body.capture_frequency = captureCheck.value;
    setBusy(true);
    try {
      await updateMember({ id: member.id, body }).unwrap();
      showToast(`Idle management saved for ${member.name}.`, "success");
    } catch (exc) {
      // The cached row has already been put back; put the row's fields back too.
      setEnabled(savedEnabled);
      setMinutes(String(savedMinutes));
      setCapture(String(savedCapture));
      showToast(formatApiError(errorData(exc), `Could not save idle management for ${member.name}.`), "error");
    } finally {
      setBusy(false);
    }
  };

  const errorId = `idle-minutes-${member.id}-error`;
  const captureErrorId = `capture-minutes-${member.id}-error`;

  return (
    <tr>
      <td className="px-4 py-3">
        <div className="font-bold text-slate-800">{member.name || "-"}</div>
        <div className="text-xs text-slate-500">{member.email}</div>
      </td>
      <td className="px-4 py-3 capitalize text-slate-600">{(member.role || "").replace(/_/g, " ")}</td>
      <td className="px-4 py-3">
        <div className="w-28">
          <input maxLength={6}
            type="text"
            inputMode="numeric"
            autoComplete="off"
            aria-label={`Screenshot frequency in minutes for ${member.name}`}
            value={capture}
            disabled={busy}
            onChange={(event) => setCapture(event.target.value)}
            aria-invalid={captureError ? true : undefined}
            aria-describedby={captureError ? captureErrorId : undefined}
            className={inputClass + (captureError ? " border-rose-300" : " border-slate-200")}
          />
          <FieldError id={captureErrorId} message={captureError} />
        </div>
      </td>
      <td className="px-4 py-3">
        <IdleSwitch name={member.name} on={enabled} busy={busy} onChange={setEnabled} />
      </td>
      <td className="px-4 py-3">
        <div className="w-28">
          <input maxLength={4}
            type="text"
            inputMode="numeric"
            autoComplete="off"
            aria-label={`Idle time in minutes for ${member.name}`}
            value={minutes}
            disabled={busy}
            onChange={(event) => setMinutes(event.target.value)}
            aria-invalid={minutesError ? true : undefined}
            aria-describedby={minutesError ? errorId : undefined}
            className={
              inputClass +
              (minutesError ? " border-rose-300" : " border-slate-200") +
              (enabled ? "" : " bg-slate-50 text-slate-400")
            }
          />
          <FieldError id={errorId} message={minutesError} />
        </div>
      </td>
      <td className="px-4 py-3 text-right">
        <button
          type="button"
          aria-label={`Save idle settings for ${member.name}`}
          disabled={!dirty || busy}
          onClick={() => void save()}
          className="rounded-lg bg-gradient-to-r from-[#0ea5e9] via-[#3b82f6] to-[#8b5cf6] px-4 py-1.5 text-[12px] font-bold text-white shadow-sm transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save"}
        </button>
      </td>
    </tr>
  );
};

export const IdleDetection: React.FC = () => {
  const members = useGetAllMembersQuery();

  const [search, setSearch] = useState("");
  const [searchError, setSearchError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);

  // The people a desktop runs for: active members, never a client or the release service account.
  const people = useMemo(() => (members.data ?? []).filter(isCapturableMember), [members.data]);

  const searchCheck = validateSearchTerm(search, { fieldLabel: "Search" });
  const term = searchCheck.ok ? searchCheck.value.trim().toLowerCase() : "";
  const filtered = useMemo(
    () => (term ? people.filter((m) => `${m.name} ${m.email}`.toLowerCase().includes(term)) : people),
    [people, term],
  );
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const rows = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const noMembers = !members.isLoading && !members.isError && people.length === 0;

  return (
    <section aria-labelledby="idle-detection-heading">
      <div className="rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
        <h2 id="idle-detection-heading" className="sr-only">
          Idle detection
        </h2>
        <div className="mb-4 flex items-start justify-between gap-3">
          <p className="text-sm text-slate-600">
            When idle detection is off, the desktop does not watch this member for inactivity. A change reaches a
            running desktop within a few seconds while it is connected, and within three minutes otherwise.
          </p>
          <InlineRefreshIndicator active={members.isFetching && !members.isLoading} label="Checking" />
        </div>

        {members.isLoading ? (
          <Spinner label="Loading members…" />
        ) : members.isError ? (
          <ErrorNote message="Could not load the members." onRetry={() => void members.refetch()} />
        ) : noMembers ? (
          <p className="rounded-xl border-2 border-dashed border-slate-200 px-6 py-10 text-center text-sm font-semibold text-slate-500">
            There are no active members yet.
          </p>
        ) : (
          <>
            <div className="mb-4 min-w-[220px]">
              <input
                type="text"
                aria-label="Search members"
                placeholder="Search members by name or email..."
                value={search}
                maxLength={SEARCH_MAX_LENGTH}
                aria-invalid={searchError ? true : undefined}
                aria-describedby={searchError ? "idle-member-search-error" : undefined}
                onChange={(event) => {
                  const next = event.target.value;
                  setSearch(next);
                  const result = validateSearchTerm(next, { fieldLabel: "Search" });
                  setSearchError(result.ok ? null : result.error);
                  setPage(1);
                }}
                className={inputClass + (searchError ? " border-rose-300" : " border-slate-200")}
              />
              <FieldError id="idle-member-search-error" message={searchError} />
            </div>

            <div className="overflow-x-auto rounded-xl border border-slate-200">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-slate-500">
                  <tr>
                    <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Member</th>
                    <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Role</th>
                    <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Screenshot frequency (min)</th>
                    <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Idle detection</th>
                    <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Idle time (min)</th>
                    <th className="px-4 py-3 text-right text-[11px] font-bold uppercase tracking-wider">
                      <span className="sr-only">Save</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {rows.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="px-6 py-10 text-center text-sm font-semibold text-slate-500">
                        No members match your search.
                      </td>
                    </tr>
                  ) : (
                    rows.map((member) => <IdleRow key={member.id} member={member} />)
                  )}
                </tbody>
              </table>
            </div>
            <p className="mt-2 text-xs text-slate-500">
              Screenshot frequency is a whole number of minutes, at least {MIN_CAPTURE_MINUTES}: one screenshot is taken at
              a random moment in every interval while the member is tracking time. Idle time is a whole number of minutes,
              from {MIN_IDLE_MINUTES} to {MAX_IDLE_MINUTES}.
            </p>
            <Pagination
              page={currentPage}
              totalPages={totalPages}
              totalItems={filtered.length}
              limit={pageSize}
              setPage={setPage}
              setLimit={setPageSize}
              noun="members"
              className="mt-4"
            />
          </>
        )}
      </div>
    </section>
  );
};
