import React, { useEffect, useMemo, useState } from "react";
import { V2Shell } from "../dashboard/v2/V2Shell";
import { Card, ErrorNote, Spinner } from "../member/MemberUi";
import { useFeedback } from "../../components/FeedbackProvider";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { formatApiError } from "../../api/utils";
import { FieldError, FormErrorBanner, validateName, validatePlainText, validateTimeOfDay } from "../../validation";
import {
  ALL_WEEKDAYS,
  WEEKDAY_SHORT,
  describeWeekdays,
  useCreateCustomNotificationMutation,
  useDeleteCustomNotificationMutation,
  useGetDesktopNotificationsQuery,
  useUpdateBuiltinNotificationMutation,
  useUpdateCustomNotificationMutation,
  type BuiltinNotification,
  type CustomNotification,
} from "../../store/api/desktopNotificationsApi";

/**
 * Admin Settings -> Desktop Notifications.
 *
 * An administrator decides what the desktop app shows and when
 * (docs/DESKTOP_NOTIFICATIONS.md). The checkbox is the whole "send / don't
 * send" decision: checked, the desktop shows it at its time on its days;
 * unchecked, it is never sent. Two kinds share one list shape -- the desktop's
 * built-in reminders (switch, move, restrict to weekdays) and custom
 * notifications the administrator writes.
 *
 * Times are IST, said on the page because the desktop reads them as IST
 * whatever this browser's timezone is. The route is administrators-only, and so
 * is every endpoint behind it.
 */

const TITLE_MAX_LENGTH = 80;
const MESSAGE_MAX_LENGTH = 300;

type Editing =
  | { kind: "builtin"; item: BuiltinNotification }
  | { kind: "custom"; item: CustomNotification }
  | { kind: "new" };

const formatWhen = (iso: string | null | undefined) => {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
};

const cadenceOf = (item: BuiltinNotification) =>
  item.kind === "daily" ? `${item.time} IST` : `Every ${item.every_minutes} min while working`;

/** The seven day toggles, shared by the dialog's one schedule control. */
const WeekdayPicker: React.FC<{ value: number[]; onChange: (days: number[]) => void; error?: string | null }> = ({
  value,
  onChange,
  error,
}) => (
  <div>
    <div role="group" aria-label="Days" className="flex flex-wrap gap-1.5">
      {WEEKDAY_SHORT.map((label, day) => {
        const on = value.includes(day);
        return (
          <button
            key={label}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(on ? value.filter((d) => d !== day) : [...value, day].sort((a, b) => a - b))}
            className={
              "h-9 min-w-12 rounded-lg border px-3 text-[12px] font-bold transition " +
              (on
                ? "border-[#2563EB] bg-[#2563EB] text-white"
                : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50")
            }
          >
            {label}
          </button>
        );
      })}
    </div>
    <div className="mt-2 flex gap-3 text-[11px] font-bold uppercase tracking-wider">
      <button type="button" onClick={() => onChange([0, 1, 2, 3, 4])} className="text-[#2563EB] hover:underline">
        Mon&ndash;Fri
      </button>
      <button type="button" onClick={() => onChange([...ALL_WEEKDAYS])} className="text-[#2563EB] hover:underline">
        Every day
      </button>
    </div>
    <FieldError message={error} />
  </div>
);

const inputClass =
  "w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 outline-none transition focus:border-[#38bdf8] focus:ring-2 focus:ring-[#38bdf8]/15";

