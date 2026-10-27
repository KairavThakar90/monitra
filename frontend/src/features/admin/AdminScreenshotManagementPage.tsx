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
import { ScreenManagement } from "./AdminScreenshotScreens";
import { useGetScreenshotDisplaysQuery } from "../../store/api/screenshotDisplaysApi";
import { PillTabs } from "../feedback/feedbackFilters";

/**
 * Settings -> Screenshot Management: everything an administrator decides about
 * screenshots and idle detection, on one page, as four tabs.
 *
 * 1. **Resolution and quality** -- what every screenshot looks like (`SizeAndQuality`).
 * 2. **Idle management** -- per member, how often a screenshot is taken and whether,
 *    and after how long, the desktop treats the member as idle (`IdleDetection`).
 * 3. **Multi-screen** -- how many screens each member is using, and which of them
 *    are captured (`ScreenManagement`).
 * 4. **Privacy rules** -- the applications and websites that are never captured
 *    (`AdminScreenshotPrivacy`, formerly a page of its own).
 *
 * This page only arranges them. Every panel stays mounted while its tab is not
 * selected (it is hidden, not removed), so a picked member, a half-typed size or
 * an open search is still there when the tab is selected again.
 *
 * Who is captured at all is not decided here: it is a per-member switch in
 * User Management, beside the notification and activity-tracking switches.
 *
 * `/admin/settings/screenshot-privacy` redirects here with `#privacy`, which
 * selects that tab on arrival.
 */

type SectionId = "look" | "idle" | "screens" | "privacy";

const TABS: ReadonlyArray<{ id: SectionId; title: string; description: string }> = [
  { id: "look", title: "Resolution and quality", description: "The size of every screenshot, and how sharp it is saved." },
  {
    id: "idle",
    title: "Idle management",
    description:
      "Per member: how often a screenshot is taken, whether the desktop watches for inactivity, and after how many minutes.",
  },
  {
    id: "screens",
    title: "Multi-screen",
    description:
      "Which members use more than one screen, and which of each member's screens are captured.",
  },
  { id: "privacy", title: "Privacy rules", description: "Applications and websites that are blanked out of a member's screenshots." },
];

/** Resolution and quality is selected by default; a `#section` in the address selects that one instead. */
const initialTab = (hash: string): SectionId => TABS.find((tab) => `#${tab.id}` === hash)?.id ?? "look";

const errorStatus = (error: unknown) => (error as { status?: unknown } | undefined)?.status;

export const AdminScreenshotManagementPage: React.FC = () => {
  const location = useLocation();
  const settings = useGetScreenshotSettingsQuery();
  const data = settings.data;
  const forbidden = errorStatus(settings.error) === 403;
  // Not before the settings answer: a non-administrator is refused there, and
  // must not see members first.
  const ready = !settings.isLoading && !forbidden;

  // The same cached queries the sections make themselves, read here only for the
  // one-line summaries beside each tab name.
  const members = useGetAllMembersQuery(undefined, { skip: !ready });
  const applications = useGetScreenshotApplicationsQuery(undefined, { skip: !ready });
  const websites = useGetScreenshotUrlsQuery(undefined, { skip: !ready });
  const screens = useGetScreenshotDisplaysQuery(undefined, { skip: !ready });

  const [active, setActive] = useState<SectionId>(() => initialTab(location.hash));

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

  const multiScreen = screens.data ? screens.data.members.filter((m) => m.connected_count > 1).length : null;
  const screensSummary =
    multiScreen === null ? null : screens.data!.members.length === 0 ? "None reported" : `${multiScreen} multi-screen`;

  const summaries: Record<SectionId, string | null> = {
    look: lookSummary,
    idle: idleSummary,
    screens: screensSummary,
    privacy: privacySummary,
  };

  return (
    <V2Shell
      title="Screenshot Management"
      subtitle="Decide what a screenshot looks like, how often it is taken, when a member counts as idle, and what is never captured."
    >
      <div className="flex w-full flex-col gap-4 pb-12">
        {forbidden ? (
          <ErrorNote message="Only administrators can open this page." onRetry={null} />
        ) : (
          // No `overflow-hidden` on this card: the member picker in the privacy
          // panel opens a dropdown that can extend past it.
          <div className="rounded-2xl border border-[#E2E8F0] bg-white shadow-sm">
            <div className="overflow-x-auto border-b border-[#E2E8F0] px-4 py-3 sm:px-5">
              <PillTabs<SectionId>
                options={TABS.map(({ id, title }) => ({ id, label: title }))}
                value={active}
                onChange={setActive}
                counts={summaries}
                label="Screenshot management sections"
                idPrefix="screenshot"
              />
            </div>

            {/* Hidden, not unmounted: what a panel holds survives switching tabs. */}
            {TABS.map((tab) => (
              <div
                key={tab.id}
                id={`screenshot-${tab.id}-panel`}
                role="tabpanel"
                aria-labelledby={`screenshot-${tab.id}-tab`}
                hidden={active !== tab.id}
                className="rounded-b-2xl bg-[#F8FAFC] p-5 sm:p-6"
              >
                <p className="mb-4 text-[13px] leading-5 text-[#64748B]">
                  {tab.description} Desktops pick up a change within a few minutes.
                </p>
                {tab.id === "look" ? (
                  settings.isLoading ? (
                    <Spinner label="Loading screenshot settings…" />
                  ) : settings.isError || !data ? (
                    <ErrorNote message="Could not load the screenshot settings." onRetry={() => void settings.refetch()} />
                  ) : (
                    <SizeAndQuality settings={data} />
                  )
                ) : tab.id === "idle" ? (
                  ready ? (
                    <IdleDetection />
                  ) : (
                    <Spinner label="Loading members…" />
                  )
                ) : tab.id === "screens" ? (
                  ready ? (
                    <ScreenManagement />
                  ) : (
                    <Spinner label="Loading screens…" />
                  )
                ) : ready ? (
                  <AdminScreenshotPrivacy embedded />
                ) : (
                  <Spinner label="Loading privacy rules…" />
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </V2Shell>
  );
};
