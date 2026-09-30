import React, { useMemo, useState } from 'react';
import { V2Shell } from '../dashboard/v2/V2Shell';
import { useAuth } from '../auth/authContext';
import {
  canDeleteScreenshots,
  canViewOthersScreenshots,
  canViewTeamScreenshots,
} from '../auth/roles';
import { useGetAllMembersQuery } from '../../store/api/membersApi';
import { useGetAllProjectsQuery } from '../../store/api/projectsApi';
import {
  useDeleteScreenshotMutation,
  useGetScreenshotDayQuery,
} from '../../store/api/screenshotsApi';
import type { ScreenshotDay, ScreenshotMemberDays, ScreenshotView } from '../../store/api/screenshotsApi';
import { MemberMultiSelect, ProjectMultiSelect } from '../dashboard/v2/filters';
import { DayFilter, istTodayIso } from '../screenshots/DayFilter';
import { groupWindowsByHour } from '../screenshots/hours';
import { HourRow } from '../screenshots/HourRow';
import { ScreenshotLightbox } from '../screenshots/ScreenshotLightbox';
import { ScreenshotMessageDrawer } from '../screenshots/ScreenshotMessageDrawer';
import type { LightboxItem } from '../screenshots/ScreenshotLightbox';
import { InlineRefreshIndicator } from '../../components/InlineRefreshIndicator';
import { useFeedback } from '../../components/FeedbackProvider';
import { formatHMS, formatISTDate } from '../../utils/duration';

/**
 * Screenshots, for the people allowed to see someone else's.
 *
 * Two views, chosen with the **Employees / Own** switch:
 *
 * - **Employees** — everyone the caller may see *other than themselves*, from
 *   one request to `/time-entry-screenshots/day` for the selected day. Who
 *   that is belongs to the backend: every member for Admin and HR, the people
 *   on the projects they lead for a leader (`visible_member_ids`). This page
 *   sends no list of people, so it cannot widen that. Each employee is an
 *   accordion section headed by their name and the time they worked, so a
 *   long roster is a list you scan rather than a page you scroll, and the
 *   member and project pickers narrow what came back.
 * - **Own** — the caller's own captures, with the request pinned to their
 *   `user_id`. No pickers: there is one person and nothing to narrow.
 *
 * Someone whose scope does not reach past themselves (a manager, say) gets no
 * switch and only ever the Own view.
 *
 * Seeing is not deleting. The lightbox's Delete is offered only to a caller
 * holding `screenshots:delete` — Admin and HR. A leader reviews their team's
 * captures and cannot remove one, here or through the endpoint.
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

/** Whose captures the page is showing. */
type ScreenshotScope = 'employees' | 'own';

/**
 * The Employees / Own switch.
 *
 * A view filter over what the caller is already entitled to, not a permission
 * boundary: Employees is whatever the endpoint returns for them minus their
 * own row, and Own is their own row.
 */
const ScopeSwitch: React.FC<{
  value: ScreenshotScope;
  onChange: (value: ScreenshotScope) => void;
}> = ({ value, onChange }) => {
  const options: { id: ScreenshotScope; label: string }[] = [
    { id: 'employees', label: 'Employees' },
    { id: 'own', label: 'Own' },
  ];
  return (
    <div
      className="inline-flex shrink-0 items-center gap-1 rounded-lg bg-[#F1F5F9] p-1"
      role="tablist"
      aria-label="Whose screenshots"
    >
      {options.map((option) => {
        const active = value === option.id;
        return (
          <button
            key={option.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(option.id)}
            className={
              'rounded-md px-3 py-1.5 text-[12px] font-bold transition ' +
              (active ? 'bg-white text-[#0F172A] shadow-sm' : 'text-[#64748B] hover:text-[#334155]')
            }
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
};

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
  onMessage?: (subjectName: string, shot: ScreenshotView) => void;
}> = ({ day, subject, onOpen, onMessage }) => {
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
          onMessage={onMessage ? (shot) => onMessage(subject.name, shot) : undefined}
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
  onMessage?: (subjectName: string, shot: ScreenshotView) => void;
}> = ({ member, defaultOpen, onOpen, onMessage }) => {
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
            <DaySection key={day.date} day={day} subject={subject} onOpen={onOpen} onMessage={onMessage} />
          ))}
        </div>
      )}
    </section>
  );
};

