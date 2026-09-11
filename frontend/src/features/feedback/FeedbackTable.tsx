import React, { useState } from "react";
import { FEEDBACK_CATEGORY_LABELS } from "../../store/api/feedbackApi";
import type { Feedback } from "../../store/api/feedbackApi";
import { formatISTDate } from "../../utils/duration";
import { CATEGORY_STYLES } from "./feedbackFilters";

/**
 * The feedback list, shared by the member and the organization-wide page.
 *
 * The two pages differ only in which endpoint fills this table and whether the
 * submitter is shown, so the markup lives in one place. `showEmployee` is off
 * on the member screen: a person reading their own submissions already knows
 * who sent them, and an id column there is noise, not information.
 *
 * The row controls are currently visual-only. Feedback carries no workflow
 * status yet, so Working and Resolved do not change data.
 *
 * A wide table cannot shrink below its content, so on a narrow screen the rows
 * are rendered as stacked cards instead of being cut off.
 */

const columnHead =
  "px-5 py-3 text-left text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]";

/** Up to two initials, matching the avatars the rest of the app draws. */
const initials = (name: string) =>
  (name || "?")
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase() || "?";

/**
 * A stable colour per person, so the same submitter looks the same on every
 * row without the backend having to store an avatar.
 */
const avatarHue = (name: string) => {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) % 360;
  return hash;
};

/**
 * The category tag.
 *
 * Square-cornered rather than a lozenge: the tag sits in a column of a table
 * whose every other edge is square, and the pill shape fought that. The marker
 * beside the label is squared off for the same reason, and matches the one the
 * category dropdown draws so the filter and the column read as one control and
 * its result.
 */
const CategoryPill: React.FC<{ category: Feedback["category"] }> = ({ category }) => {
  const style = CATEGORY_STYLES[category] ?? CATEGORY_STYLES.other;
  return (
    <span
      className="inline-flex items-center gap-2 whitespace-nowrap rounded-md px-2.5 py-1.5 text-[11px] font-bold"
      style={{ color: style.text, backgroundColor: style.bg }}
    >
      <span className="h-2 w-2 rounded-[2px]" style={{ background: style.dot }} />
      {FEEDBACK_CATEGORY_LABELS[category] ?? category}
    </span>
  );
};

/** Keep the table preview short; the Action column opens the full message. */
const DescriptionCell: React.FC<{ message: string }> = ({ message }) => {
  return <div className="line-clamp-2 break-words">{message}</div>;
};

const Submitter: React.FC<{ item: Feedback }> = ({ item }) => {
  const hue = avatarHue(item.employee_name || "");
  return (
    <div className="flex items-center gap-3">
      <span
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-[11px] font-bold"
        style={{ background: `hsl(${hue} 70% 94%)`, color: `hsl(${hue} 55% 32%)` }}
        aria-hidden="true"
      >
        {initials(item.employee_name)}
      </span>
      <div className="min-w-0">
        <div className="truncate text-[13px] font-bold text-[#0F172A]">{item.employee_name}</div>
      </div>
    </div>
  );
};

