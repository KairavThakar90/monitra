import React, { useState } from "react";
import { useLocation } from "react-router-dom";
import { V2Shell } from "../dashboard/v2/V2Shell";
import { ErrorNote, Spinner } from "../member/MemberUi";
import { useGetAllMembersQuery } from "../../store/api/membersApi";
import { useGetScreenshotApplicationsQuery, useGetScreenshotUrlsQuery } from "../../api/screenshotPrivacy";
import { idleIsOn, isCapturableMember, useGetScreenshotSettingsQuery } from "../../store/api/screenshotSettingsApi";
import { AdminScreenshotPrivacy } from "./AdminScreenshotPrivacy";
import { SizeAndQuality } from "./AdminScreenshotManagement";
import { IdleDetection } from "./AdminIdleDetection";

/**
 * Settings -> Screenshot Management: everything an administrator decides about
 * screenshots and idle detection, on one page, as an accordion of three sections.
 *
 * 1. **Size and quality** -- what every screenshot looks like (`SizeAndQuality`).
 * 2. **Idle detection** -- whether, and after how long, the desktop treats a
 *    member as idle (`IdleDetection`).
 * 3. **Privacy rules** -- the applications and websites that are never captured
 *    (`AdminScreenshotPrivacy`, formerly a page of its own).
 *
 * This page only arranges them. Every section stays mounted while collapsed (it
 * is hidden, not removed), so a picked member, a half-typed size or an open
 * search is still there when the section is opened again.
 *
 * Who is captured at all is not decided here: it is a per-member switch in
 * User Management, beside the notification and activity-tracking switches.
 *
 * `/admin/settings/screenshot-privacy` now redirects here with `#privacy`, which
 * opens that section on arrival.
 */

type SectionId = "look" | "idle" | "privacy";
const SECTION_IDS: readonly SectionId[] = ["look", "idle", "privacy"];

/** Size and quality is open by default; a `#section` in the address opens that one instead. */
const initiallyOpen = (hash: string): Record<SectionId, boolean> => {
  const target = SECTION_IDS.find((id) => `#${id}` === hash) ?? "look";
  return { look: target === "look", idle: target === "idle", privacy: target === "privacy" };
};

const errorStatus = (error: unknown) => (error as { status?: unknown } | undefined)?.status;

const Icon: React.FC<{ path: string }> = ({ path }) => (
  <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d={path} />
  </svg>
);

const ICONS: Record<SectionId, string> = {
  // picture
  look:
    "M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z",
  // clock
  idle: "M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z",
  // shield
  privacy:
    "M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z",
};

