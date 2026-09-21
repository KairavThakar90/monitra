import React, { useMemo, useState } from 'react';
import { V2Shell } from '../dashboard/v2/V2Shell';
import { useAuth } from '../auth/authContext';
import { canDeleteScreenshots, canViewAllScreenshots } from '../auth/roles';
import { useGetAllMembersQuery } from '../../store/api/membersApi';
import { useGetAllProjectsQuery } from '../../store/api/projectsApi';
import {
  useDeleteScreenshotMutation,
  useGetScreenshotDayQuery,
} from '../../store/api/screenshotsApi';
import type { ScreenshotDay, ScreenshotMemberDays } from '../../store/api/screenshotsApi';
import { MemberMultiSelect, ProjectMultiSelect } from '../dashboard/v2/filters';
import { DayFilter, istTodayIso } from '../screenshots/DayFilter';
import { groupWindowsByHour } from '../screenshots/hours';
import { HourRow } from '../screenshots/HourRow';
import { ScreenshotLightbox } from '../screenshots/ScreenshotLightbox';
import type { LightboxItem } from '../screenshots/ScreenshotLightbox';
import { InlineRefreshIndicator } from '../../components/InlineRefreshIndicator';
import { useFeedback } from '../../components/FeedbackProvider';
import { formatHMS, formatISTDate } from '../../utils/duration';

/**
 * Screenshots, for the people allowed to see someone else's.
 *
 * Two audiences share this screen, and the difference between them is the
 * member picker:
 *
 * - **Admin / HR** (`canViewAllScreenshots`) open on *every* employee — one
 *   request to `/time-entry-screenshots/day` for the selected day — and can
 *   narrow with the member filter. Each employee is an accordion section
 *   headed by their name and the time they worked, so a long roster is a list
 *   you scan rather than a page you scroll.
 * - **A leader** reaches this route too — they hold `view_employees`, which is
 *   what gates the read-only `/admin` screens — but screenshots are not part of
 *   a leader's authority over their team. They get no picker, and the request
 *   pins `user_id` to themselves.
 *
 * The day is the unit of this screen: it opens on today, and inside a member's
 * section the captures are grouped into hour rows, each headed by the time
 * actually worked in that hour. The hour is what someone reviewing a day looks
 * for; the ten-minute capture windows live inside it as cards.
 *
 * Everything here comes from the API. There is no local sample data: a day
 * with no captures renders an empty state that says so, because a stand-in
 * image on a monitoring screen is a claim about a person that is not true.
 */

type Subject = { id: number; name: string };

/** Stable per-member accent, matching the avatars in the member filter. */
const AVATAR_COLORS = [
  'bg-blue-500',
  'bg-rose-500',
  'bg-emerald-500',
  'bg-amber-500',
  'bg-purple-500',
  'bg-cyan-500',
];

const initialsOf = (name: string) =>
  name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join('') || '?';

/**
 * Every capture of one member's day, in the order they were taken.
 *
 * This is what the lightbox walks. It is built from the same windows the cards
 * render, so each screenshot keeps the window it belongs to and the caption
 * cannot drift from the picture.
 */
const itemsOfDay = (day: ScreenshotDay, subjectName: string): LightboxItem[] =>
  [...day.windows]
    .sort((a, b) => a.window_start.localeCompare(b.window_start))
    .flatMap((window) =>
      window.screenshots.map((shot) => ({ shot, window, subjectName })),
    );

/**
 * Narrow a day's response to the selected projects.
 *
 * Project is per-screenshot (`ScreenshotView.project_id`, resolved
 * server-side from the entry it was captured against), not per-member or
 * per-window, so this reaches all the way into `windows[].screenshots`
 * rather than filtering whole members the way the member picker does.
 * `tracked_seconds` at every level is left untouched — it describes time
 * actually worked, independent of which screenshots are shown.
 */
const filterByProjects = (
  members: ScreenshotMemberDays[],
  selectedProjects: string[],
): ScreenshotMemberDays[] => {
  if (selectedProjects.length === 0) return members;
  const wanted = new Set(selectedProjects);
  return members
    .map((member) => {
      const days = member.days
        .map((day) => {
          const windows = day.windows
            .map((window) => {
              const screenshots = window.screenshots.filter(
                (shot) => shot.project_id !== null && wanted.has(String(shot.project_id)),
              );
              return { ...window, screenshots, screenshot_count: screenshots.length };
            })
            .filter((window) => window.screenshot_count > 0);
          const screenshot_count = windows.reduce((sum, w) => sum + w.screenshot_count, 0);
          return { ...day, windows, screenshot_count };
        })
        .filter((day) => day.screenshot_count > 0);
      const screenshot_count = days.reduce((sum, d) => sum + d.screenshot_count, 0);
      return { ...member, days, screenshot_count };
    })
    .filter((member) => member.screenshot_count > 0);
};

