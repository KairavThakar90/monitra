import React, { useEffect, useMemo, useState } from "react";
import { MemberShell } from "./MemberShell";
import { Card, EmptyState, ErrorNote, Spinner } from "./MemberUi";
import { FeedbackTable } from "../feedback/FeedbackTable";
import {
  FeedbackFilterBar,
  ResultSummary,
  filterFeedback,
  spanCovering,
} from "../feedback/feedbackFilters";
import { useGetMyFeedbackItemsQuery } from "../../store/api/feedbackApi";
import type { FeedbackCategory } from "../../store/api/feedbackApi";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { PaginationArrow } from "../../components/PaginationArrow";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import type { DateRange } from "../dashboard/v2/filters";

/**
 * The member's own feedback.
 *
 * `/feedback/my` already answers with only the caller's submissions -- the
 * owner comes from the access token, not from anything this page sends -- so
 * nothing is filtered for *ownership* here and another person's message can
 * never reach this screen.
 *
 * Because every row is the reader's own, the table is shown without the
 * submitter columns: category, reason and date are the three things that
 * distinguish one of your own messages from another, and an employee id
 * beside your own name is noise. That is also why there is no "whose
 * feedback" control -- the answer is always "mine".
 *
 * Search, category and date all run client-side, for the same reason as the
 * org-wide screen: the endpoint accepts only `page` and `limit`.
 */

const PAGE_SIZE = 15;

export const MemberFeedback: React.FC = () => {
  const { data, isLoading, isFetching, isError } = useGetMyFeedbackItemsQuery();
  const items = useMemo(() => data ?? [], [data]);

  const [search, setSearch] = useState("");
  const [category, setCategory] = useState<FeedbackCategory | null>(null);
  const [page, setPage] = useState(1);
  const debouncedSearch = useDebouncedValue(search);

  // `null` is "everything", which is what this page opens on -- see the note
  // on the same state in AdminFeedback.
  const [range, setRange] = useState<DateRange | null>(null);
  const fullRange = useMemo(() => spanCovering(items), [items]);
  const effectiveRange = range ?? fullRange;

  const visible = useMemo(
    // `currentUserId` is null: this screen has no scope control, and passing an
    // id would make the unused "employees"/"mine" split meaningful here.
    () =>
      filterFeedback(
        items,
        { search: debouncedSearch, category, range: effectiveRange, scope: "all", selectedMembers: [] },
        null,
      ),
    [items, debouncedSearch, category, effectiveRange],
  );

  const isDirty = search !== "" || category !== null || range !== null;

  const resetFilters = () => {
    setSearch("");
    setCategory(null);
    setRange(null);
  };

  useEffect(() => {
    setPage(1);
  }, [debouncedSearch, category, effectiveRange.from, effectiveRange.to]);

  const totalPages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  const pageItems = visible.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  return (
    <MemberShell
      title="Feedback"
      subtitle="Every Feedback & Help message you have submitted from the desktop app."
      actions={<InlineRefreshIndicator active={isFetching && !isLoading} />}
    >
      <div className="w-full space-y-4 pb-20">
        <FeedbackFilterBar
          search={search}
          onSearch={setSearch}
          category={category}
          onCategory={setCategory}
          range={effectiveRange}
          onRange={setRange}
          onReset={resetFilters}
          isDirty={isDirty}
        />

        {isError && <ErrorNote message="Your feedback could not be loaded. Please try again." />}

        {isLoading ? (
          <Spinner label="Loading your feedback…" />
        ) : (
          <>
            <div className="flex items-center justify-between px-1">
              <ResultSummary shown={visible.length} total={items.length} />
              {totalPages > 1 && (
                <span className="text-[12px] font-semibold text-[#94A3B8]">
                  Page {page} of {totalPages}
                </span>
              )}
            </div>

            {pageItems.length === 0 ? (
              <Card>
                <EmptyState
                  message={isDirty ? "No feedback matches these filters." : "No feedback submitted yet."}
                  hint={
                    isDirty
                      ? "Try a wider date range, a different category, or clear the search."
                      : "Send feedback from the Monitra desktop app and it will appear here."
                  }
                />
              </Card>
            ) : (
              <div className={`transition-opacity ${isFetching ? "opacity-60" : ""}`}>
                <FeedbackTable items={pageItems} showEmployee={false} />
              </div>
            )}

            {totalPages > 1 && (
              <div className="flex items-center justify-between rounded-xl border border-[#E2E8F0] bg-white px-4 py-3 shadow-sm">
                <span className="text-[12px] font-semibold text-[#64748B]">
                  Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, visible.length)} of{" "}
                  {visible.length}
                </span>
                <div className="flex items-center gap-2">
                  <PaginationArrow
                    direction="prev"
                    disabled={page <= 1}
                    onClick={() => setPage((current) => Math.max(1, current - 1))}
                  />
                  <PaginationArrow
                    direction="next"
                    disabled={page >= totalPages}
                    onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
                  />
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </MemberShell>
  );
};
