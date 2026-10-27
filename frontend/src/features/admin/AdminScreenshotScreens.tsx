import React, { useEffect, useMemo, useRef, useState } from "react";
import { ErrorNote, Spinner } from "../member/MemberUi";
import { useFeedback } from "../../components/FeedbackProvider";
import { Pagination } from "../../components/Pagination";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { formatApiError } from "../../api/utils";
import { FieldError, SEARCH_MAX_LENGTH, validateSearchTerm } from "../../validation";
import {
  useGetScreenshotDisplaysQuery,
  useSetScreenshotDisplaysMutation,
  type MemberScreen,
  type MemberScreens,
} from "../../store/api/screenshotDisplaysApi";

/**
 * Screenshot Management -> Multi-screen.
 *
 * A member's desktop reports the screens it can see; this lists the members who
 * have, says how many screens each is using right now, and lets an administrator
 * choose which of those screens are captured. A member with three screens can be
 * captured from one: the desktop leaves the others out before reading them, and
 * the screenshot is composed from the screens that remain.
 *
 * Only what the desktop reported is shown -- nothing is guessed for a member whose
 * computer has not reported. A screen that was unplugged since stays listed (muted,
 * "Not connected") so a choice made for it is not lost when it returns. A save
 * sends only the screens that changed, through `PUT /screenshot-displays/{id}`.
 *
 * The one rule the page mirrors from the server: at least one *connected* screen
 * stays on. Switching every screen off is "screenshot capture" for the member in
 * User Management, so it is explained here rather than offered.
 */

const inputClass =
  "w-full rounded-lg border bg-white px-3 py-2 text-sm text-slate-800 outline-none transition focus:border-[#38bdf8] focus:ring-2 focus:ring-[#38bdf8]/15";

const errorData = (error: unknown) => (error as { data?: unknown } | undefined)?.data;

type Filter = "multiple" | "all";

/** How many screens the member is using right now. */
const isMulti = (member: MemberScreens) => member.connected_count > 1;

const formatWhen = (iso: string | null) => {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
};

const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

const MonitorIcon: React.FC = () => (
  <svg className="h-5 w-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
    />
  </svg>
);

const ScreenToggle: React.FC<{
  member: string;
  screen: MemberScreen;
  on: boolean;
  busy: boolean;
  onChange: (next: boolean) => void;
}> = ({ member, screen, on, busy, onChange }) => (
  <button
    type="button"
    role="switch"
    aria-checked={on}
    aria-label={`Capture screen ${screen.number} of ${member}`}
    disabled={busy}
    onClick={() => onChange(!on)}
    className={
      "flex min-w-[190px] cursor-pointer items-center gap-3 rounded-xl border px-3 py-2.5 text-left transition focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/40 disabled:cursor-not-allowed disabled:opacity-60 " +
      (on ? "border-blue-200 bg-blue-50 text-blue-900" : "border-slate-200 bg-white text-slate-500") +
      (screen.connected ? "" : " border-dashed")
    }
  >
    <span className={on ? "text-blue-600" : "text-slate-400"}>
      <MonitorIcon />
    </span>
    <span className="min-w-0 flex-1">
      <span className="block text-[13px] font-bold">
        Screen {screen.number}
        {screen.is_primary && <span className="ml-1.5 text-[11px] font-semibold text-slate-500">Primary</span>}
      </span>
      <span className="block text-[11px] tabular-nums text-slate-500">
        {screen.width} × {screen.height}
        {!screen.connected && " · Not connected"}
      </span>
    </span>
    <span className={"shrink-0 text-[11px] font-bold " + (on ? "text-blue-700" : "text-slate-500")}>
      {on ? "Captured" : "Skipped"}
    </span>
  </button>
);

