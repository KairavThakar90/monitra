import React, { useMemo, useState } from "react";
import { V2Shell } from "../dashboard/v2/V2Shell";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { useFeedback } from "../../components/FeedbackProvider";
import { formatApiError } from "../../api/utils";
import { useGetMembersQuery, useUpdateMemberMutation, type Member } from "../../store/api/membersApi";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { PaginationArrow } from "../../components/PaginationArrow";
import { FieldError, SEARCH_MAX_LENGTH, validateSearchTerm } from "../../validation";

/**
 * Admin Settings → User Management.
 *
 * Per-user tracking settings — screenshot capture frequency and idle-time
 * detection — already live on `users.capture_frequency` / `idle_enabled` /
 * `idle_minutes` and already round-trip through `GET/PATCH /members/{id}`
 * (see `MemberResponse` / `MemberUpdate` in backend/app/schemas/member.py).
 * This page is the first place an administrator can actually set them,
 * rather than only inheriting whatever the SSO provider sent.
 *
 * `capture_frequency` has no unit of its own, and production data is not
 * fully consistent: the SSO login-sync path once wrote a literal `300`
 * meaning "seconds", but every other write path — 61 of 63 real accounts on
 * the live database — stores a plain minute count (`10`, matching the
 * desktop's actual 10-minute screenshot window). This page follows the
 * convention the data actually uses (minutes, matching
 * `backend/app/schemas/member.py`'s `CaptureFrequencyMinutes`) and shows the
 * stored value as-is: the admin has full control over the interval, with no
 * upper bound, so a value is never silently reinterpreted or clamped.
 *
 * The drawer also supports assigning the same tracking settings to several
 * selected members at once, applied with one PATCH per member.
 */

const GRADIENT_CYAN_PURPLE = "bg-gradient-to-r from-[#0ea5e9] via-[#3b82f6] to-[#8b5cf6]";

const MIN_IDLE_MINUTES = 1;
const MAX_IDLE_MINUTES = 120;
const MIN_CAPTURE_MINUTES = 1;

type FormState = {
  captureMinutes: string;
  idleEnabled: boolean;
  idleMinutes: string;
};

const toFormState = (member: Member): FormState => ({
  captureMinutes: String(member.capture_frequency ?? 10),
  idleEnabled: member.idle_enabled ?? true,
  idleMinutes: String(member.idle_minutes ?? 5),
});

const DEFAULT_FORM_STATE: FormState = { captureMinutes: "10", idleEnabled: true, idleMinutes: "5" };

const LoadingSpinner: React.FC = () => (
  <div className="flex min-h-28 items-center justify-center" role="status" aria-label="Loading">
    <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-500 border-t-transparent" />
  </div>
);

const Pagination: React.FC<{
  page: number;
  totalPages: number;
  totalItems: number;
  limit: number;
  setPage: (page: number) => void;
  setLimit: (limit: number) => void;
}> = ({ page, totalPages, totalItems, limit, setPage, setLimit }) => {
  const startItem = totalItems === 0 ? 0 : (page - 1) * limit + 1;
  const endItem = Math.min(page * limit, totalItems);
  const pages = totalPages <= 7
    ? Array.from({ length: totalPages }, (_, index) => index + 1)
    : page <= 4
      ? [1, 2, 3, 4, 5, "...", totalPages]
      : page >= totalPages - 3
        ? [1, "...", totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1, totalPages]
        : [1, "...", page - 1, page, page + 1, "...", totalPages];

  return (
    <div className="mt-6 flex flex-col items-center justify-between gap-4 border-t border-slate-200 pt-5 text-sm text-slate-500 sm:flex-row">
      <div>Showing {startItem} to {endItem} of {totalItems} members</div>
      <div className="flex items-center gap-3">
        <select value={limit} onChange={(e) => { setLimit(Number(e.target.value)); setPage(1); }} className="rounded-md border border-slate-300 py-1.5 pl-3 pr-8 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500">
          <option value={10}>10</option>
          <option value={20}>20</option>
          <option value={50}>50</option>
        </select>
        <div className="flex items-center gap-1">
          <PaginationArrow direction="prev" disabled={page === 1} onClick={() => setPage(page - 1)} />
          {pages.map((visiblePage, index) => visiblePage === "..." ? (
            <span key={`ellipsis-${index}`} className="flex h-8 w-8 items-center justify-center text-slate-400">...</span>
          ) : (
            <button key={visiblePage} onClick={() => setPage(visiblePage as number)} className={`flex h-8 w-8 items-center justify-center rounded text-sm font-semibold transition ${visiblePage === page ? "bg-blue-500 text-white shadow-sm" : "text-slate-600 hover:bg-slate-100"}`}>{visiblePage}</button>
          ))}
          <PaginationArrow direction="next" disabled={page === totalPages} onClick={() => setPage(page + 1)} />
        </div>
      </div>
    </div>
  );
};

