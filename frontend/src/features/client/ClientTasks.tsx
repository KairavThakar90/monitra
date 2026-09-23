import React, { useState } from 'react';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { ClientMemberFilter, ClientProjectFilter } from './ClientFilters';
import { Card, EmptyState, ErrorNote } from '../member/MemberUi';
import { RankedBars } from '../dashboard/v2/charts';
import { series } from '../dashboard/v2/theme';
import { DateRangeFilter } from '../dashboard/v2/filters';
import { useGetMyMemberHoursQuery, useGetMyProjectsQuery, useGetMyTaskHoursQuery } from '../../store/api/clientPortalApi';
import { formatHMS } from '../../utils/duration';
import { CLIENT_DEFAULT_RANGE, longDate } from './clientRange';

/** Tracked hours per task, across every shared project (or a filtered
 * subset of projects/members), over a date range — the client-portal
 * equivalent of the member/admin reports' Top Tasks. */
export const ClientTasks: React.FC = () => {
  const [range, setRange] = useState(CLIENT_DEFAULT_RANGE);
  const [selectedProjectIds, setSelectedProjectIds] = useState<string[]>([]);
  const [selectedMemberIds, setSelectedMemberIds] = useState<string[]>([]);
  const dateArgs = { start_date: range.from, end_date: range.to };
  const projectIds = selectedProjectIds.map(Number);
  const memberIds = selectedMemberIds.map(Number);

  const { data: projectData } = useGetMyProjectsQuery(dateArgs);
  const { data: memberData } = useGetMyMemberHoursQuery({ ...dateArgs, project_ids: projectIds });
  const { data, isFetching, isError } = useGetMyTaskHoursQuery({ ...dateArgs, project_ids: projectIds, member_ids: memberIds });

  const allProjects = projectData?.items ?? [];
  const allMembers = memberData?.items ?? [];
  const tasks = data?.items ?? [];
  const totalSeconds = tasks.reduce((sum, t) => sum + t.total_tracked_seconds, 0);

  return (
    <ClientShell
      title="Tasks"
      subtitle={`Tasks worked on across your shared projects, ${longDate(range.from)} – ${longDate(range.to)}`}
    >
      <div className="w-full space-y-6 pb-20">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#E2E8F0] bg-white p-2 pl-4 shadow-sm">
          <div className="flex flex-wrap items-center gap-2">
            <DateRangeFilter value={range} onChange={setRange} />
            <ClientProjectFilter projects={allProjects} selected={selectedProjectIds} onChange={setSelectedProjectIds} />
            <ClientMemberFilter members={allMembers} selected={selectedMemberIds} onChange={setSelectedMemberIds} />
          </div>
          <button
            onClick={() => {
              setRange(CLIENT_DEFAULT_RANGE);
              setSelectedProjectIds([]);
              setSelectedMemberIds([]);
            }}
            className="rounded-lg border border-[#E2E8F0] px-4 py-2 text-[13px] font-bold text-[#64748B] transition hover:bg-[#F8FAFC] hover:text-[#0F172A]"
          >
            Reset
          </button>
        </div>

        {isError && <ErrorNote message="Tasks could not be loaded. Please try again." />}

        <div className={`space-y-6 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <ClientKpiCard title="Total Task Hours" value={formatHMS(totalSeconds)} />
            <ClientKpiCard title="Tasks Worked" value={tasks.length} />
          </div>

          <Card title="Top Tasks">
            {tasks.length === 0 ? (
              <EmptyState message="No task activity for this range or filter." hint="Try a different date range, or clear the filters." />
            ) : (
              <RankedBars
                items={tasks.map((task) => ({
                  id: String(task.id),
                  name: task.task_name,
                  value: task.total_tracked_hours,
                  meta: task.project_name ?? 'Unknown project',
                }))}
                color={series[3]}
                formatValue={(n) => `${n}h`}
              />
            )}
          </Card>
        </div>
      </div>
    </ClientShell>
  );
};