const MemberRow: React.FC<{ member: MemberScreens }> = ({ member }) => {
  const { showToast } = useFeedback();
  const [save, { isLoading: busy }] = useSetScreenshotDisplaysMutation();

  // The server's choice per screen is the truth; the row holds a draft of it.
  const savedKey = member.screens.map((s) => `${s.number}:${s.capture_enabled ? 1 : 0}`).join("|");
  const [draft, setDraft] = useState<Record<number, boolean>>(() =>
    Object.fromEntries(member.screens.map((s) => [s.number, s.capture_enabled])),
  );
  const syncedKey = useRef(savedKey);
  useEffect(() => {
    if (syncedKey.current === savedKey) return;
    syncedKey.current = savedKey;
    setDraft(Object.fromEntries(member.screens.map((s) => [s.number, s.capture_enabled])));
  }, [savedKey, member.screens]);

  const changed = member.screens.filter((s) => (draft[s.number] ?? s.capture_enabled) !== s.capture_enabled);
  const connected = member.screens.filter((s) => s.connected);
  const noneLeftOn = connected.length > 0 && !connected.some((s) => draft[s.number] ?? s.capture_enabled);
  const dirty = changed.length > 0 && !noneLeftOn;
  const errorId = `screens-${member.user_id}-error`;

  const submit = async () => {
    if (!dirty || busy) return;
    try {
      await save({
        userId: member.user_id,
        screens: changed.map((s) => ({ number: s.number, capture_enabled: draft[s.number] })),
      }).unwrap();
      showToast(`Screen capture saved for ${member.name}.`, "success");
    } catch (exc) {
      setDraft(Object.fromEntries(member.screens.map((s) => [s.number, s.capture_enabled])));
      showToast(formatApiError(errorData(exc), `Could not save the screens for ${member.name}.`), "error");
    }
  };

  return (
    <tr className="align-top">
      <td className="px-4 py-4">
        <div className="font-bold text-slate-800">{member.name || "-"}</div>
        <div className="text-xs text-slate-500">{member.email}</div>
      </td>
      <td className="px-4 py-4">
        <span
          className={
            "inline-flex items-center rounded-md border px-2.5 py-1 text-xs font-bold " +
            (isMulti(member)
              ? "border-violet-200 bg-violet-50 text-violet-700"
              : "border-slate-200 bg-slate-50 text-slate-600")
          }
        >
          {plural(member.connected_count, "screen", "screens")}
        </span>
        {member.last_reported_at && (
          <div className="mt-1.5 text-[11px] text-slate-500">Reported {formatWhen(member.last_reported_at)}</div>
        )}
      </td>
      <td className="px-4 py-4">
        <div className="flex flex-wrap gap-2" role="group" aria-label={`Screens of ${member.name}`}>
          {member.screens.map((screen) => (
            <ScreenToggle
              key={screen.number}
              member={member.name}
              screen={screen}
              on={draft[screen.number] ?? screen.capture_enabled}
              busy={busy}
              onChange={(next) => setDraft((current) => ({ ...current, [screen.number]: next }))}
            />
          ))}
        </div>
        {noneLeftOn && (
          <p id={errorId} role="alert" className="mt-2 text-xs font-semibold text-rose-600">
            Leave at least one connected screen on. To stop screenshots for {member.name} altogether, switch screenshot
            capture off for them in User Management.
          </p>
        )}
      </td>
      <td className="px-4 py-4 text-right">
        <button
          type="button"
          aria-label={`Save screens for ${member.name}`}
          aria-describedby={noneLeftOn ? errorId : undefined}
          disabled={!dirty || busy}
          onClick={() => void submit()}
          className="rounded-lg bg-gradient-to-r from-[#0ea5e9] via-[#3b82f6] to-[#8b5cf6] px-4 py-1.5 text-[12px] font-bold text-white shadow-sm transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save"}
        </button>
      </td>
    </tr>
  );
};

const FilterButton: React.FC<{ active: boolean; count: number; onClick: () => void; children: React.ReactNode }> = ({
  active,
  count,
  onClick,
  children,
}) => (
  <button
    type="button"
    aria-pressed={active}
    onClick={onClick}
    className={
      "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-bold transition " +
      (active ? "bg-white text-[#0F172A] shadow-sm" : "text-[#64748B] hover:text-[#334155]")
    }
  >
    {children}
    <span
      className={
        "rounded-full px-1.5 py-0.5 text-[10px] font-bold tabular-nums " +
        (active ? "bg-[#EFF6FF] text-[#2563EB]" : "bg-[#E2E8F0] text-[#64748B]")
      }
    >
      {count}
    </span>
  </button>
);

