import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { ClientProjectFilter } from './ClientFilters';
import { ClientExportButton } from './ClientExportButton';
import { ClientExportDialog } from './ClientExportDialog';
import { Card, EmptyState, ErrorNote } from '../member/MemberUi';
import { ClientAvatarStack, ClientTable } from './ClientTable';
import { DateRangeFilter } from '../dashboard/v2/filters';
import { useGetMyProjectsQuery } from '../../store/api/clientPortalApi';
import { CLIENT_DEFAULT_RANGE, longDate } from './clientRange';

/**
 * The client portal's landing page: which projects are shared with this
 * client, and a way into each one — deliberately *only* that. Hours live on
 * Timing, people on Members, tasks on Tasks, budgets on Billing: each
 * section has its own page, so this one does not repeat their numbers.
 */
export const ClientDashboard: React.FC = () => {
  const navigate = useNavigate();
  const [range, setRange] = useState(CLIENT_DEFAULT_RANGE);
  const [selectedProjectIds, setSelectedProjectIds] = useState<string[]>([]);
  const [exportOpen, setExportOpen] = useState(false);

  const { data, isFetching, isError } = useGetMyProjectsQuery({ start_date: range.from, end_date: range.to });

  const allProjects = data?.items ?? [];
  const visibleProjects = selectedProjectIds.length === 0
    ? allProjects
    : allProjects.filter((p) => selectedProjectIds.includes(String(p.id)));

  return (
    <ClientShell
      title="Your Projects"
      subtitle={`Projects shared with you, ${longDate(range.from)} – ${longDate(range.to)}`}
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

        {isError && <ErrorNote message="Your projects could not be loaded. Please try again." />}

        <div className={`space-y-6 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <ClientKpiCard title="Projects Shared" value={allProjects.length} />
            <ClientKpiCard title="Projects Shown" value={visibleProjects.length} />
          </div>

          {visibleProjects.length === 0 ? (
            <Card>
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
            </Card>
          ) : (
            <ClientTable
              headers={[
                { label: 'Project Name' },
                { label: 'Description' },
                { label: 'Team' },
                { label: 'Status' },
                { label: 'Deadline' },
                { label: '', align: 'right' },
              ]}
            >
              {visibleProjects.map((project) => (
                <tr
                  key={project.id}
                  onClick={() => navigate(`/client/projects/${project.id}?start=${range.from}&end=${range.to}`)}
                  className="cursor-pointer transition hover:bg-[#F8FAFC]"
                >
                  <td className="px-4 py-3 font-medium text-[#0F172A]">{project.project_name}</td>
                  <td className="max-w-md px-4 py-3 text-[#475569]">
                    <span className="line-clamp-1">{project.description || '—'}</span>
                  </td>
                  <td className="px-4 py-3">
                    {/* The assigned roster as the admin table's avatar stack —
                        empty (not zero people) when the admin has not shared
                        Member Details. */}
                    <ClientAvatarStack members={project.members ?? []} />
                  </td>
                  <td className="px-4 py-3 capitalize text-[#475569]">{project.status}</td>
                  <td className="px-4 py-3 text-[#475569]">
                    {project.deadline ? longDate(project.deadline) : '—'}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <span className="text-sm font-semibold text-[#2563EB]">View</span>
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
