import React, { useMemo, useState } from "react";
import { V2Shell } from "../dashboard/v2/V2Shell";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { useFeedback } from "../../components/FeedbackProvider";
import { formatApiError } from "../../api/utils";
import { useGetMembersQuery, useLazyGetMembersQuery, useUpdateMemberMutation, type Member } from "../../store/api/membersApi";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { Pagination } from "../../components/Pagination";
import { FieldError, SEARCH_MAX_LENGTH, validateSearchTerm } from "../../validation";

/**
 * Admin Settings → User Management.
 *
 * Three per-member desktop switches -- screenshot capture, the administrator's
 * desktop notifications, and activity tracking (keyboard/mouse activity plus
 * application and website usage) -- stored as `screenshots_enabled` /
 * `notifications_enabled` / `activity_tracking_enabled` and saved through the
 * member update (`PATCH /members/{id}`). Absent means on; only an explicit
 * `false` is off. Each switch is sent on its own, so changing one never
 * rewrites another.
 *
 * Each switch has its own column, drawn like the Members page's Add Task /
 * Login switches. The switch in a column heading turns that one on or off for
 * every active member the search matches, not only the rows on this page.
 *
 * The screenshot frequency and idle detection are not set here: both live in
 * Screenshot Management, so there is one place to change each.
 *
 * Ticking rows opens a toolbar, like the Members page's, that turns one switch
 * on or off for the ticked members with one PATCH per member. The Configure
 * drawer edits one member's three switches together.
 */

const GRADIENT_CYAN_PURPLE = "bg-gradient-to-r from-[#0ea5e9] via-[#3b82f6] to-[#8b5cf6]";

type FormState = {
  screenshots: boolean;
  notifications: boolean;
  activity: boolean;
};

const toFormState = (member: Member): FormState => ({
  screenshots: member.screenshots_enabled !== false,
  notifications: member.notifications_enabled !== false,
  activity: member.activity_tracking_enabled !== false,
});

/** The three desktop switches: which form field, which member field, and what the administrator reads. */
const DESKTOP_SWITCHES = [
  {
    form: "screenshots",
    field: "screenshots_enabled",
    label: "Screenshot capture",
    column: "Screenshots",
    help: "Capture periodic screenshots while this member is tracking time. Screenshots already taken are kept.",
  },
  {
    form: "notifications",
    field: "notifications_enabled",
    label: "Desktop notifications",
    column: "Notifications",
    help: "Show this member reminders, messages from the administrator and routine status notices. Alerts that need attention are always shown.",
  },
  {
    form: "activity",
    field: "activity_tracking_enabled",
    label: "Activity tracking",
    column: "Activity",
    help: "Measure keyboard and mouse activity, and the applications and websites used, while tracking time. Idle detection is managed in Screenshot Management.",
  },
] as const;

/** Checkbox, Employee, the three switches, Action. */
const COLUMN_COUNT = 6;

type SwitchField = (typeof DESKTOP_SWITCHES)[number]["field"];

const LoadingSpinner: React.FC = () => (
  <div className="flex min-h-28 items-center justify-center" role="status" aria-label="Loading">
    <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-500 border-t-transparent" />
  </div>
);

/**
 * One On/Off switch, drawn like the Members page's Add Task / Login switch: a
 * status pill beside a sliding switch.
 */
const OnOffSwitch: React.FC<{
  on: boolean;
  busy?: boolean;
  /** Accessible name; it also names what the switch does. */
  label: string;
  title?: string;
  onChange: (on: boolean) => void;
}> = ({ on, busy = false, label, title, onChange }) => (
  <div className="flex items-center gap-2">
    {on ? (
      <span className="inline-flex items-center rounded-md border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[11px] font-bold tracking-wider text-emerald-600">On</span>
    ) : (
      <span className="inline-flex items-center rounded-md border border-rose-200 bg-rose-50 px-2.5 py-1 text-[11px] font-bold tracking-wider text-rose-500">Off</span>
    )}
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      title={title ?? label}
      disabled={busy}
      onClick={() => onChange(!on)}
      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500/40 disabled:cursor-not-allowed disabled:opacity-60 ${
        on ? "bg-blue-600" : "bg-slate-300"
      }`}
    >
      <span
        className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
          on ? "translate-x-5" : "translate-x-0.5"
        }`}
      />
    </button>
  </div>
);