/** Create a custom notification, or edit a custom or built-in one. */
const NotificationDialog: React.FC<{ editing: Editing; onClose: () => void }> = ({ editing, onClose }) => {
  const { showToast } = useFeedback();
  const [updateBuiltin, { isLoading: savingBuiltin }] = useUpdateBuiltinNotificationMutation();
  const [createCustom, { isLoading: creating }] = useCreateCustomNotificationMutation();
  const [updateCustom, { isLoading: savingCustom }] = useUpdateCustomNotificationMutation();
  const saving = savingBuiltin || creating || savingCustom;

  const builtin = editing.kind === "builtin" ? editing.item : null;
  const custom = editing.kind === "custom" ? editing.item : null;
  const isCustom = editing.kind !== "builtin";

  const [title, setTitle] = useState(custom?.title ?? "");
  const [message, setMessage] = useState(custom?.message ?? "");
  const [time, setTime] = useState(custom?.time ?? builtin?.time ?? "10:00");
  const [weekdays, setWeekdays] = useState<number[]>(custom?.weekdays ?? builtin?.weekdays ?? [0, 1, 2, 3, 4]);
  const [enabled, setEnabled] = useState(custom?.enabled ?? true);
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const showsTime = isCustom || builtin?.kind === "daily";

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const next: Record<string, string | null> = {};
    const titleCheck = isCustom ? validateName(title, { fieldLabel: "Title", maxLength: TITLE_MAX_LENGTH }) : null;
    const messageCheck = isCustom
      ? validatePlainText(message, { fieldLabel: "Message", maxLength: MESSAGE_MAX_LENGTH, required: true })
      : null;
    const timeCheck = showsTime ? validateTimeOfDay(time, { fieldLabel: "Time" }) : null;
    if (titleCheck && !titleCheck.ok) next.title = titleCheck.error;
    if (messageCheck && !messageCheck.ok) next.message = messageCheck.error;
    if (timeCheck && !timeCheck.ok) next.time = timeCheck.error;
    if (weekdays.length === 0) next.weekdays = "Choose at least one day.";
    setErrors(next);
    setFormError(null);
    if (Object.values(next).some(Boolean)) return;

    try {
      if (builtin) {
        // Only what actually changed, so an unchanged field is never re-sent.
        const body: { time?: string; weekdays?: number[] } = {};
        if (builtin.kind === "daily" && timeCheck?.ok && timeCheck.value !== builtin.time) body.time = timeCheck.value;
        if (weekdays.join(",") !== builtin.weekdays.join(",")) body.weekdays = weekdays;
        if (Object.keys(body).length) await updateBuiltin({ key: builtin.key, body }).unwrap();
      } else {
        const draft = {
          title: (titleCheck as { value: string }).value,
          message: (messageCheck as { value: string }).value,
          time: (timeCheck as { value: string }).value,
          weekdays,
          enabled,
        };
        if (custom) await updateCustom({ id: custom.id, body: draft }).unwrap();
        else await createCustom(draft).unwrap();
      }
      showToast(custom || builtin ? "Notification updated." : "Notification created.", "success");
      onClose();
    } catch (exc) {
      setFormError(formatApiError((exc as { data?: unknown })?.data, "Could not save the notification."));
    }
  };

  const resetToDefault = () => {
    if (!builtin) return;
    setTime(builtin.default_time ?? "");
    setWeekdays([...ALL_WEEKDAYS]);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-sm"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <form
        onSubmit={submit}
        noValidate
        role="dialog"
        aria-modal="true"
        aria-label={custom ? "Edit notification" : builtin ? "Edit reminder" : "New notification"}
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-slate-200 bg-white shadow-2xl"
      >
        <div className="border-b border-slate-100 bg-slate-50 px-6 py-5">
          <h2 className="text-xl font-black text-slate-800">
            {custom ? "Edit notification" : builtin ? builtin.label : "New notification"}
          </h2>
          <p className="mt-1 text-sm font-semibold text-slate-500">
            {builtin
              ? builtin.description
              : "Shown on every desktop at this time, on the days you choose. Times are IST."}
          </p>
        </div>

        <div className="space-y-5 p-6">
          <FormErrorBanner message={formError} />

          {isCustom && (
            <>
              <div>
                <label htmlFor="notification-title" className="mb-1.5 block text-[11px] font-bold uppercase tracking-wider text-slate-500">
                  Title
                </label>
                <input
                  id="notification-title"
                  value={title}
                  maxLength={TITLE_MAX_LENGTH}
                  onChange={(event) => setTitle(event.target.value)}
                  placeholder="e.g. Daily standup"
                  aria-invalid={errors.title ? true : undefined}
                  className={inputClass}
                />
                <FieldError message={errors.title} />
              </div>
              <div>
                <label htmlFor="notification-message" className="mb-1.5 block text-[11px] font-bold uppercase tracking-wider text-slate-500">
                  Message
                </label>
                <textarea
                  id="notification-message"
                  value={message}
                  maxLength={MESSAGE_MAX_LENGTH}
                  rows={3}
                  onChange={(event) => setMessage(event.target.value)}
                  placeholder="What the desktop shows"
                  aria-invalid={errors.message ? true : undefined}
                  className={inputClass}
                />
                <div className="mt-1 text-right text-[11px] text-slate-400">
                  {message.length}/{MESSAGE_MAX_LENGTH}
                </div>
                <FieldError message={errors.message} />
              </div>
            </>
          )}

          {showsTime && (
            <div>
              <label htmlFor="notification-time" className="mb-1.5 block text-[11px] font-bold uppercase tracking-wider text-slate-500">
                Time (IST)
              </label>
              <input
                id="notification-time"
                type="time"
                value={time}
                onChange={(event) => setTime(event.target.value)}
                aria-invalid={errors.time ? true : undefined}
                className={inputClass + " sm:w-40"}
              />
              <FieldError message={errors.time} />
            </div>
          )}

          {builtin && builtin.kind === "interval" && (
            <p className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-[13px] text-slate-600">
              This reminder repeats every {builtin.every_minutes} minutes of a working session, so it has no fixed
              time. You can switch it off or limit it to certain days.
            </p>
          )}

          <div>
            <div className="mb-1.5 text-[11px] font-bold uppercase tracking-wider text-slate-500">Days</div>
            <WeekdayPicker value={weekdays} onChange={setWeekdays} error={errors.weekdays} />
          </div>

          {isCustom && (
            <label className="flex items-center gap-2.5 text-sm font-semibold text-slate-700">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
                className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500/40"
              />
              Send to desktops
            </label>
          )}
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-slate-100 px-6 py-4">
          <div>
            {builtin && (
              <button
                type="button"
                onClick={resetToDefault}
                className="text-[12px] font-bold uppercase tracking-wider text-slate-500 hover:text-slate-700 hover:underline"
              >
                Reset to default
              </button>
            )}
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm font-bold text-slate-600 transition hover:bg-slate-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="rounded-lg bg-gradient-to-r from-[#0ea5e9] via-[#3b82f6] to-[#8b5cf6] px-5 py-2 text-sm font-bold text-white shadow-md transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
};