const AccordionSection: React.FC<{
  id: SectionId;
  title: string;
  description: string;
  summary: string | null;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}> = ({ id, title, description, summary, open, onToggle, children }) => (
  <section
    className={
      // No `overflow-hidden`: the member picker inside the privacy section opens a
      // dropdown that can extend past the panel, and clipping it would cut it off.
      "rounded-2xl border bg-white transition-shadow " +
      (open ? "border-[#CBD5E1] shadow-md" : "border-[#E2E8F0] shadow-sm hover:shadow-md")
    }
  >
    <h2 className="m-0">
      <button
        type="button"
        id={`screenshot-${id}-header`}
        aria-expanded={open}
        aria-controls={`screenshot-${id}-panel`}
        onClick={onToggle}
        className={
          "flex w-full cursor-pointer items-center gap-4 rounded-t-2xl px-5 py-4 text-left transition-colors hover:bg-slate-50/70 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500/40 " +
          (open ? "" : "rounded-b-2xl")
        }
      >
        <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-[#3B82F6] to-[#8B5CF6] text-white shadow-sm">
          <Icon path={ICONS[id]} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-[17px] font-extrabold leading-6 tracking-tight text-[#0F172A]">{title}</span>
          <span className="mt-0.5 block text-[13px] leading-5 text-[#64748B]">{description}</span>
        </span>
        {summary && (
          <span className="hidden shrink-0 rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-[11px] font-bold text-slate-600 sm:inline-flex">
            {summary}
          </span>
        )}
        <svg
          className={"h-5 w-5 shrink-0 text-slate-400 transition-transform duration-200 " + (open ? "rotate-180" : "")}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
    </h2>
    {/* Hidden, not unmounted: what the section holds survives being collapsed. */}
    <div
      id={`screenshot-${id}-panel`}
      role="region"
      aria-labelledby={`screenshot-${id}-header`}
      hidden={!open}
      className="rounded-b-2xl border-t border-[#EEF2F7] bg-[#F8FAFC] p-5 sm:p-6"
    >
      {children}
    </div>
  </section>
);

export const AdminScreenshotManagementPage: React.FC = () => {
  const location = useLocation();
  const settings = useGetScreenshotSettingsQuery();
  const data = settings.data;
  const forbidden = errorStatus(settings.error) === 403;
  // Not before the settings answer: a non-administrator is refused there, and
  // must not see members first.
  const ready = !settings.isLoading && !forbidden;

  // The same cached queries the sections make themselves, read here only for the
  // one-line summaries beside each heading.
  const members = useGetAllMembersQuery(undefined, { skip: !ready });
  const applications = useGetScreenshotApplicationsQuery(undefined, { skip: !ready });
  const websites = useGetScreenshotUrlsQuery(undefined, { skip: !ready });

  const [open, setOpen] = useState<Record<SectionId, boolean>>(() => initiallyOpen(location.hash));
  const toggle = (id: SectionId) => setOpen((current) => ({ ...current, [id]: !current[id] }));
  const everyOpen = SECTION_IDS.every((id) => open[id]);
  const setAll = (value: boolean) => setOpen({ look: value, idle: value, privacy: value });

  const people = (members.data ?? []).filter(isCapturableMember);
  const idleSummary = !members.data
    ? null
    : people.length === 0
      ? "No active members"
      : `${people.filter(idleIsOn).length} of ${people.length} on`;

  const lookSummary = data
    ? `${data.image_width} × ${data.image_height} · ${data.image_quality === null ? "Automatic" : `${data.image_quality}%`}`
    : null;

  const ruleCount = applications.data && websites.data ? applications.data.length + websites.data.length : null;
  const privacySummary =
    ruleCount === null ? null : ruleCount === 0 ? "No rules yet" : `${ruleCount} ${ruleCount === 1 ? "rule" : "rules"}`;

  return (
    <V2Shell
      title="Screenshot Management"
      subtitle="Decide what a screenshot looks like, when a member counts as idle, and what is never captured."
    >
      <div className="flex w-full flex-col gap-4 pb-12">
        {forbidden ? (
          <ErrorNote message="Only administrators can open this page." onRetry={null} />
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-3 px-1">
              <p className="min-w-0 text-[13px] leading-5 text-[#64748B]">
                Open a section to change its settings. Desktops pick up a change within a few minutes.
              </p>
              <button
                type="button"
                onClick={() => setAll(!everyOpen)}
                className="shrink-0 cursor-pointer rounded-lg border border-slate-200 bg-white px-3 py-2 text-[12px] font-bold text-slate-600 transition hover:bg-slate-50"
              >
                {everyOpen ? "Collapse all" : "Expand all"}
              </button>
            </div>

            <AccordionSection
              id="look"
              title="Size and quality"
              description="The size of every screenshot, and how sharp it is saved."
              summary={lookSummary}
              open={open.look}
              onToggle={() => toggle("look")}
            >
              {settings.isLoading ? (
                <Spinner label="Loading screenshot settings…" />
              ) : settings.isError || !data ? (
                <ErrorNote message="Could not load the screenshot settings." onRetry={() => void settings.refetch()} />
              ) : (
                <SizeAndQuality settings={data} />
              )}
            </AccordionSection>

            <AccordionSection
              id="idle"
              title="Idle detection"
              description="Whether the desktop watches each member for inactivity, and after how many minutes."
              summary={idleSummary}
              open={open.idle}
              onToggle={() => toggle("idle")}
            >
              {ready ? <IdleDetection /> : <Spinner label="Loading members…" />}
            </AccordionSection>

            <AccordionSection
              id="privacy"
              title="Privacy rules"
              description="Applications and websites that are blanked out of a member's screenshots."
              summary={privacySummary}
              open={open.privacy}
              onToggle={() => toggle("privacy")}
            >
              {ready ? <AdminScreenshotPrivacy embedded /> : <Spinner label="Loading privacy rules…" />}
            </AccordionSection>
          </>
        )}
      </div>
    </V2Shell>
  );
};
