import React, { useRef, useState, useEffect } from "react";
import { DateRangeFilter, type DateRange } from "../dashboard/v2/filters";
import { FEEDBACK_CATEGORY_LABELS } from "../../store/api/feedbackApi";
import type { Feedback, FeedbackCategory } from "../../store/api/feedbackApi";
import { FieldError, SEARCH_MAX_LENGTH, validateSearchTerm } from "../../validation";

/**
 * The filter bar both Feedback screens share, and the filtering it describes.
 *
 * Everything here runs client-side. `GET /feedback` and `GET /feedback/my`
 * accept `page`, `limit` and `category` and nothing else -- no search, no date
 * range, no submitter -- so the pages load the whole list once (see
 * `getAllFeedbackItems` in `feedbackApi`) and narrow it here. Filtering a
 * single server page instead would report "no results" for a match sitting on
 * page two.
 *
 * The date control is the app's own `DateRangeFilter`, unchanged: the presets
 * rail and calendar are already the design used on Dashboard, Reports and Time
 * Tracking, and a second date picker that looked almost the same would be one
 * to keep in step forever.
 */

/* ------------------------------------------------------------------ */
/* Categories                                                          */
/* ------------------------------------------------------------------ */

/**
 * A colour per category, so a reader scanning the column tells a problem
 * report from a suggestion without reading the words. The tints are the
 * palette the rest of the app already uses for status pills.
 */
export const CATEGORY_STYLES: Record<FeedbackCategory, { text: string; bg: string; dot: string }> = {
  suggestion: { text: "#7C3AED", bg: "#F5F3FF", dot: "#8B5CF6" },
  report_a_problem: { text: "#DC2626", bg: "#FEF2F2", dot: "#EF4444" },
  general_feedback: { text: "#2563EB", bg: "#EFF6FF", dot: "#3B82F6" },
  need_help: { text: "#B45309", bg: "#FFFBEB", dot: "#F59E0B" },
  account_login_issue: { text: "#BE123C", bg: "#FFF1F2", dot: "#F43F5E" },
  other: { text: "#475569", bg: "#F1F5F9", dot: "#94A3B8" },
};

export const CATEGORIES = Object.keys(FEEDBACK_CATEGORY_LABELS) as FeedbackCategory[];

/* ------------------------------------------------------------------ */
/* Filter state                                                        */
/* ------------------------------------------------------------------ */

/** Whose feedback the org-wide screen is showing. */
export type FeedbackScope = "all" | "employees" | "mine";

export interface FeedbackFilterState {
  search: string;
  category: FeedbackCategory | null;
  range: DateRange;
  scope: FeedbackScope;
  /** Empty means everyone — the same convention every other filter uses. */
  selectedMembers: string[];
}

