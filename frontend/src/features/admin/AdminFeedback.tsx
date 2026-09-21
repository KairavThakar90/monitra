import React, { useEffect, useMemo, useState } from "react";
import { V2Shell } from "../dashboard/v2/V2Shell";
import { Card, EmptyState, ErrorNote, Spinner } from "../member/MemberUi";
import { FeedbackTable } from "../feedback/FeedbackTable";
import {
  FeedbackFilterBar,
  ResultSummary,
  ScopeTabs,
  filterFeedback,
  spanCovering,
  type FeedbackScope,
} from "../feedback/feedbackFilters";
import { useGetAllFeedbackItemsQuery } from "../../store/api/feedbackApi";
import type { FeedbackCategory } from "../../store/api/feedbackApi";
import { useGetAllMembersQuery } from "../../store/api/membersApi";
import { useAuth } from "../auth/authContext";
import { canManageFeedback } from "../feedback/feedbackActions";
import { isTeamScoped } from "../../utils/roles";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { PaginationArrow } from "../../components/PaginationArrow";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import type { DateRange } from "../dashboard/v2/filters";
import { MemberMultiSelect } from "../dashboard/v2/filters";

/**
 * Every feedback the caller may read, for Admin, HR and Leader.
 *
 * `GET /feedback` scopes rows to the caller's own organization and refuses any
 * other role with a 403, so this page sends no organization id. What it *does*
 * do is let the reader narrow that list: by search term, by category, by date,
 * and by whose feedback it is.
 *
 * All four filters run client-side, because the endpoint takes only `page`,
 * `limit` and `category`. The page therefore loads the whole list once through
 * `getAllFeedbackItems` and paginates the filtered result itself -- filtering a
 * single server page would report "no matches" for a row sitting on page two.
 *
 * The row controls are live for an administrator and read-only for everybody
 * else who can reach this screen. HR and Leader see the same list and the same
 * states and press nothing: marking feedback Working or Resolved emails the
 * employee who submitted it, and that is an administrator's act. The buttons
 * being hidden is a courtesy; `PATCH /feedback/{id}/status` is what enforces
 * it, and it refuses a non-administrator regardless of what this page renders.
 */

const PAGE_SIZE = 15;

export const AdminFeedback: React.FC = () => {
  const { currentUser } = useAuth();

  const { data, isLoading, isFetching, isError } = useGetAllFeedbackItemsQuery();
  const items = useMemo(() => data ?? [], [data]);

  /**
   * A leader's "My team" tab.
   *
   * `/members` is already narrowed to the caller's team server-side
   * (`visible_member_ids`), so the ids it returns are the right set without
   * this page deciding anything about who leads whom. It is only asked for
   * when the tab can appear, and only ever *narrows* what is displayed -- what
   * the caller is allowed to read is still `GET /feedback`'s answer.
   */
  /**
   * Whether this reader may drive the workflow. Mirrors the backend's
   * administrator set; a stale copy here can only hide a control, never
   * authorise one.
   */
  const canManage = canManageFeedback(currentUser);

  const teamScoped = isTeamScoped(currentUser);
  /**
   * Loaded for everyone who reaches this page, not only a leader: a leader
   * needs it to compute `teamIds` below, and Admin/HR need it to populate the
   * member filter picker.
   */
  const { data: members } = useGetAllMembersQuery();
  const teamIds = useMemo(
    () => (teamScoped && members ? new Set(members.map((member) => member.id)) : null),
    [teamScoped, members],
  );

  const [search, setSearch] = useState("");
  const [category, setCategory] = useState<FeedbackCategory | null>(null);
  const [scope, setScope] = useState<FeedbackScope>("all");
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  const [page, setPage] = useState(1);

  // Typing filters a list already in memory, but debouncing still keeps a long
  // list from being re-filtered and re-rendered on every keystroke.
  const debouncedSearch = useDebouncedValue(search);

  /**
   * The date window. `null` is "everything", which is what this page opens on:
   * feedback is sparse and long-lived, so defaulting to the last seven days
   * the way Dashboard and Reports do would make older messages look deleted.
   * Picking a range replaces it; Reset puts it back.
   */
  const [range, setRange] = useState<DateRange | null>(null);
  const fullRange = useMemo(() => spanCovering(items), [items]);
  const effectiveRange = range ?? fullRange;

  const currentUserId = currentUser?.id ?? null;

  const filters = useMemo(
    () => ({ search: debouncedSearch, category, range: effectiveRange, scope, selectedMembers }),
    [debouncedSearch, category, effectiveRange, scope, selectedMembers],
  );

  const visible = useMemo(
    () => filterFeedback(items, filters, currentUserId, teamIds),
    [items, filters, currentUserId, teamIds],
  );

  /**
   * The tab counts reflect the other filters, so switching tabs never lands on
   * an empty list whose count said otherwise.
   */
  const counts = useMemo(() => {
    const withScope = (next: FeedbackScope) =>
      filterFeedback(items, { ...filters, scope: next }, currentUserId, teamIds).length;
    return { all: withScope("all"), employees: withScope("employees"), mine: withScope("mine") };
  }, [items, filters, currentUserId, teamIds]);

  const isDirty =
    search !== "" ||
    category !== null ||
    scope !== "all" ||
    range !== null ||
    selectedMembers.length > 0;

  const resetFilters = () => {
    setSearch("");
    setCategory(null);
    setScope("all");
    setRange(null);
    setSelectedMembers([]);
  };

  // Any change to the filters invalidates the current page number: page 3 of a
  // result that now has one page would render as empty.
  useEffect(() => {
    setPage(1);
  }, [debouncedSearch, category, scope, effectiveRange.from, effectiveRange.to, selectedMembers]);

  const totalPages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  const pageItems = visible.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  return (
    <V2Shell
      title="Feedback"
      subtitle={
        canManage
          ? "Feedback & Help messages submitted from the Monitra desktop app. Marking one Working or Resolved emails the employee who submitted it."
          : "Feedback & Help messages submitted from the Monitra desktop app. View only — status updates are made by an administrator."
      }
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
          members={
            <MemberMultiSelect
              members={members ?? []}
              selected={selectedMembers}
              onChange={setSelectedMembers}
            />
          }
          scope={
            <ScopeTabs
              value={scope}
              onChange={setScope}
              employeesLabel={teamScoped ? "My team" : "Employees"}
              counts={counts}
            />
          }
        />

        {isError && <ErrorNote message="Feedback could not be loaded. Please try again." />}

        {isLoading ? (
          <Spinner label="Loading feedback…" />
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
                      : "Feedback appears here once someone sends it from the Monitra desktop app."
                  }
                />
              </Card>
            ) : (
              <div className={`transition-opacity ${isFetching ? "opacity-60" : ""}`}>
                <FeedbackTable items={pageItems} showActions canManage={canManage} />
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
    </V2Shell>
  );
};
