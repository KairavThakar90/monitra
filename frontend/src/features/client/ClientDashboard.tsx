import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { ClientProjectFilter } from './ClientFilters';
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
  const [selectedProjectIds, setSelectedProjectIds] = useState<string[]>([]);
  const dateArgs = { start_date: range.from, end_date: range.to };
  const projectIds = selectedProjectIds.map(Number);

  // Unfiltered, so the Project filter always offers every project shared
  // with this client, whatever is currently selected.
  const { data, isFetching, isError } = useGetMyProjectsQuery(dateArgs);
  const { data: memberData } = useGetMyMemberHoursQuery({ ...dateArgs, project_ids: projectIds });
  const { data: taskData } = useGetMyTaskHoursQuery({ ...dateArgs, project_ids: projectIds });

  const allProjects = data?.items ?? [];
  const visibleProjects = selectedProjectIds.length === 0
    ? allProjects
    : allProjects.filter((p) => selectedProjectIds.includes(String(p.id)));
  const totalSeconds = visibleProjects.reduce((sum, p) => sum + p.total_tracked_seconds, 0);

  return (
    <ClientShell
      title="Your Projects"
      subtitle={`Projects shared with you, ${longDate(range.from)} – ${longDate(range.to)}`}
    >
      <div className="w-full space-y-6 pb-20">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#E2E8F0] bg-white p-2 pl-4 shadow-sm">
          <div className="flex flex-wrap items-center gap-2">
            <DateRangeFilter value={range} onChange={setRange} />
            <ClientProjectFilter projects={allProjects} selected={selectedProjectIds} onChange={setSelectedProjectIds} />
          </div>
          <button
            onClick={() => {
              setRange(CLIENT_DEFAULT_RANGE);
              setSelectedProjectIds([]);
            }}
            className="rounded-lg border border-[#E2E8F0] px-4 py-2 text-[13px] font-bold text-[#64748B] transition hover:bg-[#F8FAFC] hover:text-[#0F172A]"
          >
            Reset
          </button>
        </div>

        {isError && <ErrorNote message="Your projects could not be loaded. Please try again." />}

        <div className={`space-y-6 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <ClientKpiCard title="Total Hours" value={formatHMS(totalSeconds)} />
            <ClientKpiCard title="Projects Shown" value={visibleProjects.length} />
            <ClientKpiCard title="Team Members" value={memberData?.items.length ?? 0} />
            <ClientKpiCard title="Tasks Worked" value={taskData?.items.length ?? 0} />
          </div>

          <Card title="Project Activity">
            {visibleProjects.length === 0 ? (
              <EmptyState
                message={
                  allProjects.length === 0
                    ? 'No projects have been shared with you yet.'
                    : 'No projects match the current filter.'
                }
                hint={
                  allProjects.length === 0
                    ? 'Once your Monitra contact shares a project, it will appear here.'
                    : 'Clear the project filter to see everything shared with you.'
                }
              />
            ) : (
              <RankedBars
                items={visibleProjects
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

          {visibleProjects.length > 0 && (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {visibleProjects.map((project) => (
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