const rowButton =
  "rounded-lg border px-3 py-1.5 text-[11px] font-bold transition disabled:cursor-not-allowed disabled:opacity-50";

export const AdminDesktopNotifications: React.FC = () => {
  const { showToast, confirmAction } = useFeedback();
  const notifications = useGetDesktopNotificationsQuery();
  const [updateBuiltin] = useUpdateBuiltinNotificationMutation();
  const [updateCustom] = useUpdateCustomNotificationMutation();
  const [deleteCustom] = useDeleteCustomNotificationMutation();
  const [editing, setEditing] = useState<Editing | null>(null);
  const [pending, setPending] = useState<Set<string>>(new Set());

  const data = notifications.data;
  const builtinOn = useMemo(() => (data?.builtin ?? []).filter((item) => item.enabled).length, [data]);
  const customOn = useMemo(() => (data?.custom ?? []).filter((item) => item.enabled).length, [data]);

  const hold = async (id: string, work: () => Promise<unknown>, failure: string) => {
    setPending((current) => new Set(current).add(id));
    try {
      await work();
    } catch (exc) {
      showToast(formatApiError((exc as { data?: unknown })?.data, failure), "error");
    } finally {
      setPending((current) => {
        const next = new Set(current);
        next.delete(id);
        return next;
      });
    }
  };

  const toggleBuiltin = (item: BuiltinNotification, enabled: boolean) =>
    hold(`builtin:${item.key}`, () => updateBuiltin({ key: item.key, body: { enabled } }).unwrap(), "Could not update the reminder.");

  const toggleCustom = (item: CustomNotification, enabled: boolean) =>
    hold(`custom:${item.id}`, () => updateCustom({ id: item.id, body: { enabled } }).unwrap(), "Could not update the notification.");

  const remove = async (item: CustomNotification) => {
    const confirmed = await confirmAction(
      `Delete "${item.title}"?`,
      "It will no longer be sent to any desktop. This cannot be undone.",
    );
    if (!confirmed) return;
    await hold(`custom:${item.id}`, () => deleteCustom(item.id).unwrap(), "Could not delete the notification.");
  };

  const sendBox = (label: string, checked: boolean, busy: boolean, onChange: (next: boolean) => void) => (
    <input
      type="checkbox"
      aria-label={`Send ${label}`}
      checked={checked}
      disabled={busy}
      onChange={(event) => onChange(event.target.checked)}
      className="h-4 w-4 cursor-pointer rounded border-slate-300 text-blue-600 focus:ring-blue-500/40 disabled:cursor-not-allowed"
    />
  );

  const header = (
    <thead className="bg-slate-50 text-slate-500">
      <tr>
        <th className="w-14 px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Send</th>
        <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Notification</th>
        <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">When</th>
        <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Days</th>
        <th className="px-4 py-3 text-right text-[11px] font-bold uppercase tracking-wider">Action</th>
      </tr>
    </thead>
  );

  const addButton = (
    <button
      type="button"
      onClick={() => setEditing({ kind: "new" })}
      className="rounded-lg bg-gradient-to-r from-[#0ea5e9] via-[#3b82f6] to-[#8b5cf6] px-4 py-2 text-sm font-bold text-white shadow-md transition hover:opacity-90"
    >
      + Add Notification
    </button>
  );

  return (
    <V2Shell title="Settings" subtitle="Desktop Notifications" actions={addButton}>
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-8">
        <div className="rounded-xl border border-[#E2E8F0] bg-white p-5 text-sm leading-6 text-slate-600 shadow-sm">
          <p className="font-semibold text-slate-800">Choose what the desktop app shows, and when.</p>
          <p>
            Tick <span className="font-semibold">Send</span> to have a notification shown on every desktop at its
            time, on its days. Untick it and it is never sent. All times are{" "}
            <span className="font-semibold">IST</span>. A desktop picks up a change within about half a minute, so
            save a notification at least a minute before its time to see it on the minute.
          </p>
          {data && data.version > 0 && data.updated_by_username && (
            <p className="mt-1 text-xs text-slate-500">
              Last changed by <span className="font-semibold text-slate-700">{data.updated_by_username}</span>
              {data.updated_at ? ` on ${formatWhen(data.updated_at)}` : ""}.
            </p>
          )}
        </div>

        {notifications.isLoading ? (
          <Card>
            <Spinner label="Loading notifications…" />
          </Card>
        ) : notifications.isError || !data ? (
          <ErrorNote message="Could not load the desktop notifications. Only administrators can open this page." />
        ) : (
          <>
            <section aria-labelledby="custom-notifications-heading">
              <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
                <div>
                  <h2 id="custom-notifications-heading" className="text-[13px] font-bold uppercase tracking-wider text-[#64748B]">
                    {`Custom notifications (${customOn} of ${data.custom.length} on)`}
                  </h2>
                  <p className="mt-0.5 text-xs text-slate-500">Your own messages, shown on every desktop at a time you choose.</p>
                </div>
                <InlineRefreshIndicator active={notifications.isFetching && !notifications.isLoading} label="Checking" />
              </div>

              {data.custom.length === 0 ? (
                <div className="flex flex-col items-center gap-3 rounded-2xl border-2 border-dashed border-slate-200 bg-white px-6 py-10 text-center">
                  <div className="flex h-12 w-12 items-center justify-center rounded-full bg-[#EFF6FF] text-[#2563EB]">
                    <svg className="h-6 w-6" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M15 17h5l-1.4-1.4A2 2 0 0118 14.2V11a6 6 0 10-12 0v3.2a2 2 0 01-.6 1.4L4 17h5m6 0a3 3 0 11-6 0m6 0H9" />
                    </svg>
                  </div>
                  <div>
                    <p className="text-sm font-bold text-slate-700">No custom notifications yet.</p>
                    <p className="mt-1 max-w-sm text-xs text-slate-500">
                      Add one to show your own message on every desktop at a set time, on the days you choose.
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => setEditing({ kind: "new" })}
                    className="rounded-lg border border-[#2563EB]/25 bg-[#EFF6FF] px-4 py-2 text-[12px] font-bold text-[#2563EB] transition hover:bg-[#DBEAFE]"
                  >
                    + Add your first notification
                  </button>
                </div>
              ) : (
                <div className="grid gap-4 md:grid-cols-2" data-testid="custom-list">
                  {data.custom.map((item) => {
                    const busy = pending.has(`custom:${item.id}`);
                    return (
                      <article
                        key={item.id}
                        data-testid="custom-card"
                        className={
                          "relative flex flex-col gap-3 overflow-hidden rounded-2xl border p-5 pl-6 shadow-sm transition " +
                          (item.enabled ? "border-[#E2E8F0] bg-white hover:shadow-md" : "border-slate-200 bg-slate-50/70")
                        }
                      >
                        <span
                          aria-hidden="true"
                          className={
                            "absolute inset-y-0 left-0 w-1.5 " +
                            (item.enabled ? "bg-gradient-to-b from-[#0ea5e9] to-[#8b5cf6]" : "bg-slate-300")
                          }
                        />
                        <div className="flex items-start justify-between gap-3">
                          <h3 className={"min-w-0 break-words text-base font-bold " + (item.enabled ? "text-slate-800" : "text-slate-400")}>
                            {item.title}
                          </h3>
                          <label className="flex shrink-0 cursor-pointer items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-slate-500">
                            {sendBox(item.title, item.enabled, busy, (next) => toggleCustom(item, next))}
                            Send
                          </label>
                        </div>
                        <p className={"break-words text-sm leading-6 " + (item.enabled ? "text-slate-600" : "text-slate-400")}>
                          {item.message}
                        </p>
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="inline-flex items-center gap-1.5 rounded-full bg-[#EFF6FF] px-3 py-1 text-xs font-bold text-[#2563EB]">
                            <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4l3 2m6-2a9 9 0 11-18 0 9 9 0 0118 0z" />
                            </svg>
                            {item.time} IST
                          </span>
                          <span className="inline-flex items-center rounded-full bg-slate-100 px-3 py-1 text-xs font-bold text-slate-600">
                            {describeWeekdays(item.weekdays)}
                          </span>
                        </div>
                        <div className="mt-auto flex justify-end gap-2 border-t border-slate-100 pt-3">
                          <button
                            type="button"
                            onClick={() => setEditing({ kind: "custom", item })}
                            className={rowButton + " border-[#14B8A6]/30 text-[#14B8A6] hover:bg-[#14B8A6]/10"}
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => remove(item)}
                            className={rowButton + " border-rose-200 text-rose-500 hover:bg-rose-50"}
                          >
                            Delete
                          </button>
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}
            </section>

            <section aria-labelledby="builtin-notifications-heading">
              <div className="mb-3">
                <h2 id="builtin-notifications-heading" className="text-[13px] font-bold uppercase tracking-wider text-[#64748B]">
                  {`Built-in reminders (${builtinOn} of ${data.builtin.length} on)`}
                </h2>
                <p className="mt-0.5 text-xs text-slate-500">The desktop's own wellbeing reminders. Switch one off, move it, or limit it to certain days.</p>
              </div>
              <div className="overflow-hidden rounded-xl border border-[#E2E8F0] bg-white shadow-sm">
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm" data-testid="builtin-table">
                    {header}
                    <tbody className="divide-y divide-slate-100">
                      {data.builtin.map((item) => (
                        <tr key={item.key} className={item.enabled ? "" : "bg-slate-50/60"}>
                          <td className="px-4 py-3.5">
                            {sendBox(item.label, item.enabled, pending.has(`builtin:${item.key}`), (next) => toggleBuiltin(item, next))}
                          </td>
                          <td className="px-4 py-3.5">
                            <div className={"font-bold " + (item.enabled ? "text-slate-800" : "text-slate-400")}>{item.label}</div>
                            <div className="max-w-md text-xs text-slate-500">{item.description}</div>
                          </td>
                          <td className="whitespace-nowrap px-4 py-3.5 font-semibold text-slate-700">{cadenceOf(item)}</td>
                          <td className="whitespace-nowrap px-4 py-3.5 text-slate-600">{describeWeekdays(item.weekdays)}</td>
                          <td className="px-4 py-3.5 text-right">
                            <button
                              type="button"
                              onClick={() => setEditing({ kind: "builtin", item })}
                              className={rowButton + " border-[#14B8A6]/30 text-[#14B8A6] hover:bg-[#14B8A6]/10"}
                            >
                              Edit
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </section>
          </>
        )}
      </div>

      {editing && <NotificationDialog editing={editing} onClose={() => setEditing(null)} />}
    </V2Shell>
  );
};
