import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { ClientMemberFilter, ClientProjectFilter } from './ClientFilters';
import { ClientExportButton } from './ClientExportButton';
import { ClientExportDialog } from './ClientExportDialog';
import { Card, EmptyState, ErrorNote } from '../member/MemberUi';
import { ClientTable } from './ClientTable';
import { DateRangeFilter } from '../dashboard/v2/filters';
import { useGetMyMemberHoursQuery, useGetMyProjectsQuery } from '../../store/api/clientPortalApi';
import { CLIENT_DEFAULT_RANGE, formatSharedHMS, longDate } from './clientRange';

/** Tracked hours per team member, across every shared project (or a
 * filtered subset of projects/members), over a date range — the
 * client-portal equivalent of the member/admin reports' own per-member
 * ranking. */
export const ClientMembers: React.FC = () => {
  const navigate = useNavigate();
  const [range, setRange] = useState(CLIENT_DEFAULT_RANGE);
  const [selectedProjectIds, setSelectedProjectIds] = useState<string[]>([]);
  const [selectedMemberIds, setSelectedMemberIds] = useState<string[]>([]);
  const [exportOpen, setExportOpen] = useState(false);
  const dateArgs = { start_date: range.from, end_date: range.to };
  const projectIds = selectedProjectIds.map(Number);

  // Unfiltered project list, so the Project filter always offers every
  // project shared with this client.
  const { data: projectData } = useGetMyProjectsQuery(dateArgs);
  const { data, isFetching, isError } = useGetMyMemberHoursQuery({ ...dateArgs, project_ids: projectIds });

  const allProjects = projectData?.items ?? [];
  const allMembers = data?.items ?? [];
  const members = selectedMemberIds.length === 0
    ? allMembers
    : allMembers.filter((m) => selectedMemberIds.includes(String(m.id)));
  const memberDetailsShared = data?.permissions.share_member_details ?? true;
  const timingShared = data?.permissions.share_timing ?? true;
  const totalSeconds = timingShared ? members.reduce((sum, m) => sum + (m.total_tracked_seconds ?? 0), 0) : null;

  return (
    <ClientShell
      title="Members"
      subtitle={`Team members working on your shared projects, ${longDate(range.from)} – ${longDate(range.to)}`}
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

        {isError && <ErrorNote message="Members could not be loaded. Please try again." />}

        <div className={`space-y-6 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <ClientKpiCard title="Total Member Hours" value={formatSharedHMS(totalSeconds)} />
            <ClientKpiCard title="Members Shown" value={members.length} />
          </div>

          {!memberDetailsShared ? (
            <Card title="Time by Member">
              <EmptyState message="Member details are not shared for your account." hint="Ask your admin to enable it if you need this." />
            </Card>
          ) : members.length === 0 ? (
            <Card title="Time by Member">
              <EmptyState message="No member activity for this range or filter." hint="Try a different date range, or clear the filters." />
            </Card>
          ) : (
            <ClientTable
              headers={[
                { label: 'Member' },
                { label: 'Designation' },
                { label: 'Projects' },
                { label: 'Hours', align: 'right' },
                { label: 'Tracked Time', align: 'right' },
                { label: '', align: 'right' },
              ]}
            >
              {members.map((member) => (
                <tr
                  key={member.id}
                  onClick={() => navigate(`/client/members/${member.id}?start=${range.from}&end=${range.to}`)}
                  className="cursor-pointer transition hover:bg-[#F8FAFC]"
                >
                  <td className="px-4 py-3 font-medium text-[#0F172A]">{member.name}</td>
                  <td className="px-4 py-3 text-[#475569]">{member.designation || '—'}</td>
                  <td className="max-w-sm px-4 py-3 text-[#475569]">
                    <span className="line-clamp-1" title={(member.project_names ?? []).join(', ')}>
                      {member.project_names?.length ? member.project_names.join(', ') : '—'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right font-semibold text-[#0F172A]">
                    {member.total_tracked_hours ?? 0}h
                  </td>
                  <td className="px-4 py-3 text-right text-[#475569]">
                    {formatSharedHMS(member.total_tracked_seconds)}
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
        defaultReport="members"
        range={range}
        selectedProjectIds={selectedProjectIds}
        selectedMemberIds={selectedMemberIds}
        allProjects={allProjects}
        allMembers={allMembers}
      />
    </ClientShell>
  );
};
