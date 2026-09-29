import React, { useState } from 'react';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { ClientProjectFilter } from './ClientFilters';
import { ClientExportButton } from './ClientExportButton';
import { ClientExportDialog } from './ClientExportDialog';
import { Card, EmptyState, ErrorNote } from '../member/MemberUi';
import { ClientTable } from './ClientTable';
import { DateRangeFilter } from '../dashboard/v2/filters';
import { useGetMyProjectsQuery } from '../../store/api/clientPortalApi';
import { CLIENT_DEFAULT_RANGE, formatSharedHMS, longDate } from './clientRange';

/** Tracked hours per shared project, over a date range — the client-portal
 * equivalent of the member's Time Tracking / Project-Wise report. */
export const ClientTiming: React.FC = () => {
  const [range, setRange] = useState(CLIENT_DEFAULT_RANGE);
  const [selectedProjectIds, setSelectedProjectIds] = useState<string[]>([]);
  const [exportOpen, setExportOpen] = useState(false);
  const { data, isFetching, isError } = useGetMyProjectsQuery({ start_date: range.from, end_date: range.to });

  const allProjects = data?.items ?? [];
  const projects = selectedProjectIds.length === 0
    ? allProjects
    : allProjects.filter((p) => selectedProjectIds.includes(String(p.id)));
  const timingShared = data?.permissions.share_timing ?? true;
  const totalSeconds = timingShared ? projects.reduce((sum, p) => sum + (p.total_tracked_seconds ?? 0), 0) : null;

  return (
    <ClientShell
      title="Timing"
      subtitle={`Tracked time by project, ${longDate(range.from)} – ${longDate(range.to)}`}
      actions={<ClientExportButton onClick={() => setExportOpen(true)} />}
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

        {isError && <ErrorNote message="Timing could not be loaded. Please try again." />}

        <div className={`space-y-6 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <ClientKpiCard title="Total Tracked" value={formatSharedHMS(totalSeconds)} />
            <ClientKpiCard title="Projects With Time" value={projects.filter((p) => (p.total_tracked_seconds ?? 0) > 0).length} />
          </div>

          {!timingShared ? (
            <Card title="Time by Project">
              <EmptyState message="Timing is not shared for your account." hint="Ask your admin to enable it if you need this." />
            </Card>
          ) : projects.length === 0 ? (
            <Card title="Time by Project">
              <EmptyState message="No tracked time for this range." hint="Try a different date range or filter." />
            </Card>
          ) : (
            <ClientTable
              headers={[
                { label: 'Project' },
                { label: 'Members Active' },
                { label: 'Hours', align: 'right' },
                { label: 'Tracked Time', align: 'right' },
              ]}
            >
              {projects
                .slice()
                .sort((a, b) => (b.total_tracked_seconds ?? 0) - (a.total_tracked_seconds ?? 0))
                .map((project) => (
                  <tr key={project.id}>
                    <td className="px-4 py-3 font-medium text-[#0F172A]">{project.project_name}</td>
                    <td className="px-4 py-3 text-[#475569]">
                      {project.member_count == null ? '—' : project.member_count}
                    </td>
                    <td className="px-4 py-3 text-right font-semibold text-[#0F172A]">
                      {project.total_tracked_hours ?? 0}h
                    </td>
                    <td className="px-4 py-3 text-right text-[#475569]">
                      {formatSharedHMS(project.total_tracked_seconds)}
                    </td>
                  </tr>
                ))}
            </ClientTable>
          )}
        </div>
      </div>

      <ClientExportDialog
        open={exportOpen}
        onClose={() => setExportOpen(false)}
        defaultReport="projects"
        range={range}
        selectedProjectIds={selectedProjectIds}
        selectedMemberIds={[]}
        allProjects={allProjects}
        allMembers={[]}
      />
    </ClientShell>
  );
};