export const AdminUserManagement: React.FC = () => {
  const { showToast, confirmAction } = useFeedback();

  const [search, setSearch] = useState("");
  const [searchError, setSearchError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);

  const debouncedSearch = useDebouncedValue(search);
  const searchCheck = validateSearchTerm(debouncedSearch, { fieldLabel: "Search" });
  const searchTerm = searchCheck.ok ? searchCheck.value : "";

  const { data, isLoading, isFetching, isError } = useGetMembersQuery({
    page,
    limit: pageSize,
    status: "active",
    search: searchTerm,
  });

  const [updateMember, { isLoading: isSaving }] = useUpdateMemberMutation();
  const [fetchMembersPage] = useLazyGetMembersQuery();

  const items = data?.items ?? [];
  const totalPages = data?.pages || 1;
  const showFirstLoad = isLoading && !data;

  // Rows the admin has ticked, so one switch can be changed for several people
  // from the toolbar instead of one row at a time.
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const selectedOnPage = items.filter((member) => selectedIds.has(member.id));
  const allOnPageSelected = items.length > 0 && selectedOnPage.length === items.length;

  const toggleSelected = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectAllOnPage = () => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (allOnPageSelected) items.forEach((member) => next.delete(member.id));
      else items.forEach((member) => next.add(member.id));
      return next;
    });
  };

  const [drawerMember, setDrawerMember] = useState<Member | null>(null);
  const [form, setForm] = useState<FormState | null>(null);
  const [error, setError] = useState<string | null>(null);

  const openDrawer = (member: Member) => {
    setDrawerMember(member);
    setForm(toFormState(member));
    setError(null);
  };

  const closeDrawer = () => {
    setDrawerMember(null);
    setForm(null);
    setError(null);
  };

  // Only a switch that was changed is sent.
  const changedSwitches = useMemo(() => {
    if (!form || !drawerMember) return [];
    return DESKTOP_SWITCHES.filter(({ form: key }) => form[key] !== toFormState(drawerMember)[key]);
  }, [drawerMember, form]);
  const isDirty = changedSwitches.length > 0;

  // A row's switch is saved at once, on its own, and put back if the server refuses.
  const [pendingSwitches, setPendingSwitches] = useState<Set<string>>(new Set());
  const [bulkSwitch, setBulkSwitch] = useState<SwitchField | null>(null);

  const handleRowSwitch = async (member: Member, field: SwitchField, label: string, on: boolean) => {
    const key = `${member.id}:${field}`;
    setPendingSwitches((prev) => new Set(prev).add(key));
    try {
      await updateMember({ id: member.id, body: { [field]: on } as Partial<Member> }).unwrap();
    } catch (err) {
      showToast(
        formatApiError((err as { data?: unknown })?.data, `Could not change ${label.toLowerCase()} for ${member.name}.`),
        "error",
      );
    } finally {
      setPendingSwitches((prev) => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
    }
  };

  // One switch for a group of members, one PATCH each, only for the members it
  // would change. Reports the outcome; resolves true when nothing failed.
  const applySwitch = async (members: Member[], field: SwitchField, label: string, on: boolean, scope: string) => {
    const word = on ? "on" : "off";
    const targets = members.filter((member) => (member[field] !== false) !== on);
    if (targets.length === 0) {
      showToast(`${label} is already ${word} for ${scope}.`, "info");
      return true;
    }
    const results = await Promise.allSettled(
      targets.map((member) => updateMember({ id: member.id, body: { [field]: on } as Partial<Member> }).unwrap()),
    );
    const failures = results.filter((result) => result.status === "rejected");
    if (failures.length === 0) {
      showToast(`${label} turned ${word} for ${targets.length} member${targets.length === 1 ? "" : "s"}.`, "success");
      return true;
    }
    const firstFailure = failures[0] as PromiseRejectedResult;
    showToast(
      formatApiError(
        (firstFailure.reason as { data?: unknown })?.data,
        `${label} changed for ${targets.length - failures.length} of ${targets.length} members; ${failures.length} failed.`,
      ),
      failures.length === targets.length ? "error" : "info",
    );
    return false;
  };

  // The toolbar over the ticked rows: On / Off for one switch, like the Members
  // page's Allow / Exclude. The ticks are cleared once everything was saved.
  const handleSelectedSwitch = async (field: SwitchField, label: string, on: boolean) => {
    setBulkSwitch(field);
    try {
      if (await applySwitch(selectedOnPage, field, label, on, "the selected members")) setSelectedIds(new Set());
    } finally {
      setBulkSwitch(null);
    }
  };

  // The column-heading switch: every active member the search matches, read page
  // by page (the API caps a page at 100) so members on other pages are included.
  const handleAllSwitch = async (field: SwitchField, label: string, on: boolean) => {
    const word = on ? "on" : "off";
    if (
      !(await confirmAction(
        `Turn ${label.toLowerCase()} ${word} for every member?`,
        searchTerm
          ? `This changes every active member matching "${searchTerm}", on every page.`
          : "This changes every active member, on every page.",
      ))
    ) {
      return;
    }
    setBulkSwitch(field);
    try {
      const everyone: Member[] = [];
      let pageNumber = 1;
      let pages = 1;
      do {
        // eslint-disable-next-line no-await-in-loop
        const result = await fetchMembersPage({ page: pageNumber, limit: 100, status: "active", search: searchTerm }).unwrap();
        everyone.push(...result.items);
        pages = result.pages || 1;
        pageNumber += 1;
      } while (pageNumber <= pages);

      await applySwitch(everyone, field, label, on, "every member");
    } catch (err) {
      showToast(
        formatApiError((err as { data?: unknown })?.data, `Could not turn ${label.toLowerCase()} ${word} for every member.`),
        "error",
      );
    } finally {
      setBulkSwitch(null);
    }
  };

  const handleSave = async () => {
    if (!drawerMember || !form || !isDirty) return;
    setError(null);
    const body = Object.fromEntries(changedSwitches.map(({ form: key, field }) => [field, form[key]]));
    try {
      await updateMember({ id: drawerMember.id, body }).unwrap();
    } catch (err) {
      const message = formatApiError((err as { data?: unknown })?.data, "Could not save tracking settings.");
      setError(message);
      showToast(message, "error");
      return;
    }
    showToast(`Tracking settings saved for ${drawerMember.name}.`, "success");
    closeDrawer();
  };

  return (
    <V2Shell
      title="Settings"
      subtitle="User Management"
      actions={<InlineRefreshIndicator active={isFetching && !showFirstLoad} label="Refreshing" />}
    >
      {/* No side padding of its own on a phone: the shell's <main> already gives 16px. */}
      <div className="w-full px-0 sm:px-6 lg:px-8 pt-2 sm:pt-6 space-y-6 pb-20">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex min-w-0 flex-1 items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 lg:border-0 lg:bg-transparent lg:p-0 lg:px-2">
            <svg className="h-5 w-5 text-slate-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input
              type="text"
              placeholder="Search members by name or email..."
              value={search}
              maxLength={SEARCH_MAX_LENGTH}
              aria-invalid={searchError ? true : undefined}
              aria-describedby={searchError ? "settings-search-error" : undefined}
              onChange={(event) => {
                const next = event.target.value;
                setSearch(next);
                const result = validateSearchTerm(next, { fieldLabel: "Search" });
                setSearchError(result.ok ? null : result.error);
                setPage(1);
              }}
              className="flex-1 bg-transparent text-sm outline-none placeholder:text-slate-400 text-slate-700"
            />
          </div>
          {searchError && (
            <div className="px-2">
              <FieldError id="settings-search-error" message={searchError} />
            </div>
          )}
        </div>

        {selectedOnPage.length > 0 && (
          <div
            role="toolbar"
            aria-label="Bulk desktop switches"
            className="sticky top-3 z-20 flex flex-wrap items-center gap-x-6 gap-y-3 rounded-2xl border border-blue-200 bg-white/95 px-5 py-3 shadow-lg shadow-blue-900/5 backdrop-blur"
          >
            <div className="flex items-center gap-3">
              <span className="flex h-9 min-w-9 items-center justify-center rounded-full bg-blue-600 px-2.5 text-sm font-bold text-white">
                {selectedOnPage.length}
              </span>
              <div className="leading-tight">
                <div className="text-sm font-bold text-slate-800">{selectedOnPage.length} selected</div>
                <div className="text-[11px] font-medium text-slate-500">of {data?.total ?? items.length} members</div>
              </div>
            </div>

            <div className="hidden h-8 w-px bg-slate-200 md:block" />

            {DESKTOP_SWITCHES.map(({ field, label, column }) => {
              const canTurnOn = selectedOnPage.some((member) => member[field] === false);
              const canTurnOff = selectedOnPage.some((member) => member[field] !== false);
              return (
                <div key={field} className="flex items-center gap-2.5">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-slate-500">{column}:</span>
                  <div className="inline-flex overflow-hidden rounded-lg border border-slate-200 shadow-sm">
                    <button
                      type="button"
                      aria-label={`Turn ${label.toLowerCase()} on for the selected members`}
                      disabled={bulkSwitch !== null || !canTurnOn}
                      title={canTurnOn ? undefined : `${label} is already on for every selected member`}
                      onClick={() => handleSelectedSwitch(field, label, true)}
                      className="px-4 py-2 text-[11px] font-bold uppercase tracking-wider text-emerald-700 transition hover:bg-emerald-50 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-300 disabled:hover:bg-slate-50"
                    >
                      On
                    </button>
                    <button
                      type="button"
                      aria-label={`Turn ${label.toLowerCase()} off for the selected members`}
                      disabled={bulkSwitch !== null || !canTurnOff}
                      title={canTurnOff ? undefined : `${label} is already off for every selected member`}
                      onClick={() => handleSelectedSwitch(field, label, false)}
                      className="border-l border-slate-200 px-4 py-2 text-[11px] font-bold uppercase tracking-wider text-rose-600 transition hover:bg-rose-50 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-300 disabled:hover:bg-slate-50"
                    >
                      Off
                    </button>
                  </div>
                </div>
              );
            })}

            <button
              type="button"
              onClick={() => setSelectedIds(new Set())}
              className="ml-auto inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-[11px] font-bold uppercase tracking-wider text-slate-500 transition hover:bg-slate-100 hover:text-slate-700"
            >
              <span aria-hidden="true">&times;</span> Clear selection
            </button>
          </div>
        )}

        <div className="relative overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="overflow-x-auto pb-4">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-50 text-slate-500 border-b border-slate-200">
                <tr>
                  <th className="w-10 pl-4 py-4">
                    <input
                      type="checkbox"
                      aria-label="Select all members on this page"
                      title={allOnPageSelected ? "Clear the selection" : "Select all members on this page"}
                      ref={(el) => { if (el) el.indeterminate = selectedOnPage.length > 0 && !allOnPageSelected; }}
                      checked={allOnPageSelected}
                      disabled={items.length === 0}
                      onChange={toggleSelectAllOnPage}
                      className="h-4 w-4 cursor-pointer rounded border-slate-300 text-blue-600 focus:ring-blue-500/40"
                    />
                  </th>
                  <th className="px-4 py-4 font-bold uppercase tracking-wider text-[11px]">Employee</th>
                  {DESKTOP_SWITCHES.map(({ field, label, column }) => (
                    <th key={field} className="px-4 py-4 font-bold uppercase tracking-wider text-[11px]">
                      <div className="flex items-center gap-3">
                        <span>{column}</span>
                        <OnOffSwitch
                          on={items.length > 0 && items.every((member) => member[field] !== false)}
                          busy={bulkSwitch !== null || items.length === 0}
                          label={`${label} for all members`}
                          title={`Turn ${label.toLowerCase()} on or off for every member`}
                          onChange={(on) => handleAllSwitch(field, label, on)}
                        />
                      </div>
                    </th>
                  ))}
                  <th className="px-4 py-4 font-bold uppercase tracking-wider text-[11px] text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {showFirstLoad ? (
                  <tr>
                    <td colSpan={COLUMN_COUNT} className="px-6 py-8">
                      <LoadingSpinner />
                    </td>
                  </tr>
                ) : isError ? (
                  <tr>
                    <td colSpan={COLUMN_COUNT} className="px-6 py-12 text-center text-red-500">
                      Failed to fetch members. Please try again.
                    </td>
                  </tr>
                ) : items.length > 0 ? (
                  items.map((member) => (
                    <tr key={member.id} className={`transition hover:bg-slate-50/50 ${selectedIds.has(member.id) ? "bg-blue-50/40" : ""}`}>
                      <td className="w-10 pl-4 py-4">
                        <input
                          type="checkbox"
                          checked={selectedIds.has(member.id)}
                          onChange={() => toggleSelected(member.id)}
                          aria-label={`Select ${member.name}`}
                          className="h-4 w-4 cursor-pointer rounded border-slate-300 text-blue-600 focus:ring-blue-500/40"
                        />
                      </td>
                      <td className="px-4 py-4">
                        <div className="flex items-center gap-3">
                          <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-xs font-bold text-white shadow-sm ${GRADIENT_CYAN_PURPLE}`}>
                            {(member.name || "U").substring(0, 2).toUpperCase()}
                          </div>
                          <div className="min-w-0">
                            <div className="font-bold text-slate-800 truncate">{member.name || "-"}</div>
                            <div className="text-xs text-slate-500 truncate mt-0.5">{member.email || "-"}</div>
                          </div>
                        </div>
                      </td>
                      {DESKTOP_SWITCHES.map(({ field, label }) => {
                        const on = member[field] !== false;
                        return (
                          <td key={field} className="px-4 py-4">
                            <OnOffSwitch
                              on={on}
                              busy={pendingSwitches.has(`${member.id}:${field}`) || bulkSwitch === field}
                              label={`${label} for ${member.name}`}
                              title={`Click to turn ${label.toLowerCase()} ${on ? "off" : "on"} for ${member.name}`}
                              onChange={(next) => handleRowSwitch(member, field, label, next)}
                            />
                          </td>
                        );
                      })}
                      <td className="px-4 py-4 text-right">
                        <div className="flex items-center justify-end gap-2">
                          <button
                            onClick={() => openDrawer(member)}
                            className="rounded px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-[#14B8A6] border border-[#14B8A6]/30 transition hover:bg-[#14B8A6]/10"
                          >
                            Configure
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={COLUMN_COUNT} className="px-6 py-12 text-center text-slate-500">
                      No members found matching your criteria.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {totalPages > 1 && (
          <Pagination page={page} totalPages={totalPages} totalItems={data?.total || 0} limit={pageSize} setPage={setPage} setLimit={setPageSize} noun="members" />
        )}
      </div>

      {/* Right slide-over: one member's desktop switches */}
      <div className={`fixed inset-0 z-50 overflow-hidden ${drawerMember ? "pointer-events-auto" : "pointer-events-none"}`}>
        <div
          className={`absolute inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity duration-300 ${drawerMember ? "opacity-100" : "opacity-0"}`}
          onClick={closeDrawer}
        />
        <div className={`absolute inset-y-0 right-0 w-full max-w-md bg-white shadow-2xl transition-transform duration-300 ease-in-out ${drawerMember ? "translate-x-0" : "translate-x-full"}`}>
          {drawerMember && form && (
            <div className="flex h-full flex-col">
              <div className="flex items-center justify-between border-b border-slate-100 px-6 py-5">
                <div className="min-w-0">
                  <h3 className="truncate text-lg font-bold text-slate-800">{drawerMember.name}</h3>
                  <p className="mt-0.5 truncate text-xs font-medium text-slate-500">
                    {drawerMember.designation || drawerMember.role} · Desktop switches
                  </p>
                </div>
                <button type="button" onClick={closeDrawer} className="text-slate-400 hover:text-slate-600">
                  <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>

              <div className="flex-1 overflow-y-auto p-6 space-y-4">
                <p className="text-xs font-medium text-slate-500">
                  Choose what the Monitra desktop app does for this member.
                </p>
                <div className="divide-y divide-slate-100 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
                  {DESKTOP_SWITCHES.map(({ form: key, label, help }) => {
                    const helpId = `desktop-switch-${key}-help`;
                    return (
                      <div key={key} className="flex items-start justify-between gap-4 px-4 py-4">
                        <div className="min-w-0">
                          <span className="block text-sm font-bold text-slate-800">{label}</span>
                          <p id={helpId} className="mt-1 text-xs leading-5 text-slate-500">{help}</p>
                        </div>
                        <OnOffSwitch on={form[key]} label={label} onChange={(on) => setForm({ ...form, [key]: on })} />
                      </div>
                    );
                  })}
                </div>
                <p className="text-[11px] font-medium text-slate-500">
                  Changes take effect on a connected desktop within a few seconds, and on any desktop within three minutes.
                </p>

                {error && (
                  <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm font-semibold text-rose-600">
                    {error}
                  </div>
                )}
              </div>

              <div className="flex gap-3 border-t border-slate-100 bg-slate-50 p-6">
                <button
                  type="button"
                  onClick={closeDrawer}
                  className="flex-1 rounded-lg border border-slate-200 bg-white py-3 text-sm font-bold text-slate-700 shadow-sm transition hover:bg-slate-50"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={!isDirty || isSaving}
                  onClick={handleSave}
                  className={`flex-1 rounded-lg py-3 text-sm font-bold text-white shadow-md transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${GRADIENT_CYAN_PURPLE}`}
                >
                  {isSaving ? "Saving…" : "Save Changes"}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </V2Shell>
  );
};
