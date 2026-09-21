import React, { useState } from "react";
import { V2Shell } from "../dashboard/v2/V2Shell";
import { Card, EmptyState, ErrorNote, Spinner } from "../member/MemberUi";
import { useFeedback } from "../../components/FeedbackProvider";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { formatApiError } from "../../api/utils";
import {
  MAINTENANCE_ACTION_LABELS,
  useGetMaintenanceHistoryQuery,
  useGetMaintenanceModeQuery,
  useSetMaintenanceModeMutation,
} from "../../store/api/systemApi";

/**
 * Admin Settings → System.
 *
 * One control today: the maintenance notice. It is a *notice*: switching it
 * on shows every signed-in web session and every desktop client a card saying
 * Monitra is under maintenance and that their activity is safe. It stops
 * nothing -- timers keep running, tracking and screenshots continue, every
 * request is answered as before -- and switching it off simply removes the
 * card. The wording on this page says so, because an administrator reaching
 * for this button is entitled to know exactly what it does and does not do.
 *
 * Every change is audited server-side (who, when, resulting state); the recent
 * ones are listed here. The route and the sidebar entry are gated on the
 * administrator role; `PUT /system/maintenance-mode` refuses anyone else
 * regardless of what this page renders.
 */

const formatWhen = (iso: string | null | undefined) => {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
};

export const AdminSettings: React.FC = () => {
  const { showToast, confirmAction } = useFeedback();
  const mode = useGetMaintenanceModeQuery();
  const history = useGetMaintenanceHistoryQuery({ limit: 10 });
  const [setMaintenanceMode, { isLoading: isSaving }] = useSetMaintenanceModeMutation();
  const [error, setError] = useState<string | null>(null);

  const active = mode.data?.maintenance_mode === true;

  const toggle = async () => {
    const next = !active;
    const confirmed = await confirmAction(
      next ? "Enable maintenance notification?" : "Disable maintenance notification?",
      next
        ? "Every signed-in user, on the web and on the desktop, will see a notice that Monitra is under maintenance. Nothing is paused or blocked: timers, tracking and screenshots continue exactly as before."
        : "The maintenance notice will be removed from every client. Nothing was stopped, so nothing needs to be resumed.",
    );
    if (!confirmed) return;
    setError(null);
    try {
      await setMaintenanceMode({ enabled: next }).unwrap();
      showToast(
        next ? "Maintenance notification enabled." : "Maintenance notification disabled.",
        "success",
      );
    } catch (exc) {
      const message = formatApiError((exc as { data?: unknown })?.data, "Could not update maintenance mode.");
      setError(message);
      showToast(message, "error");
    }
  };

  return (
    <V2Shell title="Settings" subtitle="Maintenance">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
        <Card
          title="Maintenance Mode"
          action={<InlineRefreshIndicator active={mode.isFetching && !mode.isLoading} label="Checking" />}
        >
          {mode.isLoading ? (
            <Spinner label="Loading maintenance state…" />
          ) : mode.isError ? (
            <ErrorNote message="Could not load the maintenance state. Only administrators can open this page." />
          ) : (
            <div className="flex flex-col gap-5">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div className="flex items-center gap-3">
                  <span
                    aria-hidden="true"
                    className={
                      "h-3 w-3 rounded-full " + (active ? "bg-rose-500" : "bg-emerald-500")
                    }
                  />
                  <div>
                    <div className="text-base font-bold text-slate-800" data-testid="maintenance-state">
                      {active ? "Maintenance notification is active" : "System is operational"}
                    </div>
                    <div className="mt-0.5 text-sm text-slate-500">
                      {active
                        ? "Every signed-in user is seeing the maintenance notice."
                        : "No notice is shown. Users see Monitra as normal."}
                    </div>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={toggle}
                  disabled={isSaving}
                  data-testid="maintenance-toggle"
                  className={
                    "rounded-lg px-4 py-2 text-sm font-bold text-white shadow-sm transition disabled:cursor-not-allowed disabled:opacity-50 " +
                    (active ? "bg-slate-700 hover:bg-slate-800" : "bg-rose-500 hover:bg-rose-600")
                  }
                >
                  {isSaving
                    ? "Saving…"
                    : active
                      ? "Disable Maintenance Mode"
                      : "Enable Maintenance Mode"}
                </button>
              </div>

              {error && <ErrorNote message={error} />}

              <div className="rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm leading-6 text-slate-600">
                <p className="font-semibold text-slate-700">What this does</p>
                <p>
                  Shows a non-blocking notice in the web app and the desktop client:
                  “Monitra is under maintenance — your activity is being saved safely offline
                  and will sync automatically.”
                </p>
                <p className="mt-2 font-semibold text-slate-700">What this does not do</p>
                <p>
                  It does not stop or pause timers, activity tracking, app and URL tracking,
                  screenshots or synchronisation, and it does not sign anyone out. Disabling it
                  simply removes the notice.
                </p>
              </div>

              {mode.data?.updated_by_username && (
                <div className="text-xs text-slate-500">
                  Last changed by <span className="font-semibold text-slate-700">{mode.data.updated_by_username}</span>
                  {mode.data.updated_at ? ` on ${formatWhen(mode.data.updated_at)}` : ""}.
                </div>
              )}
            </div>
          )}
        </Card>

        <Card title="Recent changes">
          {history.isLoading ? (
            <Spinner />
          ) : history.isError ? (
            <ErrorNote message="Could not load the change history." />
          ) : !history.data || history.data.items.length === 0 ? (
            <EmptyState message="No maintenance changes recorded yet." />
          ) : (
            <ul className="divide-y divide-slate-100">
              {history.data.items.map((entry) => (
                <li key={entry.id} className="flex flex-wrap items-baseline justify-between gap-2 py-2.5 text-sm">
                  <span
                    className={
                      "font-semibold " +
                      (entry.action === "maintenance_enabled" ? "text-rose-600" : "text-emerald-600")
                    }
                  >
                    {MAINTENANCE_ACTION_LABELS[entry.action] ?? entry.action}
                  </span>
                  <span className="text-slate-500">{entry.description}</span>
                  <span className="text-xs text-slate-400">{formatWhen(entry.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </V2Shell>
  );
};
