import React, { useState } from 'react';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { ClientMemberFilter, ClientProjectFilter } from './ClientFilters';
import { ClientExportButton } from './ClientExportButton';
import { ClientExportDialog } from './ClientExportDialog';
import { Card, EmptyState, ErrorNote } from '../member/MemberUi';
import { ClientTable } from './ClientTable';
import { DateRangeFilter } from '../dashboard/v2/filters';
import { useGetMyMemberHoursQuery, useGetMyProjectsQuery, useGetMyTaskHoursQuery } from '../../store/api/clientPortalApi';
import { CLIENT_DEFAULT_RANGE, formatSharedHMS, longDate } from './clientRange';

/** Tracked hours per task, across every shared project (or a filtered
 * subset of projects/members), over a date range — the client-portal
 * equivalent of the member/admin reports' Top Tasks. */
export const ClientTasks: React.FC = () => {
  const [range, setRange] = useState(CLIENT_DEFAULT_RANGE);
  const [selectedProjectIds, setSelectedProjectIds] = useState<string[]>([]);
  const [selectedMemberIds, setSelectedMemberIds] = useState<string[]>([]);
  const [exportOpen, setExportOpen] = useState(false);
  const dateArgs = { start_date: range.from, end_date: range.to };
  const projectIds = selectedProjectIds.map(Number);
  const memberIds = selectedMemberIds.map(Number);

  const { data: projectData } = useGetMyProjectsQuery(dateArgs);
  const { data: memberData } = useGetMyMemberHoursQuery({ ...dateArgs, project_ids: projectIds });
  const { data, isFetching, isError } = useGetMyTaskHoursQuery({ ...dateArgs, project_ids: projectIds, member_ids: memberIds });

  const allProjects = projectData?.items ?? [];
  const allMembers = memberData?.items ?? [];
  const tasks = data?.items ?? [];
  const tasksShared = data?.permissions.share_tasks ?? true;
  const timingShared = data?.permissions.share_timing ?? true;
  const totalSeconds = timingShared ? tasks.reduce((sum, t) => sum + (t.total_tracked_seconds ?? 0), 0) : null;

  return (
    <ClientShell
      title="Tasks"
      subtitle={`Tasks worked on across your shared projects, ${longDate(range.from)} – ${longDate(range.to)}`}
      actions={<ClientExportButton onClick={() => setExportOpen(true)} />}
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
            <ClientKpiCard title="Total Task Hours" value={formatSharedHMS(totalSeconds)} />
            <ClientKpiCard title="Tasks Shown" value={tasks.length} />
          </div>

          {!tasksShared ? (
            <Card title="Top Tasks">
              <EmptyState message="Tasks are not shared for your account." hint="Ask your admin to enable it if you need this." />
            </Card>
          ) : tasks.length === 0 ? (
            <Card title="Top Tasks">
              <EmptyState message="No task activity for this range or filter." hint="Try a different date range, or clear the filters." />
            </Card>
          ) : (
            <ClientTable
              headers={[
                { label: 'Task' },
                { label: 'Project' },
                { label: 'Assigned To' },
                { label: 'Status' },
                { label: 'Created' },
                { label: 'Hours', align: 'right' },
                { label: 'Tracked Time', align: 'right' },
              ]}
            >
              {tasks.map((task) => (
                <tr key={task.id}>
                  <td className="px-4 py-3 font-medium text-[#0F172A]">{task.task_name}</td>
                  <td className="px-4 py-3 text-[#475569]">{task.project_name ?? 'Unknown project'}</td>
                  <td className="px-4 py-3 text-[#475569]">{task.assignee ?? '—'}</td>
                  <td className="px-4 py-3 capitalize text-[#475569]">{task.status?.replace('_', ' ') ?? '—'}</td>
                  <td className="px-4 py-3 text-[#475569]">
                    {task.created_date ? longDate(task.created_date) : '—'}
                  </td>
                  <td className="px-4 py-3 text-right font-semibold text-[#0F172A]">
                    {task.total_tracked_hours ?? 0}h
                  </td>
                  <td className="px-4 py-3 text-right text-[#475569]">
                    {formatSharedHMS(task.total_tracked_seconds)}
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
        defaultReport="tasks"
        range={range}
        selectedProjectIds={selectedProjectIds}
        selectedMemberIds={selectedMemberIds}
        allProjects={allProjects}
        allMembers={allMembers}
      />
    </ClientShell>
  );
};