export const AdminScreenshots: React.FC = () => {
  const { currentUser } = useAuth();
  /** Admin, HR and leaders: the callers whose scope reaches past themselves. */
  const seesOthers = canViewOthersScreenshots(currentUser);
  /** A leader's "employees" are their own team, which changes only the wording. */
  const teamOnly = canViewTeamScreenshots(currentUser);
  /**
   * Admin and HR only, read from the permission the backend actually issued.
   * A leader viewing their team's captures, and anyone viewing their own, get
   * no delete control — and the endpoint refuses them anyway.
   */
  const mayDelete = canDeleteScreenshots(currentUser);
  /** Opens on the employees, which is what someone reviewing a team came for. */
  const [scope, setScope] = useState<ScreenshotScope>('employees');
  const showingOwn = !seesOthers || scope === 'own';
  const { showToast, confirmAction } = useFeedback();

  /** Opens on today, which is the day someone monitoring a team is looking at. */
  const [day, setDay] = useState<string>(istTodayIso);
  /** Empty means every employee — the same convention the dashboard uses. */
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  /** Empty means every project — same convention. */
  const [selectedProjects, setSelectedProjects] = useState<string[]>([]);
  const [viewer, setViewer] = useState<{ items: LightboxItem[]; index: number } | null>(null);
  /** The screenshot whose (not yet built) message panel is open. */
  const [messageFor, setMessageFor] = useState<{ subjectName: string; shot: ScreenshotView } | null>(null);

  /**
   * `getAllMembers` rather than `getMembers`: the directory endpoint caps
   * `limit` at 100, so asking for more comes back 422 and the picker renders
   * empty. This one pages through and returns the whole roster.
   */
  const { data: members = [] } = useGetAllMembersQuery(undefined, { skip: !seesOthers });
  const { data: projects = [] } = useGetAllProjectsQuery(undefined, { skip: !seesOthers });
  /** The picker offers the people the Employees view can show: not the caller. */
  const pickableMembers = useMemo(
    () => members.filter((member) => member.id !== currentUser?.id),
    [members, currentUser?.id],
  );

  const [deleteScreenshot, { isLoading: isDeleting }] = useDeleteScreenshotMutation();

  const { data, isLoading, isFetching, isError } = useGetScreenshotDayQuery({
    from: day,
    to: day,
    // Own pins the request to the caller. Employees sends no id at all: the
    // endpoint answers with everyone in the caller's own scope — the
    // organization for Admin and HR, their team for a leader.
    ...(showingOwn ? { user_id: currentUser?.id } : {}),
  });

  /**
   * The member filter narrows what is already loaded rather than re-querying:
   * the response covers everyone the caller may see, so selecting three people
   * is a filter, not a new round trip.
   */
  const shown = useMemo(() => {
    const all = data?.members ?? [];
    // Own has no pickers, so nothing chosen in the other view applies to it.
    if (showingOwn) return all;
    // The caller's own row belongs to the Own view; a leader is a member of
    // their own team server-side, so without this they would head their own
    // employee list.
    const others = all.filter((member) => member.user_id !== currentUser?.id);
    const byMember =
      selectedMembers.length === 0
        ? others
        : (() => {
            const wanted = new Set(selectedMembers);
            return others.filter((member) => wanted.has(String(member.user_id)));
          })();
    return filterByProjects(byMember, selectedProjects);
  }, [data, showingOwn, currentUser?.id, selectedMembers, selectedProjects]);
  const filtersApplied = !showingOwn && (selectedMembers.length > 0 || selectedProjects.length > 0);

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
        showingOwn
          ? 'Screens captured on your machine while you were tracking time.'
          : teamOnly
            ? 'Screens captured on your team’s machines while they were tracking time.'
            : 'Screens captured on employees’ machines while they were tracking time.'
      }
      actions={<InlineRefreshIndicator active={isFetching && !isLoading} />}
    >
      <div className="w-full space-y-6 pb-20">
        {/* Filters — one day at a time, plus the dashboard's own member picker. */}
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-[#E2E8F0] bg-white p-4 shadow-sm">
          {seesOthers && <ScopeSwitch value={scope} onChange={setScope} />}
          <DayFilter value={day} onChange={setDay} />

          {!showingOwn ? (
            <>
              <MemberMultiSelect
                members={pickableMembers}
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
            {!showingOwn &&
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
                {filtersApplied && (data?.members?.length ?? 0) > 0
                  ? 'No screenshots match the selected filters on this day.'
                  : showingOwn
                    ? 'You have no screenshots on this day.'
                    : teamOnly
                      ? 'Your team captured no screenshots on this day.'
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
                // A single person — the Own view, or a filtered-down list —
                // has nothing to scan, so it opens straight onto the captures.
                defaultOpen={shown.length === 1}
                onOpen={(items, index) => setViewer({ items, index })}
                // Admin, HR and Leader reviewing other people's captures --
                // the Employees view. The Own view has nobody to message.
                onMessage={
                  showingOwn ? undefined : (subjectName, shot) => setMessageFor({ subjectName, shot })
                }
              />
            ))}
          </div>
        )}
      </div>

      <ScreenshotMessageDrawer
        open={messageFor !== null}
        onClose={() => setMessageFor(null)}
        subjectName={messageFor?.subjectName}
        shot={messageFor?.shot}
      />

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