export const ScreenManagement: React.FC = () => {
  const overview = useGetScreenshotDisplaysQuery();

  const [filter, setFilter] = useState<Filter>("multiple");
  const [search, setSearch] = useState("");
  const [searchError, setSearchError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);

  const members = useMemo(() => overview.data?.members ?? [], [overview.data]);
  const multiCount = members.filter(isMulti).length;

  const searchCheck = validateSearchTerm(search, { fieldLabel: "Search" });
  const term = searchCheck.ok ? searchCheck.value.trim().toLowerCase() : "";
  const filtered = useMemo(
    () =>
      members
        .filter((m) => filter === "all" || isMulti(m))
        .filter((m) => !term || `${m.name} ${m.email ?? ""}`.toLowerCase().includes(term)),
    [members, filter, term],
  );
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const rows = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const nobodyReported = !overview.isLoading && !overview.isError && members.length === 0;

  return (
    <section aria-labelledby="screens-heading">
      <div className="rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
        <h2 id="screens-heading" className="sr-only">
          Multi-screen
        </h2>
        <div className="mb-4 flex items-start justify-between gap-3">
          <p className="text-sm text-slate-600">
            A screen that is switched off is never read: the screenshot is made from the screens that stay on, at the
            same size. Screens are numbered as the member&apos;s computer lists them, so a screen&apos;s size is shown
            beside its number to help you recognise it.
          </p>
          <InlineRefreshIndicator active={overview.isFetching && !overview.isLoading} label="Checking" />
        </div>

        {overview.isLoading ? (
          <Spinner label="Loading screens…" />
        ) : overview.isError ? (
          <ErrorNote message="Could not load the screens." onRetry={() => void overview.refetch()} />
        ) : nobodyReported ? (
          <p className="rounded-xl border-2 border-dashed border-slate-200 px-6 py-10 text-center text-sm font-semibold text-slate-500">
            No member&apos;s computer has reported its screens yet. A desktop reports them when the member signs in, and
            again whenever a screen is plugged in or removed.
          </p>
        ) : (
          <>
            <div className="mb-4 flex flex-wrap items-center gap-3">
              <div
                className="inline-flex shrink-0 items-center gap-1 rounded-lg bg-[#F1F5F9] p-1"
                role="group"
                aria-label="Which members to show"
              >
                <FilterButton
                  active={filter === "multiple"}
                  count={multiCount}
                  onClick={() => {
                    setFilter("multiple");
                    setPage(1);
                  }}
                >
                  Using multiple screens
                </FilterButton>
                <FilterButton
                  active={filter === "all"}
                  count={members.length}
                  onClick={() => {
                    setFilter("all");
                    setPage(1);
                  }}
                >
                  All members
                </FilterButton>
              </div>
              <div className="min-w-[220px] flex-1">
                <input
                  type="text"
                  aria-label="Search members"
                  placeholder="Search members by name or email..."
                  value={search}
                  maxLength={SEARCH_MAX_LENGTH}
                  aria-invalid={searchError ? true : undefined}
                  aria-describedby={searchError ? "screens-member-search-error" : undefined}
                  onChange={(event) => {
                    const next = event.target.value;
                    setSearch(next);
                    const result = validateSearchTerm(next, { fieldLabel: "Search" });
                    setSearchError(result.ok ? null : result.error);
                    setPage(1);
                  }}
                  className={inputClass + (searchError ? " border-rose-300" : " border-slate-200")}
                />
                <FieldError id="screens-member-search-error" message={searchError} />
              </div>
            </div>

            <div className="overflow-x-auto rounded-xl border border-slate-200">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-slate-500">
                  <tr>
                    <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Member</th>
                    <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Connected</th>
                    <th className="px-4 py-3 text-[11px] font-bold uppercase tracking-wider">Screens to capture</th>
                    <th className="px-4 py-3 text-right text-[11px] font-bold uppercase tracking-wider">
                      <span className="sr-only">Save</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {rows.length === 0 ? (
                    <tr>
                      <td colSpan={4} className="px-6 py-10 text-center text-sm font-semibold text-slate-500">
                        {term
                          ? "No members match your search."
                          : "No member is using more than one screen right now. Choose “All members” to see everyone who has reported."}
                      </td>
                    </tr>
                  ) : (
                    rows.map((member) => <MemberRow key={member.user_id} member={member} />)
                  )}
                </tbody>
              </table>
            </div>
            <p className="mt-2 text-xs text-slate-500">
              A change reaches a running desktop within a few minutes and applies to screenshots taken from then on.
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
