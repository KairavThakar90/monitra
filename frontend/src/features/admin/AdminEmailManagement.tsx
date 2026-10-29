import React, { useEffect, useMemo, useRef, useState } from "react";
import { V2Shell } from "../dashboard/v2/V2Shell";
import { ErrorNote, Spinner } from "../member/MemberUi";
import { useFeedback } from "../../components/FeedbackProvider";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { formatApiError } from "../../api/utils";
import { EMAIL_MAX_LENGTH, FieldError, validateEmail } from "../../validation";
import {
  useGetEmailCcQuery,
  useUpdateEmailCcMutation,
  type EmailCcAddress,
  type EmailTrigger,
} from "../../store/api/emailCcApi";

/**
 * Settings -> Email Management.
 *
 * Every email Monitra sends, and who is copied on each. The table is a grid: one row
 * per email, one column per CC address, and a tick where that address is copied on
 * that email. An address is copied only where it is ticked; unticked, that email goes
 * out without it. Addresses can be added and removed here, and nothing is sent or
 * changed until Save.
 *
 * The list of emails comes from the server (name, who it goes to, what triggers it, and
 * whether it may be copied), so this page cannot drift from what is really sent. The
 * two emails that carry a private link -- a client's invitation and sign-in link -- are
 * shown but locked: copying anyone on them would hand that person the client's access,
 * and the server refuses it too.
 *
 * A change applies to every email that has not been sent yet. Until an administrator
 * first saves, the addresses are the deployment's configured starting list, ticked on
 * every email that may be copied -- exactly what was sent before this page existed.
 */

const inputClass =
  "w-full rounded-lg border bg-white px-3 py-2 text-sm text-slate-800 outline-none transition focus:border-[#38bdf8] focus:ring-2 focus:ring-[#38bdf8]/15";

const errorData = (error: unknown) => (error as { data?: unknown } | undefined)?.data;

/** One address's ticks, as a set of email keys. */
type Draft = { email: string; on: Set<string> };

const toDraft = (addresses: EmailCcAddress[]): Draft[] =>
  addresses.map((a) => ({ email: a.email, on: new Set(a.triggers) }));

/** A draft in the server's shape, with each address's emails in the server's order. */
const toPayload = (draft: Draft[], order: string[]): EmailCcAddress[] =>
  draft.map((d) => ({ email: d.email, triggers: order.filter((key) => d.on.has(key)) }));

const LockIcon: React.FC = () => (
  <svg className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4"
    />
  </svg>
);

const formatWhen = (iso: string | null) => {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
};

const Checkbox: React.FC<{
  checked: boolean;
  indeterminate?: boolean;
  label: string;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}> = ({ checked, indeterminate = false, label, disabled = false, onChange }) => {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate && !checked;
  }, [indeterminate, checked]);
  return (
    <input
      ref={ref}
      type="checkbox"
      aria-label={label}
      checked={checked}
      disabled={disabled}
      onChange={(event) => onChange(event.target.checked)}
      className="h-4 w-4 cursor-pointer rounded border-slate-300 text-blue-600 focus:ring-2 focus:ring-blue-500/40 disabled:cursor-not-allowed disabled:opacity-50"
    />
  );
};

const Stat: React.FC<{ label: string; value: string; hint?: string }> = ({ label, value, hint }) => (
  <div className="rounded-xl border border-[#E2E8F0] bg-white px-5 py-4 shadow-sm">
    <div className="text-[11px] font-bold uppercase tracking-wider text-slate-500">{label}</div>
    <div className="mt-1 text-2xl font-extrabold tabular-nums text-[#0F172A]">{value}</div>
    {hint && <div className="mt-0.5 text-xs text-slate-500">{hint}</div>}
  </div>
);

