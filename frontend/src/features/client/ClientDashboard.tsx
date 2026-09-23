import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { Card, EmptyState, ErrorNote } from '../member/MemberUi';
import { RankedBars } from '../dashboard/v2/charts';
import { series } from '../dashboard/v2/theme';
import { DateRangeFilter } from '../dashboard/v2/filters';
import { useGetMyMemberHoursQuery, useGetMyProjectsQuery, useGetMyTaskHoursQuery } from '../../store/api/clientPortalApi';
import { formatHMS } from '../../utils/duration';
import { CLIENT_DEFAULT_RANGE, longDate } from './clientRange';

export const ClientDashboard: React.FC = () => {
  const navigate = useNavigate();
  const [range, setRange] = useState(CLIENT_DEFAULT_RANGE);
  const args = { start_date: range.from, end_date: range.to };

  const { data, isFetching, isError } = useGetMyProjectsQuery(args);
  const { data: memberData } = useGetMyMemberHoursQuery(args);
  const { data: taskData } = useGetMyTaskHoursQuery(args);

  const projects = data?.items ?? [];
  const totalSeconds = projects.reduce((sum, p) => sum + p.total_tracked_seconds, 0);

  return (
    <ClientShell
      title="Your Projects"
      subtitle={`Projects shared with you, ${longDate(range.from)} – ${longDate(range.to)}`}
    >
      <div className="w-full space-y-6 pb-20">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#E2E8F0] bg-white p-2 pl-4 shadow-sm">
          <DateRangeFilter value={range} onChange={setRange} />
          <button
            onClick={() => setRange(CLIENT_DEFAULT_RANGE)}
            className="rounded-lg border border-[#E2E8F0] px-4 py-2 text-[13px] font-bold text-[#64748B] transition hover:bg-[#F8FAFC] hover:text-[#0F172A]"
          >
            Reset
          </button>
        </div>

        {isError && <ErrorNote message="Your projects could not be loaded. Please try again." />}

        <div className={`space-y-6 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <ClientKpiCard title="Total Hours" value={formatHMS(totalSeconds)} />
            <ClientKpiCard title="Projects Shared" value={projects.length} />
            <ClientKpiCard title="Team Members" value={memberData?.items.length ?? 0} />
            <ClientKpiCard title="Tasks Worked" value={taskData?.items.length ?? 0} />
          </div>

          <Card title="Project Activity">
            {projects.length === 0 ? (
              <EmptyState
                message="No projects have been shared with you yet."
                hint="Once your Monitra contact shares a project, it will appear here."
              />
            ) : (
              <RankedBars
                items={projects
                  .slice()
                  .sort((a, b) => b.total_tracked_seconds - a.total_tracked_seconds)
                  .map((project) => ({
                    id: String(project.id),
                    name: project.project_name,
                    value: project.total_tracked_hours,
                    meta: `${project.member_count} member${project.member_count === 1 ? '' : 's'}`,
                  }))}
                color={series[2]}
                formatValue={(n) => `${n}h`}
              />
            )}
          </Card>

          {projects.length > 0 && (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {projects.map((project) => (
                <button
                  key={project.id}
                  onClick={() => navigate(`/client/projects/${project.id}?start=${range.from}&end=${range.to}`)}
                  className="text-left rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm transition hover:shadow-md hover:border-[#2563EB]/40"
                >
                  <div className="text-base font-bold text-[#0F172A]">{project.project_name}</div>
                  {project.description && (
                    <p className="mt-1 line-clamp-2 text-sm text-[#64748B]">{project.description}</p>
                  )}
                  <div className="mt-4 flex items-center justify-between text-xs font-semibold text-[#94A3B8]">
                    <span>{project.member_count} member{project.member_count === 1 ? '' : 's'}</span>
                    <span>{formatHMS(project.total_tracked_seconds)}</span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </ClientShell>
  );
};
