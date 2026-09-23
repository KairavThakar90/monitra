import React from 'react';
import { Link, useParams } from 'react-router-dom';
import { ClientShell } from './ClientShell';
import { Card, ErrorNote, Spinner } from '../member/MemberUi';
import { useGetMyProjectDetailQuery } from '../../store/api/clientPortalApi';

export const ClientProjectDetail: React.FC = () => {
  const { projectId } = useParams<{ projectId: string }>();
  const id = Number(projectId);
  const { data, isLoading, isError } = useGetMyProjectDetailQuery(id, { skip: !Number.isFinite(id) });

  const backLink = (
    <Link to="/client/dashboard" className="text-sm font-semibold text-[#2563EB] hover:text-blue-700">
      &larr; Back to your projects
    </Link>
  );

  if (isLoading) {
    return (
      <ClientShell title="Project" actions={backLink}>
        <Spinner label="Loading project…" />
      </ClientShell>
    );
  }

  if (isError || !data) {
    return (
      <ClientShell title="Project" actions={backLink}>
        <ErrorNote message="This project could not be loaded, or is no longer shared with you." />
      </ClientShell>
    );
  }

  return (
    <ClientShell title={data.project_name} subtitle={data.description ?? undefined} actions={backLink}>
      <div className="space-y-6 pb-16">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Card>
            <div className="text-2xl font-bold text-[#0F172A]">{data.total_tracked_hours}h</div>
            <div className="text-xs font-semibold uppercase tracking-wider text-[#94A3B8]">Total Hours</div>
          </Card>
          <Card>
            <div className="text-2xl font-bold text-[#0F172A]">{data.total_members}</div>
            <div className="text-xs font-semibold uppercase tracking-wider text-[#94A3B8]">Team Members</div>
          </Card>
          <Card>
            <div className="text-2xl font-bold text-[#0F172A]">{data.tasks.length}</div>
            <div className="text-xs font-semibold uppercase tracking-wider text-[#94A3B8]">Tasks</div>
          </Card>
          <Card>
            <div className="text-2xl font-bold capitalize text-[#0F172A]">{data.status}</div>
            <div className="text-xs font-semibold uppercase tracking-wider text-[#94A3B8]">Status</div>
          </Card>
        </div>

        <Card title="Team Members">
          {data.members.length === 0 ? (
            <p className="text-sm text-[#94A3B8]">No members are staffed on this project yet.</p>
          ) : (
            <div className="divide-y divide-[#F1F5F9]">
              {data.members.map((member) => (
                <div key={member.id} className="flex items-center justify-between py-2.5 text-sm">
                  <div>
                    <div className="font-medium text-[#0F172A]">{member.name}</div>
                    {member.designation && <div className="text-xs text-[#94A3B8]">{member.designation}</div>}
                  </div>
                  <div className="font-semibold text-[#475569]">{member.total_tracked_hours}h</div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card title="Tasks">
          {data.tasks.length === 0 ? (
            <p className="text-sm text-[#94A3B8]">No active tasks on this project yet.</p>
          ) : (
            <div className="divide-y divide-[#F1F5F9]">
              {data.tasks.map((task) => (
                <div key={task.id} className="flex items-center justify-between py-2.5 text-sm">
                  <div className="font-medium text-[#0F172A]">{task.task_name}</div>
                  <div className="flex items-center gap-3">
                    <span className="text-xs capitalize text-[#94A3B8]">{task.status}</span>
                    <span className="font-semibold text-[#475569]">{task.total_tracked_hours}h</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </ClientShell>
  );
};