export const AdminEmailManagement: React.FC = () => {
  const { showToast } = useFeedback();
  const query = useGetEmailCcQuery();
  const [save, { isLoading: saving }] = useUpdateEmailCcMutation();
  const data = query.data;

  const copyable = useMemo(() => (data?.triggers ?? []).filter((t) => t.cc_allowed), [data?.triggers]);
  const order = useMemo(() => copyable.map((t) => t.key), [copyable]);

  const [draft, setDraft] = useState<Draft[]>([]);
  const [newAddress, setNewAddress] = useState("");
  const [addError, setAddError] = useState<string | null>(null);

  // What the server holds is the truth: after a save, or another administrator's change,
  // the grid shows that. Keyed on the saved value, so a background refetch that returns
  // the same thing never overwrites unsaved ticks.
  const serverKey = data ? JSON.stringify([data.version, data.addresses]) : "";
  const syncedKey = useRef<string | null>(null);
  useEffect(() => {
    if (!data || syncedKey.current === serverKey) return;
    syncedKey.current = serverKey;
    setDraft(toDraft(data.addresses));
  }, [data, serverKey]);

  const savedPayload = useMemo(
    () => (data ? JSON.stringify(toPayload(toDraft(data.addresses), order)) : ""),
    [data, order],
  );
  const draftPayload = JSON.stringify(toPayload(draft, order));
  const dirty = !!data && draftPayload !== savedPayload;

  const setCell = (email: string, key: string, next: boolean) =>
    setDraft((current) =>
      current.map((d) => {
        if (d.email !== email) return d;
        const on = new Set(d.on);
        if (next) on.add(key);
        else on.delete(key);
        return { ...d, on };
      }),
    );
  const setColumn = (email: string, next: boolean) =>
    setDraft((current) => current.map((d) => (d.email === email ? { ...d, on: new Set(next ? order : []) } : d)));
  const setRow = (key: string, next: boolean) =>
    setDraft((current) =>
      current.map((d) => {
        const on = new Set(d.on);
        if (next) on.add(key);
        else on.delete(key);
        return { ...d, on };
      }),
    );
  const remove = (email: string) => setDraft((current) => current.filter((d) => d.email !== email));

  const add = () => {
    if (!data) return;
    const check = validateEmail(newAddress, { fieldLabel: "Email address" });
    if (!check.ok) {
      setAddError(check.error);
      return;
    }
    // The server stores the whole address lower-cased and lists it once.
    const email = check.value.toLowerCase();
    if (draft.some((d) => d.email === email)) {
      setAddError("That address is already in the list.");
      return;
    }
    if (draft.length >= data.max_addresses) {
      setAddError(`At most ${data.max_addresses} addresses can be copied.`);
      return;
    }
    setDraft((current) => [...current, { email, on: new Set() }]);
    setNewAddress("");
    setAddError(null);
  };

  const submit = async () => {
    if (!dirty || saving) return;
    try {
      // The version this grid was built from goes with the save: the server refuses
      // (409) a save over a newer policy rather than silently discarding what
      // another administrator saved in between.
      await save({ addresses: toPayload(draft, order), version: data.version }).unwrap();
      showToast("Email management saved.", "success");
    } catch (exc) {
      showToast(formatApiError(errorData(exc), "Could not save email management."), "error");
      if ((exc as { status?: unknown } | undefined)?.status === 409) {
        // Someone else saved first: show what is now in force. The grid re-syncs
        // from the server's version, so the unsaved ticks are dropped deliberately.
        void query.refetch();
      }
    }
  };

  const discard = () => {
    if (data) setDraft(toDraft(data.addresses));
    setAddError(null);
    setNewAddress("");
  };

  const copiedOn = (d: Draft) => order.filter((key) => d.on.has(key)).length;
  const rowCount = (key: string) => draft.filter((d) => d.on.has(key)).length;
  const emailsCopied = order.filter((key) => rowCount(key) > 0).length;

  const columns = draft.length;
  const triggers = data?.triggers ?? [];

  return (
    <V2Shell title="Settings" subtitle="Email Management">
      <div className="flex w-full flex-col gap-6 pb-24">
        {query.isLoading ? (
          <Spinner label="Loading emails…" />
        ) : query.isError || !data ? (
          <ErrorNote message="Could not load email management." onRetry={() => void query.refetch()} />
        ) : (
          <>
            <div className="rounded-xl border border-[#E2E8F0] bg-white p-5 text-sm leading-6 text-slate-600 shadow-sm">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="font-semibold text-slate-800">Choose who is copied on each email.</p>
                  <p>
                    Tick an address under an email to copy it on that email; leave it unticked and that email is sent
                    without it. A change applies to every email that has not been sent yet. The people an email is
                    addressed to are not affected.
                  </p>
                  {data.is_default && (
                    <p className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-[13px] text-amber-800">
                      Nothing has been saved here yet. These are the addresses Monitra is configured to copy, ticked on
                      every email that can be copied &mdash; the same as before this page existed.
                    </p>
                  )}
                </div>
                <InlineRefreshIndicator active={query.isFetching && !query.isLoading} label="Checking" />
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-3">
              <Stat label="Emails" value={String(triggers.length)} hint={`${copyable.length} can be copied`} />
              <Stat label="CC addresses" value={String(draft.length)} hint={`up to ${data.max_addresses}`} />
              <Stat
                label="Emails with a copy"
                value={`${emailsCopied} of ${copyable.length}`}
                hint={emailsCopied === 0 ? "Nobody is copied" : "at least one address copied"}
              />
            </div>

            <div className="rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
              <h2 className="mb-3 text-[13px] font-bold uppercase tracking-wider text-[#64748B]">CC addresses</h2>
              <form
                className="flex flex-wrap items-start gap-3"
                onSubmit={(event) => {
                  event.preventDefault();
                  add();
                }}
                noValidate
              >
                <div className="min-w-[260px] flex-1 sm:max-w-md">
                  <input maxLength={EMAIL_MAX_LENGTH}
                    type="email"
                    aria-label="New CC address"
                    placeholder="name@company.com"
                    autoComplete="off"
                    value={newAddress}
                    onChange={(event) => {
                      setNewAddress(event.target.value);
                      setAddError(null);
                    }}
                    aria-invalid={addError ? true : undefined}
                    aria-describedby={addError ? "cc-add-error" : undefined}
                    className={inputClass + (addError ? " border-rose-300" : " border-slate-200")}
                  />
                  <FieldError id="cc-add-error" message={addError} />
                </div>
                <button
                  type="submit"
                  className="cursor-pointer rounded-lg bg-gradient-to-r from-[#0ea5e9] via-[#3b82f6] to-[#8b5cf6] px-4 py-2 text-sm font-bold text-white shadow-sm transition hover:opacity-90"
                >
                  Add address
                </button>
              </form>
              <p className="mt-2 text-xs text-slate-500">
                A new address starts with no email ticked. Tick the emails it should be copied on in the grid below.
              </p>
            </div>

            <div className="rounded-xl border border-[#E2E8F0] bg-white shadow-sm">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[900px] border-collapse text-left text-sm">
                  <thead className="border-b border-slate-200 bg-slate-50 text-slate-500">
                    <tr>
                      <th className="w-12 px-4 py-3 text-[11px] font-bold uppercase tracking-wider">#</th>
                      <th className="min-w-[260px] px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Email</th>
                      <th className="min-w-[200px] px-4 py-3 text-[11px] font-bold uppercase tracking-wider">
                        Goes to
                      </th>
                      <th className="min-w-[220px] px-4 py-3 text-[11px] font-bold uppercase tracking-wider">
                        When it is sent
                      </th>
                      <th className="w-20 px-3 py-3 text-center text-[11px] font-bold uppercase tracking-wider">
                        All
                      </th>
                      {draft.map((d) => (
                        <th key={d.email} className="min-w-[150px] border-l border-slate-200 px-3 py-3 align-top">
                          <div className="flex items-start justify-between gap-2">
                            <span
                              className="break-all text-[12px] font-bold normal-case tracking-normal text-slate-700"
                              title={d.email}
                            >
                              {d.email}
                            </span>
                            <button
                              type="button"
                              aria-label={`Remove ${d.email}`}
                              onClick={() => remove(d.email)}
                              className="shrink-0 cursor-pointer rounded p-0.5 text-slate-400 transition hover:bg-rose-50 hover:text-rose-600"
                            >
                              <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12" />
                              </svg>
                            </button>
                          </div>
                          <label className="mt-2 flex cursor-pointer items-center gap-2 text-[11px] font-semibold normal-case tracking-normal text-slate-500">
                            <Checkbox
                              checked={copiedOn(d) === order.length && order.length > 0}
                              indeterminate={copiedOn(d) > 0}
                              label={`Copy ${d.email} on every email`}
                              onChange={(next) => setColumn(d.email, next)}
                            />
                            {copiedOn(d)} of {order.length}
                          </label>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {triggers.map((t: EmailTrigger) => (
                      <tr key={t.key} className={t.cc_allowed ? "hover:bg-slate-50/60" : "bg-slate-50/70"}>
                        <td className="px-4 py-3 align-top text-xs font-bold tabular-nums text-slate-400">{t.number}</td>
                        <td className="px-4 py-3 align-top">
                          <div className="font-bold text-slate-800">{t.name}</div>
                          {!t.active && (
                            <span className="mt-1 inline-flex rounded-md border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-bold text-amber-700">
                              Switched off in configuration
                            </span>
                          )}
                          {t.note && t.cc_allowed && <div className="mt-0.5 text-xs text-slate-500">{t.note}</div>}
                        </td>
                        <td className="px-4 py-3 align-top text-slate-600">{t.goes_to}</td>
                        <td className="px-4 py-3 align-top text-slate-600">{t.trigger}</td>
                        {t.cc_allowed ? (
                          <>
                            <td className="px-3 py-3 text-center align-top">
                              <Checkbox
                                checked={columns > 0 && rowCount(t.key) === columns}
                                indeterminate={rowCount(t.key) > 0}
                                disabled={columns === 0}
                                label={`Copy every address on ${t.name}`}
                                onChange={(next) => setRow(t.key, next)}
                              />
                            </td>
                            {draft.map((d) => (
                              <td key={d.email} className="border-l border-slate-100 px-3 py-3 text-center align-top">
                                <Checkbox
                                  checked={d.on.has(t.key)}
                                  label={`Copy ${d.email} on ${t.name}`}
                                  onChange={(next) => setCell(d.email, t.key, next)}
                                />
                              </td>
                            ))}
                          </>
                        ) : (
                          <td
                            colSpan={1 + columns}
                            className="border-l border-slate-100 px-4 py-3 align-top text-xs font-semibold text-slate-500"
                          >
                            <span className="flex items-center gap-2">
                              <LockIcon />
                              No CC. {t.note}
                            </span>
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {columns === 0 && (
                <p className="border-t border-slate-100 px-5 py-6 text-center text-sm font-semibold text-slate-500">
                  No address is copied on any email. Add an address above to copy someone.
                </p>
              )}
            </div>

            {data.version > 0 && data.updated_by_username && (
              <p className="text-xs text-slate-500">
                Last changed by <span className="font-semibold text-slate-700">{data.updated_by_username}</span>
                {data.updated_at ? ` on ${formatWhen(data.updated_at)}` : ""}.
              </p>
            )}

            <div
              className={
                "sticky bottom-4 z-10 flex flex-wrap items-center justify-between gap-3 rounded-xl border px-5 py-3 shadow-lg transition " +
                (dirty ? "border-blue-200 bg-white" : "border-[#E2E8F0] bg-white/90")
              }
              role="region"
              aria-label="Save changes"
            >
              <p className="text-sm text-slate-600">
                {dirty ? (
                  <span className="font-semibold text-blue-700">You have unsaved changes.</span>
                ) : (
                  "Everything shown is saved."
                )}
              </p>
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  disabled={!dirty || saving}
                  onClick={discard}
                  className="cursor-pointer rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm font-bold text-slate-600 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Discard
                </button>
                <button
                  type="button"
                  disabled={!dirty || saving}
                  onClick={() => void submit()}
                  className="cursor-pointer rounded-lg bg-gradient-to-r from-[#0ea5e9] via-[#3b82f6] to-[#8b5cf6] px-5 py-2 text-sm font-bold text-white shadow-md transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {saving ? "Saving…" : "Save changes"}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </V2Shell>
  );
};
