import React from 'react';
import { useNavigate } from 'react-router-dom';
import { ClientShell } from './ClientShell';
import { Card, EmptyState, ErrorNote, Spinner } from '../member/MemberUi';
import { useGetMyProjectsQuery } from '../../store/api/clientPortalApi';

export const ClientDashboard: React.FC = () => {
  const navigate = useNavigate();
  const { data, isLoading, isError } = useGetMyProjectsQuery();
  const projects = data?.items ?? [];

  return (
    <ClientShell title="Your Projects" subtitle="Projects your Monitra contact has shared with you.">
      <div className="space-y-4 pb-16">
        {isError && <ErrorNote message="Your projects could not be loaded. Please try again." />}

        {isLoading ? (
          <Spinner label="Loading your projects…" />
        ) : projects.length === 0 ? (
          <Card>
            <EmptyState
              message="No projects have been shared with you yet."
              hint="Once your Monitra contact shares a project, it will appear here."
            />
          </Card>
        ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {projects.map((project) => (
              <button
                key={project.id}
                onClick={() => navigate(`/client/projects/${project.id}`)}
                className="text-left rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm transition hover:shadow-md hover:border-[#2563EB]/40"
              >
                <div className="text-base font-bold text-[#0F172A]">{project.project_name}</div>
                {project.description && (
                  <p className="mt-1 line-clamp-2 text-sm text-[#64748B]">{project.description}</p>
                )}
                <div className="mt-4 flex items-center justify-between text-xs font-semibold text-[#94A3B8]">
                  <span>{project.member_count} member{project.member_count === 1 ? '' : 's'}</span>
                  <span>{project.total_tracked_hours}h tracked</span>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </ClientShell>
  );
};