/** A small pill showing minutes, tinted by how aggressive the cadence is. */
const MinutesPill: React.FC<{ minutes: number; tone: "sky" | "violet" }> = ({ minutes, tone }) => {
  const toneClasses = tone === "sky"
    ? "bg-sky-50 text-sky-700 border-sky-200"
    : "bg-violet-50 text-violet-700 border-violet-200";
  return (
    <span className={`inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs font-bold ${toneClasses}`}>
      {minutes} min
    </span>
  );
};

const IdleBadge: React.FC<{ enabled: boolean }> = ({ enabled }) =>
  enabled ? (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[11px] font-bold uppercase tracking-wider text-emerald-600">
      <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" /> Enabled
    </span>
  ) : (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-bold uppercase tracking-wider text-slate-500">
      <span className="h-1.5 w-1.5 rounded-full bg-slate-400" /> Disabled
    </span>
  );

export const AdminUserManagement: React.FC = () => {
  const { showToast } = useFeedback();

  const [search, setSearch] = useState("");
  const [searchError, setSearchError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);

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

  const items = data?.items ?? [];
  const totalPages = data?.pages || 1;
  const showFirstLoad = isLoading && !data;

  // Rows the admin has ticked, so the same tracking settings can be assigned
  // to several people in one drawer session instead of one row at a time.
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

  const [drawerTargets, setDrawerTargets] = useState<Member[] | null>(null);
  const [form, setForm] = useState<FormState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const isBulk = (drawerTargets?.length ?? 0) > 1;

  const openDrawer = (member: Member) => {
    setDrawerTargets([member]);
    setForm(toFormState(member));
    setError(null);
  };

  const openBulkDrawer = () => {
    if (selectedOnPage.length === 0) return;
    setDrawerTargets(selectedOnPage);
    // Selected members likely carry different settings today, so the form
    // opens with sensible defaults rather than implying they already match.
    setForm(DEFAULT_FORM_STATE);
    setError(null);
  };

  const closeDrawer = () => {
    setDrawerTargets(null);
    setForm(null);
    setError(null);
  };

  const captureMinutesNum = form ? Number(form.captureMinutes) : NaN;
  const idleMinutesNum = form ? Number(form.idleMinutes) : NaN;
  const captureValid = Number.isInteger(captureMinutesNum) && captureMinutesNum >= MIN_CAPTURE_MINUTES;
  const idleValid =
    Number.isInteger(idleMinutesNum) && idleMinutesNum >= MIN_IDLE_MINUTES && idleMinutesNum <= MAX_IDLE_MINUTES;

  const isDirty = useMemo(() => {
    if (!form || !drawerTargets) return false;
    if (isBulk) return true; // Assigning to a group is always an explicit action, not a diff.
    return JSON.stringify(form) !== JSON.stringify(toFormState(drawerTargets[0]));
  }, [drawerTargets, form, isBulk]);

  const handleSave = async () => {
    if (!drawerTargets || drawerTargets.length === 0 || !form || !captureValid || !idleValid) return;
    setError(null);
    const body = {
      capture_frequency: captureMinutesNum,
      idle_enabled: form.idleEnabled,
      idle_minutes: idleMinutesNum,
    };
    const results = await Promise.allSettled(
      drawerTargets.map((member) => updateMember({ id: member.id, body }).unwrap()),
    );
    const failures = results.filter((result) => result.status === "rejected");
    if (failures.length === 0) {
      showToast(
        drawerTargets.length === 1
          ? `Tracking settings saved for ${drawerTargets[0].name}.`
          : `Tracking settings applied to ${drawerTargets.length} members.`,
        "success",
      );
      setSelectedIds(new Set());
      closeDrawer();
      return;
    }
    const firstFailure = failures[0] as PromiseRejectedResult;
    const message = formatApiError(
      (firstFailure.reason as { data?: unknown })?.data,
      failures.length === drawerTargets.length
        ? "Could not save tracking settings."
        : `Saved for ${drawerTargets.length - failures.length} of ${drawerTargets.length} members; ${failures.length} failed.`,
    );
    setError(message);
    showToast(message, "error");
  };

  return (
    <V2Shell
      title="Settings"
      subtitle="User Management"
      actions={<InlineRefreshIndicator active={isFetching && !showFirstLoad} label="Refreshing" />}
    >
      <div className="w-full space-y-6 pb-20">
        <div className="flex flex-col gap-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm lg:flex-row lg:items-center lg:justify-between">
          <div className="flex flex-1 items-center gap-2 px-2">
            <svg className="h-5 w-5 shrink-0 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
              className="flex-1 bg-transparent text-sm text-slate-700 outline-none placeholder:text-slate-400"
            />
          </div>
          {searchError && (
            <div className="px-2">
              <FieldError id="settings-search-error" message={searchError} />
            </div>
          )}
        </div>

        <div className="relative overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 bg-slate-50 px-6 py-4">
            <div>
              <h3 className="text-sm font-bold text-slate-800">Tracking Settings by Member</h3>
              <p className="mt-0.5 text-xs text-slate-500">Screenshot cadence and idle-time detection, applied per person.</p>
            </div>
            {selectedOnPage.length > 0 && (
              <button
                onClick={openBulkDrawer}
                className={`inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-bold text-white shadow-sm transition hover:opacity-90 ${GRADIENT_CYAN_PURPLE}`}
              >
                Assign to {selectedOnPage.length} selected
              </button>
            )}
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm whitespace-nowrap">
              <thead className="border-b border-slate-200 bg-slate-50 text-slate-500">
                <tr>
                  <th className="w-10 px-6 py-4">
                    <input
                      type="checkbox"
                      checked={allOnPageSelected}
                      onChange={toggleSelectAllOnPage}
                      aria-label="Select all members on this page"
                      className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                    />
                  </th>
                  <th className="px-6 py-4 text-[11px] font-bold uppercase tracking-wider">Employee</th>
                  <th className="px-6 py-4 text-[11px] font-bold uppercase tracking-wider">Screenshot Frequency</th>
                  <th className="px-6 py-4 text-[11px] font-bold uppercase tracking-wider">Idle Monitoring</th>
                  <th className="px-6 py-4 text-[11px] font-bold uppercase tracking-wider">Idle Threshold</th>
                  <th className="px-6 py-4 text-right text-[11px] font-bold uppercase tracking-wider">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {showFirstLoad ? (
                  <tr>
                    <td colSpan={6} className="px-6 py-8">
                      <LoadingSpinner />
                    </td>
                  </tr>
                ) : isError ? (
                  <tr>
                    <td colSpan={6} className="px-6 py-12 text-center text-red-500">
                      Failed to fetch members. Please try again.
                    </td>
                  </tr>
                ) : items.length > 0 ? (
                  items.map((member) => (
                    <tr key={member.id} className={`transition hover:bg-slate-50/50 ${selectedIds.has(member.id) ? "bg-blue-50/40" : ""}`}>
                      <td className="px-6 py-4">
                        <input
                          type="checkbox"
                          checked={selectedIds.has(member.id)}
                          onChange={() => toggleSelected(member.id)}
                          aria-label={`Select ${member.name}`}
                          className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                        />
                      </td>
                      <td className="px-6 py-4">
                        <div className="flex items-center gap-3">
                          <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-xs font-bold text-white shadow-sm ${GRADIENT_CYAN_PURPLE}`}>
                            {(member.name || "U").substring(0, 2).toUpperCase()}
                          </div>
                          <div className="min-w-0">
                            <div className="truncate font-bold text-slate-800">{member.name || "-"}</div>
                            <div className="mt-0.5 truncate text-xs text-slate-500">{member.email || "-"}</div>
                          </div>
                        </div>
                      </td>
                      <td className="px-6 py-4">
                        <MinutesPill minutes={member.capture_frequency ?? 10} tone="sky" />
                      </td>
                      <td className="px-6 py-4">
                        <IdleBadge enabled={member.idle_enabled ?? true} />
                      </td>
                      <td className="px-6 py-4">
                        <MinutesPill minutes={member.idle_minutes ?? 5} tone="violet" />
                      </td>
                      <td className="px-6 py-4 text-right">
                        <button
                          onClick={() => openDrawer(member)}
                          className="inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-[#14B8A6] border border-[#14B8A6]/30 transition hover:bg-[#14B8A6]/10"
                        >
                          <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                          </svg>
                          Configure
                        </button>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={6} className="px-6 py-12 text-center text-slate-500">
                      No members found matching your criteria.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {totalPages > 1 && (
          <Pagination page={page} totalPages={totalPages} totalItems={data?.total || 0} limit={pageSize} setPage={setPage} setLimit={setPageSize} />
        )}
      </div>

      {/* Right slide-over: Tracking Settings for one or several members */}
      <div className={`fixed inset-0 z-50 overflow-hidden ${drawerTargets ? "pointer-events-auto" : "pointer-events-none"}`}>
        <div
          className={`absolute inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity duration-300 ${drawerTargets ? "opacity-100" : "opacity-0"}`}
          onClick={closeDrawer}
        />
        <div className={`absolute inset-y-0 right-0 w-full max-w-md bg-white shadow-2xl transition-transform duration-300 ease-in-out ${drawerTargets ? "translate-x-0" : "translate-x-full"}`}>
          {drawerTargets && drawerTargets.length > 0 && form && (
            <div className="flex h-full flex-col">
              {/* Drawer Header — matches the Create/Edit Project drawer */}
              <div className="flex items-center justify-between border-b border-slate-100 px-6 py-5">
                <div className="min-w-0">
                  <h2 className="truncate text-xl font-black text-slate-800">
                    {isBulk ? `${drawerTargets.length} members selected` : drawerTargets[0].name}
                  </h2>
                  <p className="mt-1 truncate text-sm font-semibold text-slate-500">
                    {isBulk
                      ? "Tracking settings will be applied to every selected member"
                      : `${drawerTargets[0].designation || drawerTargets[0].role} · Tracking settings`}
                  </p>
                </div>
                <button onClick={closeDrawer} className="rounded-full p-2 text-slate-400 transition hover:bg-slate-50 hover:text-slate-600">
                  <svg className="h-6 w-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>

              {/* Drawer Content */}
              <div className="flex-1 overflow-y-auto p-6">
                {isBulk && (
                  <div className="mb-5 flex flex-wrap gap-1.5">
                    {drawerTargets.map((member) => (
                      <span key={member.id} className="rounded-md bg-slate-100 px-2 py-1 text-[11px] font-semibold text-slate-600">
                        {member.name}
                      </span>
                    ))}
                  </div>
                )}
                <div>
                  <h3 className="mb-4 text-xs font-black uppercase tracking-widest text-[#3B82F6]">Tracking Settings</h3>
                  <div className="space-y-4">
                    <div>
                      <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">
                        Screenshot Capture Frequency
                      </label>
                      <input
                        type="number"
                        min={MIN_CAPTURE_MINUTES}
                        value={form.captureMinutes}
                        onChange={(event) => setForm({ ...form, captureMinutes: event.target.value })}
                        className="w-full rounded-lg border border-slate-300 bg-white px-4 py-2.5 text-sm font-medium text-slate-700 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6]"
                      />
                      <p className="mt-1.5 text-[11px] font-semibold text-slate-500">Interval in minutes — any value the role requires (e.g., 1, 5, 10, 30)</p>
                      {!captureValid && (
                        <p className="mt-1.5 text-xs font-semibold text-rose-600">
                          Must be a whole number of at least {MIN_CAPTURE_MINUTES} minute.
                        </p>
                      )}
                    </div>

                    <label className="flex cursor-pointer items-center justify-between gap-3 rounded-lg border-2 border-slate-200 bg-white p-3 transition hover:bg-slate-50 has-[:checked]:border-[#3B82F6] has-[:checked]:bg-blue-50">
                      <span>
                        <span className="block text-sm font-bold text-slate-700">Idle Time Monitoring</span>
                        <span className="block text-xs font-medium text-slate-500">Enable Idle Time Detection</span>
                      </span>
                      <input
                        type="checkbox"
                        checked={form.idleEnabled}
                        onChange={(event) => setForm({ ...form, idleEnabled: event.target.checked })}
                        className="h-5 w-5 shrink-0 rounded border-slate-300 text-[#3B82F6] focus:ring-[#3B82F6]"
                      />
                    </label>

                    <div>
                      <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">
                        Idle Time Threshold (Minutes)
                      </label>
                      <input
                        type="number"
                        min={MIN_IDLE_MINUTES}
                        max={MAX_IDLE_MINUTES}
                        value={form.idleMinutes}
                        disabled={!form.idleEnabled}
                        onChange={(event) => setForm({ ...form, idleMinutes: event.target.value })}
                        className="w-full rounded-lg border border-slate-300 bg-white px-4 py-2.5 text-sm font-medium text-slate-700 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-400"
                      />
                      <p className="mt-1.5 text-[11px] font-semibold text-slate-500">e.g., 1, 3, 5, 10 minutes</p>
                      {form.idleEnabled && !idleValid && (
                        <p className="mt-1.5 text-xs font-semibold text-rose-600">
                          Must be a whole number between {MIN_IDLE_MINUTES} and {MAX_IDLE_MINUTES} minutes.
                        </p>
                      )}
                    </div>

                    {error && (
                      <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm font-semibold text-rose-600">
                        {error}
                      </div>
                    )}
                  </div>
                </div>
              </div>

              {/* Drawer Footer */}
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
                  disabled={!isDirty || isSaving || !captureValid || (form.idleEnabled && !idleValid)}
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