/** `YYYY-MM-DD` for an ISO timestamp, read in the viewer's own timezone. */
const dayOf = (isoTimestamp: string) => {
  const date = new Date(isoTimestamp);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(
    date.getDate(),
  ).padStart(2, "0")}`;
};

/**
 * Apply the filter state to a list of feedback.
 *
 * `currentUserId` splits "employees" from "mine"; pass `null` on a screen that
 * has no scope control and the scope is ignored. The date comparison is on
 * calendar days rather than instants, so a message filed at 23:50 on the last
 * day of the range is inside it.
 */
export const filterFeedback = (
  items: Feedback[],
  { search, category, range, scope, selectedMembers }: FeedbackFilterState,
  currentUserId: number | null,
  /**
   * For a leader, the ids of the people they lead. `null` means "no
   * restriction", which is what an admin or HR gets, and what everyone gets
   * before the member list has loaded.
   */
  teamIds: Set<number> | null = null,
): Feedback[] => {
  // A rejected term narrows nothing rather than narrowing everything away, so
  // the list stays as it was while the box explains itself.
  const checked = validateSearchTerm(search, { fieldLabel: "Search" });
  const term = (checked.ok ? checked.value : "").toLowerCase();

  return items.filter((item) => {
    if (category && item.category !== category) return false;

    if (selectedMembers.length > 0 && !selectedMembers.includes(String(item.employee_id))) {
      return false;
    }

    const day = dayOf(item.created_at);
    if (range.from && day && day < range.from) return false;
    if (range.to && day && day > range.to) return false;

    if (currentUserId !== null) {
      if (scope === "mine" && item.employee_id !== currentUserId) return false;
      if (scope === "employees") {
        if (item.employee_id === currentUserId) return false;
        if (teamIds && !teamIds.has(item.employee_id)) return false;
      }
    }

    if (term) {
      const haystack = `${item.message} ${item.employee_name} ${
        FEEDBACK_CATEGORY_LABELS[item.category] ?? item.category
      }`.toLowerCase();
      if (!haystack.includes(term)) return false;
    }

    return true;
  });
};

/* ------------------------------------------------------------------ */
/* Controls                                                            */
/* ------------------------------------------------------------------ */

/** Closes a popover when the pointer goes down anywhere outside it. */
const useClickOutside = (onOutside: () => void, active: boolean) => {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!active) return;
    const handler = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onOutside();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [active, onOutside]);
  return ref;
};

export const SearchInput: React.FC<{ value: string; onChange: (v: string) => void }> = ({
  value,
  onChange,
}) => {
  const checked = validateSearchTerm(value, { fieldLabel: "Search" });
  const error = checked.ok ? null : checked.error;
  return (
  <div className="relative min-w-[200px] flex-1">
    <svg
      className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-[#94A3B8]"
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z" />
    </svg>
    <input
      type="search"
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder="Search feedback…"
      aria-label="Search feedback"
      maxLength={SEARCH_MAX_LENGTH}
      aria-invalid={error ? true : undefined}
      aria-describedby={error ? "feedback-search-error" : undefined}
      className="w-full rounded-lg border border-[#E2E8F0] bg-white py-2 pl-10 pr-9 text-[13px] font-medium text-[#0F172A] outline-none transition placeholder:font-normal placeholder:text-[#94A3B8] focus:border-[#38BDF8] focus:ring-2 focus:ring-[#38BDF8]/20"
    />
    <FieldError id="feedback-search-error" message={error} />
    {value && (
      <button
        type="button"
        onClick={() => onChange("")}
        aria-label="Clear search"
        className="absolute right-2.5 top-1/2 -translate-y-1/2 rounded p-1 text-[#94A3B8] transition hover:bg-[#F1F5F9] hover:text-[#475569]"
      >
        <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeWidth="2.5" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    )}
  </div>
  );
};

/**
 * The category picker.
 *
 * A dropdown rather than the row of chips this page used to carry: six
 * categories plus "All" wrapped onto two lines on a laptop and pushed the
 * table below the fold. A native `<select>` would not show the category's
 * colour, which is the thing that makes the list scannable.
 */
export const CategorySelect: React.FC<{
  value: FeedbackCategory | null;
  onChange: (value: FeedbackCategory | null) => void;
}> = ({ value, onChange }) => {
  const [open, setOpen] = useState(false);
  const ref = useClickOutside(() => setOpen(false), open);
  const style = value ? CATEGORY_STYLES[value] : null;

  const choose = (next: FeedbackCategory | null) => {
    onChange(next);
    setOpen(false);
  };

  const option = (active: boolean) =>
    "flex w-full items-center gap-2.5 px-3 py-2 text-left text-[13px] font-semibold transition " +
    (active ? "bg-[#F1F5F9] text-[#0F172A]" : "text-[#475569] hover:bg-[#F8FAFC]");

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={
          "flex min-w-[190px] items-center justify-between gap-3 rounded-lg border bg-white px-3.5 py-2 text-[13px] font-semibold text-[#0F172A] transition " +
          (open ? "border-[#38BDF8] ring-2 ring-[#38BDF8]/20" : "border-[#E2E8F0] hover:border-[#CBD5E1]")
        }
      >
        <span className="flex items-center gap-2">
          <span
            className="h-2.5 w-2.5 shrink-0 rounded-[3px]"
            style={{ background: style?.dot ?? "#CBD5E1" }}
          />
          {value ? FEEDBACK_CATEGORY_LABELS[value] : "All categories"}
        </span>
        <svg
          className={`h-4 w-4 shrink-0 text-[#94A3B8] transition-transform ${open ? "rotate-180" : ""}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute left-0 z-30 mt-2 w-[230px] overflow-hidden rounded-xl border border-[#E2E8F0] bg-white py-1 shadow-lg"
        >
          <button type="button" role="option" aria-selected={value === null} onClick={() => choose(null)} className={option(value === null)}>
            <span className="h-2.5 w-2.5 rounded-[3px] bg-[#CBD5E1]" />
            All categories
          </button>
          <div className="my-1 border-t border-[#F1F5F9]" />
          {CATEGORIES.map((category) => (
            <button
              key={category}
              type="button"
              role="option"
              aria-selected={value === category}
              onClick={() => choose(category)}
              className={option(value === category)}
            >
              <span className="h-2.5 w-2.5 rounded-[3px]" style={{ background: CATEGORY_STYLES[category].dot }} />
              {FEEDBACK_CATEGORY_LABELS[category]}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

/**
 * Whose feedback to show: everyone the caller may read, their people only, or
 * their own.
 *
 * This is a *view* filter over rows the caller is already entitled to, not a
 * permission boundary -- `GET /feedback` decides what arrives here.
 */
export const ScopeTabs: React.FC<{
  value: FeedbackScope;
  onChange: (value: FeedbackScope) => void;
  /** "My team" reads better than "Employees" for a leader. */
  employeesLabel: string;
  counts: Record<FeedbackScope, number>;
}> = ({ value, onChange, employeesLabel, counts }) => {
  const options: { id: FeedbackScope; label: string }[] = [
    { id: "all", label: "All" },
    { id: "employees", label: employeesLabel },
    { id: "mine", label: "My feedback" },
  ];

  return (
    <div className="inline-flex shrink-0 items-center gap-1 rounded-lg bg-[#F1F5F9] p-1" role="tablist" aria-label="Whose feedback">
      {options.map((item) => {
        const active = value === item.id;
        return (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(item.id)}
            className={
              "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-bold transition " +
              (active ? "bg-white text-[#0F172A] shadow-sm" : "text-[#64748B] hover:text-[#334155]")
            }
          >
            {item.label}
            <span
              className={
                "rounded-full px-1.5 py-0.5 text-[10px] font-bold tabular-nums " +
                (active ? "bg-[#EFF6FF] text-[#2563EB]" : "bg-[#E2E8F0] text-[#64748B]")
              }
            >
              {counts[item.id]}
            </span>
          </button>
        );
      })}
    </div>
  );
};

/**
 * The filter bar. `scope` is omitted on the member screen, which has one.
 * `members` is omitted wherever a member picker would not narrow anything --
 * the member's own "My feedback" screen, for instance.
 */
export const FeedbackFilterBar: React.FC<{
  search: string;
  onSearch: (value: string) => void;
  category: FeedbackCategory | null;
  onCategory: (value: FeedbackCategory | null) => void;
  range: DateRange;
  onRange: (value: DateRange) => void;
  onReset: () => void;
  isDirty: boolean;
  scope?: React.ReactNode;
  members?: React.ReactNode;
}> = ({ search, onSearch, category, onCategory, range, onRange, onReset, isDirty, scope, members }) => (
  <div className="rounded-xl border border-[#E2E8F0] bg-white p-4 shadow-sm">
    <div className="flex flex-wrap items-center gap-3">
      <SearchInput value={search} onChange={onSearch} />
      <CategorySelect value={category} onChange={onCategory} />
      <DateRangeFilter value={range} onChange={onRange} />
      {members}
      {isDirty && (
        <button
          type="button"
          onClick={onReset}
          className="rounded-lg px-3 py-2 text-[12px] font-bold text-[#64748B] transition hover:bg-[#F1F5F9] hover:text-[#0F172A]"
        >
          Reset
        </button>
      )}
      {scope && <div className="ml-auto">{scope}</div>}
    </div>
  </div>
);

/**
 * The span that covers every row given, so "no date filter" is a real range
 * the shared picker can display rather than a special empty state.
 *
 * Feedback is sparse and long-lived, so this page must not open on the last
 * seven days the way Dashboard and Reports do -- older messages would look
 * deleted. Opening on the full span shows everything, and narrowing is the
 * user's move.
 */
export const spanCovering = (items: Feedback[]): DateRange => {
  const today = dayOf(new Date().toISOString());
  const days = items.map((item) => dayOf(item.created_at)).filter(Boolean).sort();
  return { preset: "custom", from: days[0] || today, to: today };
};

/** A count line that says what the filters left, without over-claiming. */
export const ResultSummary: React.FC<{ shown: number; total: number }> = ({ shown, total }) => (
  <p className="text-[12px] font-semibold text-[#64748B]">
    {shown === total
      ? `${total} ${total === 1 ? "submission" : "submissions"}`
      : `${shown} of ${total} submissions match`}
  </p>
);