export const FeedbackTable: React.FC<{ items: Feedback[]; showEmployee?: boolean; showActions?: boolean }> = ({
  items,
  showEmployee = true,
  showActions = false,
}) => {
  const [selectedDescription, setSelectedDescription] = useState<Feedback | null>(null);

  return (
    <>
    {/* Table — from `md` up */}
    <div className="hidden overflow-hidden rounded-xl border border-[#E2E8F0] bg-white shadow-sm md:block">
      <div className="overflow-x-auto">
        {/*
          `table-fixed` is what makes the truncation possible: under the default
          `auto` layout a cell grows to fit its content and `max-width` on a
          `<td>` is ignored, so no amount of ellipsis styling would have held
          the description column still.
        */}
        <table className="w-full min-w-[640px] table-fixed border-collapse">
          <colgroup>
            {showEmployee && <col style={{ width: 250 }} />}
            <col style={{ width: 190 }} />
            <col />
            <col style={{ width: 130 }} />
            {showActions && <col style={{ width: 270 }} />}
          </colgroup>
          <thead className="border-b border-[#E2E8F0] bg-[#F8FAFC]">
            <tr>
              {showEmployee && <th className={columnHead}>Employee</th>}
              <th className={columnHead}>Category</th>
              <th className={columnHead}>{showEmployee ? "Description" : "Reason"}</th>
              <th className={`${columnHead} text-right`}>Submitted</th>
              {showActions && <th className={`${columnHead} text-right`}>Action</th>}
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr
                key={item.id}
                className="border-b border-[#F1F5F9] transition-colors last:border-0 hover:bg-[#F8FAFC]"
              >
                {showEmployee && (
                  <td className="px-5 py-3.5">
                    <Submitter item={item} />
                  </td>
                )}
                <td className="px-5 py-3.5 align-top">
                  <CategoryPill category={item.category} />
                </td>
                <td className="px-5 py-3.5 align-top text-[13px] leading-5 text-[#334155]">
                  <DescriptionCell
                    message={item.message}
                  />
                </td>
                <td className="whitespace-nowrap px-5 py-3.5 text-right align-top text-[12px] font-semibold text-[#64748B]">
                  {formatISTDate(item.created_at)}
                </td>
                {showActions && (
                  <td className="px-5 py-3.5 text-right align-top">
                    <div className="flex justify-end gap-1.5">
                      <button
                        type="button"
                        className="rounded-lg border border-[#F59E0B]/30 bg-[#FFFBEB] px-3 py-1.5 text-[11px] font-bold text-[#B45309] transition hover:bg-[#FEF3C7]"
                      >
                        Working
                      </button>
                      <button
                        type="button"
                        className="rounded-lg border border-[#10B981]/30 bg-[#ECFDF5] px-3 py-1.5 text-[11px] font-bold text-[#047857] transition hover:bg-[#D1FAE5]"
                      >
                        Resolved
                      </button>
                      <button
                        type="button"
                        onClick={() => setSelectedDescription(item)}
                        className="rounded-lg border border-[#2563EB]/25 bg-[#EFF6FF] px-3 py-1.5 text-[11px] font-bold text-[#2563EB] transition hover:bg-[#DBEAFE]"
                      >
                        View
                      </button>
                    </div>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>

    {/* Stacked cards — below `md` */}
    <div className="space-y-3 md:hidden">
      {items.map((item) => (
        <div key={item.id} className="rounded-xl border border-[#E2E8F0] bg-white p-4 shadow-sm">
          <div className="flex items-start justify-between gap-3">
            <CategoryPill category={item.category} />
            <span className="shrink-0 text-[12px] font-semibold text-[#64748B]">
              {formatISTDate(item.created_at)}
            </span>
          </div>
          {showEmployee && (
            <div className="mt-3">
              <Submitter item={item} />
            </div>
          )}
          {showActions ? (
            <>
              <p className="mt-3 line-clamp-2 break-words text-[13px] leading-5 text-[#334155]">{item.message}</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                <button
                  type="button"
                  className="rounded-lg border border-[#F59E0B]/30 bg-[#FFFBEB] px-3 py-1.5 text-[11px] font-bold text-[#B45309] transition hover:bg-[#FEF3C7]"
                >
                  Working
                </button>
                <button
                  type="button"
                  className="rounded-lg border border-[#10B981]/30 bg-[#ECFDF5] px-3 py-1.5 text-[11px] font-bold text-[#047857] transition hover:bg-[#D1FAE5]"
                >
                  Resolved
                </button>
                <button
                  type="button"
                  onClick={() => setSelectedDescription(item)}
                  className="rounded-lg border border-[#2563EB]/25 bg-[#EFF6FF] px-3 py-1.5 text-[11px] font-bold text-[#2563EB] transition hover:bg-[#DBEAFE]"
                >
                  View
                </button>
              </div>
            </>
          ) : (
            <p className="mt-3 whitespace-pre-wrap break-words text-[13px] leading-5 text-[#334155]">{item.message}</p>
          )}
        </div>
      ))}
    </div>

      {selectedDescription && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#0F172A]/45 p-4 backdrop-blur-sm">
          <div className="flex max-h-[min(620px,90vh)] w-full max-w-2xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl">
            <div className="flex items-start justify-between border-b border-[#E2E8F0] px-6 py-5">
              <div className="min-w-0 pr-4">
                <div className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">
                  {FEEDBACK_CATEGORY_LABELS[selectedDescription.category] ?? selectedDescription.category}
                </div>
                <h2 className="mt-1 text-lg font-black text-[#0F172A]">Feedback Description</h2>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px] font-semibold text-[#64748B]">
                  {showEmployee && <span>{selectedDescription.employee_name}</span>}
                  <span>{formatISTDate(selectedDescription.created_at)}</span>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setSelectedDescription(null)}
                className="shrink-0 rounded-lg p-2 text-[#94A3B8] transition hover:bg-[#F8FAFC] hover:text-[#0F172A]"
                aria-label="Close description"
              >
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            <div className="overflow-y-auto px-6 py-5">
              <div className="mb-4 rounded-lg border border-[#E2E8F0] bg-[#F8FAFC] p-3">
                <div className="text-[10px] font-bold uppercase tracking-wider text-[#94A3B8]">Category</div>
                <div className="mt-1 text-[13px] font-bold text-[#0F172A]">
                  {FEEDBACK_CATEGORY_LABELS[selectedDescription.category] ?? selectedDescription.category}
                </div>
              </div>
              <p className="whitespace-pre-wrap break-words text-[13px] leading-6 text-[#334155]">
                {selectedDescription.message}
              </p>
            </div>
            <div className="flex justify-end border-t border-[#E2E8F0] bg-[#F8FAFC] px-6 py-4">
              <button
                type="button"
                onClick={() => setSelectedDescription(null)}
                className="rounded-lg bg-[#0F172A] px-4 py-2 text-[13px] font-bold text-white transition hover:bg-[#1E293B]"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
};
