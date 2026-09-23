import React, { useMemo, useState } from 'react';
import { ClientShell } from './ClientShell';
import { ClientProjectFilter } from './ClientFilters';
import { Card, EmptyState, ErrorNote } from '../member/MemberUi';
import { DayFilter, istTodayIso } from '../screenshots/DayFilter';
import { groupWindowsByHour } from '../screenshots/hours';
import { HourRow } from '../screenshots/HourRow';
import { ScreenshotLightbox } from '../screenshots/ScreenshotLightbox';
import type { LightboxItem } from '../screenshots/ScreenshotLightbox';
import type { ScreenshotDay, ScreenshotMemberDays } from '../../store/api/screenshotsApi';
import { useGetMyProjectsQuery, useGetMyScreenshotsGridQuery } from '../../store/api/clientPortalApi';
import { ENDPOINTS } from '../../api/endpoints';
import { formatHMS, formatISTDate } from '../../utils/duration';

/**
 * The client portal's own Screenshots page.
 *
 * Its own nav item and route rather than something folded into a project's
 * detail page: a client with several shared projects wants one place that
 * shows everyone's captures for a day, the same way `AdminScreenshots` is one
 * place for the whole team, not a tab bolted onto each project. It reuses the
 * exact same day-grouped, member-accordion building blocks that page is built
 * from (`DayFilter`, `groupWindowsByHour`, `HourRow`, `ScreenshotLightbox`),
 * so the design matches exactly. The only two differences from the admin
 * screen are read-only (no delete control, this client did not capture these
 * images) and scoped: the grid covers only the projects shared with this
 * client, and every screenshot streams through this portal's own view route
 * rather than the staff one, which a client cannot call.
 */

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

const itemsOfDay = (day: ScreenshotDay, subjectName: string): LightboxItem[] =>
  [...day.windows]
    .sort((a, b) => a.window_start.localeCompare(b.window_start))
    .flatMap((window) => window.screenshots.map((shot) => ({ shot, window, subjectName })));

const viewUrl = (id: number) => ENDPOINTS.CLIENTS.MY_SCREENSHOT_VIEW(id);

const DaySection: React.FC<{
  day: ScreenshotDay;
  subjectName: string;
  onOpen: (items: LightboxItem[], index: number) => void;
}> = ({ day, subjectName, onOpen }) => {
  const items = useMemo(() => itemsOfDay(day, subjectName), [day, subjectName]);
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
          subjectName={subjectName}
          viewUrl={viewUrl}
          onOpen={(shot) =>
            onOpen(items, Math.max(0, items.findIndex((item) => item.shot.id === shot.id)))
          }
        />
      ))}
    </div>
  );
};

const MemberAccordion: React.FC<{
  member: ScreenshotMemberDays;
  defaultOpen: boolean;
  onOpen: (items: LightboxItem[], index: number) => void;
}> = ({ member, defaultOpen, onOpen }) => {
  const [open, setOpen] = useState(defaultOpen);
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
            <span className="truncate text-[14px] font-bold text-[#0F172A]">{member.user_name}</span>
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
            <DaySection key={day.date} day={day} subjectName={member.user_name} onOpen={onOpen} />
          ))}
        </div>
      )}
    </section>
  );
};

export const ClientScreenshots: React.FC = () => {
  const [day, setDay] = useState<string>(istTodayIso);
  const [selectedProjectIds, setSelectedProjectIds] = useState<string[]>([]);
  const [viewer, setViewer] = useState<{ items: LightboxItem[]; index: number } | null>(null);

  const { data: projectData } = useGetMyProjectsQuery({ start_date: day, end_date: day });
  const allProjects = projectData?.items ?? [];
  const projectIds = selectedProjectIds.map(Number);

  const { data, isLoading, isFetching, isError } = useGetMyScreenshotsGridQuery({
    start_date: day,
    end_date: day,
    project_ids: projectIds,
  });

  const permissions = data?.permissions;
  const screenshotsShared = permissions?.share_screenshots ?? true;
  const members = data?.members ?? [];
  const totalShots = members.reduce((sum, member) => sum + member.screenshot_count, 0);

  return (
    <ClientShell
      title="Screenshots"
      subtitle="Screens captured while tracking time on your shared projects."
    >
      <div className="w-full space-y-6 pb-20">
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-[#E2E8F0] bg-white p-4 shadow-sm">
          <DayFilter value={day} onChange={setDay} />
          <ClientProjectFilter
            projects={allProjects}
            selected={selectedProjectIds}
            onChange={setSelectedProjectIds}
          />
          <span className="ml-auto text-[12px] font-semibold text-[#64748B]">
            {totalShots} capture{totalShots === 1 ? '' : 's'}
            {members.length > 0 && ` from ${members.length} member${members.length === 1 ? '' : 's'}`}
          </span>
        </div>

        {isError && <ErrorNote message="These screenshots could not be loaded. Please try again." />}

        {!screenshotsShared && !isLoading ? (
          <Card title="Screenshots">
            <EmptyState
              message="Screenshots are not shared for your account."
              hint="Ask your admin to enable it if you need this."
            />
          </Card>
        ) : isLoading ? (
          <div className="rounded-xl border border-[#E2E8F0] bg-white p-6 shadow-sm">
            <p className="py-10 text-center text-sm font-medium text-[#64748B]">
              Loading screenshots…
            </p>
          </div>
        ) : members.length === 0 ? (
          <div className={`rounded-xl border border-[#E2E8F0] bg-white p-6 shadow-sm transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
            <div className="py-10 text-center">
              <p className="text-sm font-bold text-[#475569]">
                {selectedProjectIds.length > 0
                  ? 'No screenshots match the selected project on this day.'
                  : 'No screenshots were captured on this day.'}
              </p>
              <p className="mt-1 text-xs font-medium text-[#94A3B8]">
                Captures appear here once time is tracked against one of your shared projects.
              </p>
            </div>
          </div>
        ) : (
          <div className={`space-y-3 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
            {members.map((member) => (
              <MemberAccordion
                key={member.user_id}
                member={member}
                defaultOpen={members.length === 1}
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
          viewUrl={viewUrl}
        />
      )}
    </ClientShell>
  );
};