const DaySection: React.FC<{
  day: ScreenshotDay;
  subject: Subject;
  onOpen: (items: LightboxItem[], index: number) => void;
}> = ({ day, subject, onOpen }) => {
  const items = useMemo(() => itemsOfDay(day, subject.name), [day, subject.name]);
  const hours = useMemo(() => groupWindowsByHour(day.windows), [day.windows]);

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-center gap-3">
        <h4 className="text-[13px] font-bold text-[#0F172A]">
          {formatISTDate(`${day.date}T12:00:00Z`)}
        </h4>
        <span className="text-[11px] font-semibold text-[#94A3B8]">
          {day.screenshot_count} capture{day.screenshot_count === 1 ? '' : 's'} ·{' '}
          {formatHMS(day.tracked_seconds)} worked
        </span>
      </div>

      {hours.map((block) => (
        <HourRow
          key={block.key}
          block={block}
          subjectName={subject.name}
          onOpen={(shot) =>
            onOpen(
              items,
              Math.max(
                0,
                items.findIndex((item) => item.shot.id === shot.id),
              ),
            )
          }
        />
      ))}
    </div>
  );
};

/**
 * One employee, collapsed to a summary row until opened.
 *
 * Collapsed by default when there is more than one: an admin looking at a whole
 * team wants the roster first and the pictures second. Collapsed sections also
 * render none of their images, so opening the page does not fetch hundreds of
 * screenshots nobody has asked to look at yet.
 */
const MemberAccordion: React.FC<{
  member: ScreenshotMemberDays;
  defaultOpen: boolean;
  onOpen: (items: LightboxItem[], index: number) => void;
}> = ({ member, defaultOpen, onOpen }) => {
  const [open, setOpen] = useState(defaultOpen);
  const subject: Subject = { id: member.user_id, name: member.user_name };
  const color = AVATAR_COLORS[member.user_id % AVATAR_COLORS.length];

  return (
    <section className="overflow-hidden rounded-xl border border-[#E2E8F0] bg-white shadow-sm">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-5 py-4 text-left transition hover:bg-[#F8FAFC]"
      >
        <span
          className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-[12px] font-bold text-white ${color}`}
        >
          {initialsOf(member.user_name)}
        </span>

        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="truncate text-[14px] font-bold text-[#0F172A]">
              {member.user_name}
            </span>
            {/* The employee's tracked time, where their internal id used to be.
                An id says nothing to the person reading this screen; the hours
                they worked is the number being looked for. */}
            <span className="rounded-full bg-[#F1F5F9] px-2 py-0.5 text-[10px] font-bold text-[#334155]">
              {formatHMS(member.tracked_seconds)} worked
            </span>
          </span>
          <span className="mt-0.5 block text-[11px] font-medium text-[#94A3B8]">
            {member.screenshot_count} capture{member.screenshot_count === 1 ? '' : 's'}
          </span>
        </span>

        <svg
          className={
            'h-4 w-4 shrink-0 text-[#94A3B8] transition-transform duration-200 ' +
            (open ? 'rotate-180' : '')
          }
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="space-y-10 border-t border-[#F1F5F9] px-5 py-6">
          {member.days.map((day) => (
            <DaySection key={day.date} day={day} subject={subject} onOpen={onOpen} />
          ))}
        </div>
      )}
    </section>
  );
};

export const AdminScreenshots: React.FC = () => {
  const { currentUser } = useAuth();
  const seesEveryone = canViewAllScreenshots(currentUser);
  /**
   * Admin and HR only, read from the permission the backend actually issued.
   * A leader viewing this page, and an employee viewing their own, get no
   * delete control — and the endpoint refuses them anyway.
   */
  const mayDelete = canDeleteScreenshots(currentUser);
  const { showToast, confirmAction } = useFeedback();

  /** Opens on today, which is the day someone monitoring a team is looking at. */
  const [day, setDay] = useState<string>(istTodayIso);
  /** Empty means every employee — the same convention the dashboard uses. */
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  /** Empty means every project — same convention. */
  const [selectedProjects, setSelectedProjects] = useState<string[]>([]);
  const [viewer, setViewer] = useState<{ items: LightboxItem[]; index: number } | null>(null);

  /**
   * `getAllMembers` rather than `getMembers`: the directory endpoint caps
   * `limit` at 100, so asking for more comes back 422 and the picker renders
   * empty. This one pages through and returns the whole roster.
   */
  const { data: members = [] } = useGetAllMembersQuery(undefined, { skip: !seesEveryone });
  const { data: projects = [] } = useGetAllProjectsQuery(undefined, { skip: !seesEveryone });

  const [deleteScreenshot, { isLoading: isDeleting }] = useDeleteScreenshotMutation();

  const { data, isLoading, isFetching, isError } = useGetScreenshotDayQuery({
    from: day,
    to: day,
    // A leader is pinned to themselves. Their organisation-side scope is their
    // whole team, so leaving this off would hand them their team's captures.
    ...(seesEveryone ? {} : { user_id: currentUser?.id }),
  });

  /**
   * The member filter narrows what is already loaded rather than re-querying:
   * the response covers everyone the caller may see, so selecting three people
   * is a filter, not a new round trip.
   */
  const shown = useMemo(() => {
    const all = data?.members ?? [];
    const byMember =
      selectedMembers.length === 0
        ? all
        : (() => {
            const wanted = new Set(selectedMembers);
            return all.filter((member) => wanted.has(String(member.user_id)));
          })();
    return filterByProjects(byMember, selectedProjects);
  }, [data, selectedMembers, selectedProjects]);

  const totalShots = shown.reduce((sum, member) => sum + member.screenshot_count, 0);

  /**
   * Delete the capture the viewer is looking at.
   *
   * Confirmed first, because this destroys the image itself and there is no
   * undo. On success the deleted item is dropped from the open viewer and the
   * day is re-read through the invalidated tag, so the grid behind agrees with
   * what the server still holds; deleting the last one closes the viewer
   * rather than leaving it on a caption with no picture. On failure nothing is
   * removed — the message says the deletion did not happen, because a card
   * that vanishes on a failed request is a lie about the data.
   */
  const handleDelete = async (shotId: number) => {
    const confirmed = await confirmAction(
      'Delete this screenshot?',
      'The image and its record are permanently removed. This cannot be undone.',
    );
    if (!confirmed) return;

    try {
      await deleteScreenshot(shotId).unwrap();
      setViewer((current) => {
        if (!current) return current;
        const items = current.items.filter((item) => item.shot.id !== shotId);
        if (items.length === 0) return null;
        return { items, index: Math.min(current.index, items.length - 1) };
      });
      showToast('Screenshot deleted successfully.', 'success');
    } catch (err) {
      console.error('Failed to delete screenshot', err);
      const status = (err as { status?: number } | null)?.status;
      showToast(
        status === 403
          ? 'You do not have permission to delete screenshots.'
          : status === 404
            ? 'That screenshot no longer exists.'
            : 'Unable to delete this screenshot. Please try again.',
        'error',
      );
    }
  };

  return (
    <V2Shell
      title="Screenshots"
      subtitle={
        seesEveryone
          ? 'Screens captured on employees’ machines while they were tracking time.'
          : 'Screens captured on your machine while you were tracking time.'
      }
      actions={<InlineRefreshIndicator active={isFetching && !isLoading} />}
    >
      <div className="w-full space-y-6 pb-20">
        {/* Filters — one day at a time, plus the dashboard's own member picker. */}
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-[#E2E8F0] bg-white p-4 shadow-sm">
          <DayFilter value={day} onChange={setDay} />

          {seesEveryone ? (
            <>
              <MemberMultiSelect
                members={members}
                selected={selectedMembers}
                onChange={setSelectedMembers}
              />
              <ProjectMultiSelect
                projects={projects}
                selected={selectedProjects}
                onChange={setSelectedProjects}
              />
            </>
          ) : (
            <span className="text-[13px] font-semibold text-[#64748B]">
              Showing your own screenshots
            </span>
          )}

          <span className="ml-auto text-[12px] font-semibold text-[#64748B]">
            {totalShots} capture{totalShots === 1 ? '' : 's'}
            {seesEveryone &&
              shown.length > 0 &&
              ` from ${shown.length} employee${shown.length === 1 ? '' : 's'}`}
          </span>
        </div>

        {isError && (
          <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm font-semibold text-rose-700">
            These screenshots could not be loaded. Please try again.
          </div>
        )}

        {isLoading ? (
          <div className="rounded-xl border border-[#E2E8F0] bg-white p-6 shadow-sm">
            <p className="py-10 text-center text-sm font-medium text-[#64748B]">
              Loading screenshots…
            </p>
          </div>
        ) : shown.length === 0 ? (
          <div className="rounded-xl border border-[#E2E8F0] bg-white p-6 shadow-sm">
            <div className="py-10 text-center">
              <p className="text-sm font-bold text-[#475569]">
                {(selectedMembers.length > 0 || selectedProjects.length > 0) &&
                (data?.members?.length ?? 0) > 0
                  ? 'No screenshots match the selected filters on this day.'
                  : 'No screenshots were captured on this day.'}
              </p>
              <p className="mt-1 text-xs font-medium text-[#94A3B8]">
                Captures appear here once the desktop client records and uploads them for the
                selected date.
              </p>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            {shown.map((member) => (
              <MemberAccordion
                key={member.user_id}
                member={member}
                // A single employee — a leader, or a filtered-down list — has
                // nothing to scan, so it opens straight onto the captures.
                defaultOpen={shown.length === 1}
                onOpen={(items, index) => setViewer({ items, index })}
              />
            ))}
          </div>
        )}
      </div>

      {viewer && (
        <ScreenshotLightbox
          items={viewer.items}
          index={viewer.index}
          onIndexChange={(index) => setViewer((current) => (current ? { ...current, index } : current))}
          onClose={() => setViewer(null)}
          onDelete={mayDelete ? (shot) => void handleDelete(shot.id) : undefined}
          deleting={isDeleting}
        />
      )}
    </V2Shell>
  );
};