import React, { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { V2Shell } from '../dashboard/v2/V2Shell';
import {
  useGetTeamSummaryQuery,
  useGetTeamLeadersQuery,
  useGetTeamLeaderByIdQuery,
  useGetLeaderProjectsQuery,
  useGetTeamProjectByIdQuery,
} from '../../store/api/teamsApi';
import { InlineRefreshIndicator } from '../../components/InlineRefreshIndicator';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { PaginationArrow } from '../../components/PaginationArrow';

// Stat card icons (Heroicons outline, inline so no new dependency)
type IconProps = { className?: string };

const BriefcaseIcon: React.FC<IconProps> = ({ className }) => (
  <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M21 13.255A23.931 23.931 0 0112 15c-3.183 0-6.22-.62-9-1.745M16 6V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v1m4 6h.01M5 20h14a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
  </svg>
);

const UsersIcon: React.FC<IconProps> = ({ className }) => (
  <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
  </svg>
);

const FolderIcon: React.FC<IconProps> = ({ className }) => (
  <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
  </svg>
);

const RocketIcon: React.FC<IconProps> = ({ className }) => (
  <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M13 10V3L4 14h7v7l9-11h-7z" />
  </svg>
);

// Re-using some UI helpers
const Avatar: React.FC<{ name: string; color: string; size?: number; ring?: boolean; solid?: boolean }> = ({
  name,
  color,
  size = 40,
  ring = false,
  solid = false,
}) => (
  <div
    className={'flex shrink-0 items-center justify-center rounded-full font-bold ' + (ring ? 'ring-2 ring-white shadow-sm ' : '')}
    style={{
      width: size,
      height: size,
      background: solid ? color : `${color}15`,
      color: solid ? '#FFFFFF' : color,
      fontSize: size * 0.4,
    }}
    title={name}
    aria-label={name}
  >
    {(name || '?').split(' ').map(n => n[0]).join('').substring(0, 2).toUpperCase() || '?'}
  </div>
);

const ProgressBar: React.FC<{ value: number; color: string }> = ({ value, color }) => {
  const percent = Math.min(100, Math.max(0, value * 100));
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
      <div className="h-full rounded-full transition-all duration-500" style={{ width: `${percent}%`, background: color }} />
    </div>
  );
};

const StatusPill: React.FC<{ status: { name: string; color: string } }> = ({ status }) => {
  if (!status) return null;
  return (
    <span
      className="inline-flex shrink-0 items-center justify-center rounded-full px-2.5 py-0.5 text-xs font-bold shadow-sm"
      style={{
        background: `${status.color}15`,
        color: status.color,
        border: `1px solid ${status.color}30`,
      }}
    >
      {status.name}
    </span>
  );
};

const Crumb: React.FC<{ items: { label: string; onClick?: () => void }[] }> = ({ items }) => (
  <div className="flex items-center gap-2 text-sm font-semibold">
    {items.map((it, i) => (
      <React.Fragment key={i}>
        {i > 0 && <span className="text-slate-300">/</span>}
        {it.onClick ? (
          <button onClick={it.onClick} className="text-slate-500 hover:text-blue-600 transition">
            {it.label}
          </button>
        ) : (
          <span className="text-[#0F172A]">{it.label}</span>
        )}
      </React.Fragment>
    ))}
  </div>
);

const EmptyState: React.FC<{ message: string }> = ({ message }) => (
  <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-slate-300 bg-slate-50 py-20 text-center">
    <div className="flex h-12 w-12 items-center justify-center rounded-full bg-slate-200 text-slate-400">
      <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" />
      </svg>
    </div>
    <h3 className="mt-4 text-sm font-bold text-slate-700">No data found</h3>
    <p className="mt-1 text-sm text-slate-500">{message}</p>
  </div>
);

const LoadingSpinner: React.FC = () => (
  <div className="flex min-h-32 items-center justify-center" role="status" aria-label="Loading">
    <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-500 border-t-transparent" />
  </div>
);

/* Pagination Component */
const Pagination: React.FC<{ 
  page: number; 
  totalPages: number; 
  totalItems: number; 
  limit: number; 
  setPage: (p: number) => void;
  setLimit: (l: number) => void;
  itemName?: string;
}> = ({ page, totalPages, totalItems, limit, setPage, setLimit, itemName = "items" }) => {
  // if (totalItems === 0) return null;
  const startItem = totalItems === 0 ? 0 : (page - 1) * limit + 1;
  const getVisiblePages = () => {
    if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i + 1);
    if (page <= 4) return [1, 2, 3, 4, 5, '...', totalPages];
    if (page >= totalPages - 3) return [1, '...', totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1, totalPages];
    return [1, '...', page - 1, page, page + 1, '...', totalPages];
  };
  const pages = getVisiblePages();
  const endItem = Math.min(page * limit, totalItems);

  return (
    <div className="mt-8 flex flex-col sm:flex-row items-center justify-between gap-4 border-t border-slate-200 pt-5 text-sm text-slate-500">
      <div>
        Showing {startItem} to {endItem} of {totalItems} {itemName}
      </div>
      <div className="flex items-center gap-3">
        <select
          value={limit}
          onChange={(e) => { setLimit(Number(e.target.value)); setPage(1); }}
          className="rounded-md border border-slate-300 py-1.5 pl-3 pr-8 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
        >
          <option value={12}>12</option>
          <option value={20}>20</option>
          <option value={50}>50</option>
          <option value={100}>100</option>
        </select>

        <div className="flex items-center gap-1">
          <PaginationArrow direction="prev" disabled={page === 1} onClick={() => setPage(page - 1)} />
          
          {pages.map((p, idx) => (
            p === '...' ? (
              <span key={`ellipsis-${idx}`} className="flex h-8 w-8 items-center justify-center text-slate-400">...</span>
            ) : (
              <button
                key={p}
                onClick={() => setPage(p as number)}
                className={`flex h-8 w-8 items-center justify-center rounded text-sm font-semibold transition ${
                  p === page ? 'bg-blue-500 text-white shadow-sm' : 'text-slate-600 hover:bg-slate-100'
                }`}
              >
                {p}
              </button>
            )
          ))}

          <PaginationArrow direction="next" disabled={page === totalPages} onClick={() => setPage(page + 1)} />
        </div>
      </div>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* Sub-Views                                                           */
/* ------------------------------------------------------------------ */

const LeadersView: React.FC<{ onOpen: (leaderId: string) => void }> = ({ onOpen }) => {
  const [search, setSearch] = useState('');
  const debouncedSearch = useDebouncedValue(search);
  const { data: summary, isLoading: isLoadingSummary } = useGetTeamSummaryQuery();
  const { data: leadersData, isLoading: isLoadingLeaders, isFetching: isFetchingLeaders } = useGetTeamLeadersQuery({ search: debouncedSearch, limit: 100 });

  // Only the very first load replaces the page. Returning a spinner while
  // refetching used to unmount the search box mid-word, so a search could not
  // be typed out; now the cards stay put and refresh underneath.
  if ((isLoadingSummary && !summary) || (isLoadingLeaders && !leadersData)) return <LoadingSpinner />;

  const leaders = leadersData?.items || [];

  return (
    <div className="space-y-8">
      {/* Overview Cards */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {[
          { label: 'Team Leaders', val: summary?.team_leaders || 0, Icon: BriefcaseIcon },
          { label: 'Team Members', val: summary?.employees || 0, Icon: UsersIcon },
          { label: 'Total Projects', val: summary?.total_projects || 0, Icon: FolderIcon },
          { label: 'Active Projects', val: summary?.active_projects || 0, Icon: RocketIcon },
        ].map((stat, i) => (
          <div key={i} className="flex min-w-0 items-center gap-3 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm transition hover:shadow-md sm:gap-4 sm:p-5">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-blue-600 shadow-inner">
              <stat.Icon className="h-6 w-6" />
            </div>
            <div>
              <div className="text-3xl font-black text-[#0F172A]">{stat.val}</div>
              <div className="mt-1 text-[11px] font-bold uppercase tracking-wider text-slate-400">{stat.label}</div>
            </div>
          </div>
        ))}
      </div>

      {/* Phone: the heading, then a full-width search below it. Side by side, a
          fixed 288px search box left the heading two words wide. */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <h2 className="text-xl font-bold tracking-tight text-[#0F172A]">Leadership Team</h2>
          <InlineRefreshIndicator active={isFetchingLeaders && !!leadersData} />
        </div>
        <input
          type="text"
          placeholder="Search leaders..."
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="w-full rounded-lg border border-slate-300 px-4 py-2 text-base shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 sm:w-72 sm:text-sm"
        />
      </div>

      {leaders.length === 0 ? (
        <EmptyState message="No leaders match your search." />
      ) : (
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 xl:grid-cols-3">
          {leaders.map(row => (
            <div key={row.id} className="group relative flex min-w-0 flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white p-5 shadow-sm sm:p-6 transition duration-300 hover:border-blue-400 hover:shadow-xl">
              <div className="absolute top-0 left-0 h-1.5 w-full bg-gradient-to-r from-blue-500 to-indigo-500 opacity-0 transition-opacity group-hover:opacity-100" />
              
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-4">
                  <Avatar name={row.name} color="#2563EB" size={56} />
                  <div className="min-w-0">
                    <h3 className="truncate text-lg font-bold text-[#0F172A]">{row.name}</h3>
                    <div className="truncate text-sm font-medium text-blue-600">{row.designation || 'Leader'}</div>
                  </div>
                </div>
              </div>

              <div className="mt-5 grid grid-cols-2 gap-3 rounded-xl bg-slate-50 p-3 sm:mt-6 sm:gap-4 sm:p-4">
                <div>
                  <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Projects</div>
                  <div className="mt-1 text-base font-bold text-[#0F172A]">
                    {row.active_projects} <span className="font-medium text-slate-500">active</span>
                  </div>
                </div>
                <div className="min-w-0">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Members</div>
                  <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="text-base font-bold text-[#0F172A]">{row.total_members}</span>
                    {row.members_preview?.length > 0 && (
                      <div className="flex -space-x-2">
                        {row.members_preview.slice(0, 3).map((m: any, index: number) => (
                          <Avatar
                            key={m.id}
                            name={m.name}
                            color={['#F43F5E', '#06B6D4', '#10B981', '#3B82F6'][index % 4]}
                            size={32}
                            ring
                            solid
                          />
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>

              <div className="mt-6">
                <div className="mb-1.5 flex items-center justify-between text-[11px] font-semibold text-slate-500">
                  <span>Tasks Completed</span>
                  <span>{row.completion?.completed || 0} / {row.completion?.total || 0} ({row.completion?.percentage || 0}%)</span>
                </div>
                <ProgressBar value={(row.completion?.percentage || 0) / 100} color="#2563EB" />
              </div>

              <div className="mt-6 pt-5 border-t border-slate-100">
                <button
                  onClick={() => onOpen(row.id.toString())}
                  className="w-full flex items-center justify-center gap-2 rounded-xl bg-blue-50 py-3 text-sm font-bold text-blue-600 transition hover:bg-blue-600 hover:text-white"
                >
                  View Team Details
                  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14 5l7 7m0 0l-7 7m7-7H3" />
                  </svg>
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

const LeaderProjectsView: React.FC<{ leaderId: number; onOpen: (projectId: string) => void; onBack: () => void }> = ({
  leaderId,
  onOpen,
  onBack,
}) => {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [limit, setLimit] = useState(20);

  const debouncedSearch = useDebouncedValue(search);

  const { data: leaderData, isLoading: isLoadingLeader } = useGetTeamLeaderByIdQuery(leaderId);
  // Using Server-Side pagination to properly load all 900+ projects
  const { data: projectsData, isLoading: isLoadingProjects, isFetching: isFetchingProjects } = useGetLeaderProjectsQuery({ leaderId, page, limit, search: debouncedSearch });

  // As above: only a genuinely empty screen gets the blocking spinner.
  if ((isLoadingLeader && !leaderData) || (isLoadingProjects && !projectsData)) return <LoadingSpinner />;

  const leader = leaderData?.leader;
  const currentProjects = projectsData?.items || [];
  const totalPages = projectsData?.pagination?.total_pages || 1;
  const totalItems = projectsData?.pagination?.total || 0;

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:gap-5 sm:p-6">
        <div className="flex min-w-0 items-center gap-4 sm:gap-5">
          <Avatar name={leader?.name || '?'} color="#2563EB" size={64} />
          <div className="min-w-0">
            <h2 className="break-words text-xl font-bold tracking-tight text-[#0F172A] sm:text-2xl">{leader?.name}</h2>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm font-medium text-slate-500">
              <span className="rounded-md bg-blue-50 px-2 py-0.5 font-bold text-blue-600">{leader?.designation || 'Leader'}</span>
              <span className="h-1 w-1 rounded-full bg-slate-300" />
              <span>{leader?.total_members} Team Members</span>
            </div>
          </div>
        </div>
        <button
          onClick={onBack}
          className="w-full rounded-lg border border-slate-300 bg-white px-5 py-2.5 text-sm font-bold text-slate-600 shadow-sm transition hover:bg-slate-50 md:w-auto"
        >
          &larr; Back to All Teams
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-4 rounded-xl bg-slate-50 p-4 border border-slate-200">
        <input
          type="text"
          placeholder="Search projects..."
          value={search}
          onChange={e => { setSearch(e.target.value); setPage(1); }}
          className="w-full rounded-lg border border-slate-300 px-3 py-2 text-base shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 sm:w-64 sm:text-sm"
        />
        <InlineRefreshIndicator active={isFetchingProjects && !!projectsData} />
        {search && (
          <button onClick={() => { setSearch(''); setPage(1); }} className="text-sm font-bold text-slate-500 hover:text-slate-800">
            Clear Search
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {currentProjects.length === 0 ? (
          <div className="col-span-full">
            <EmptyState message="No projects found matching the criteria." />
          </div>
        ) : (
          currentProjects.map((project: any) => (
            <div
              key={project.id}
              onClick={() => onOpen(project.id.toString())}
              className="group flex cursor-pointer flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm transition duration-300 hover:border-blue-400 hover:shadow-xl"
            >
              <div className="h-1.5 w-full transition-all group-hover:h-2" style={{ background: project.status?.color || '#cbd5e1' }} />
              <div className="flex flex-1 flex-col p-6">
                <div className="flex items-start justify-between gap-3">
                  <h3 className="line-clamp-2 text-base font-bold leading-snug text-[#0F172A] group-hover:text-blue-600">
                    {project.project_name}
                  </h3>
                  <StatusPill status={project.status} />
                </div>

                <div className="mt-5 flex flex-1 flex-col justify-end gap-5">
                  <div className="rounded-xl bg-slate-50 p-3">
                    <div className="mb-2 flex items-center justify-between text-xs font-semibold text-slate-600">
                      <span>Tasks: {project.task_progress?.completed || 0}/{project.task_progress?.total || 0}</span>
                      <span style={{ color: project.status?.color || '#cbd5e1' }}>{project.task_progress?.percentage || 0}%</span>
                    </div>
                    <ProgressBar value={(project.task_progress?.percentage || 0) / 100} color={project.status?.color || '#cbd5e1'} />
                  </div>

                  <div className="flex items-center justify-between pt-2">
                    <div className="flex items-center gap-1.5 text-xs font-bold text-slate-500">
                      <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z" />
                      </svg>
                      {project.member_count || 0} assigned
                    </div>
                    {project.deadline && (
                      <div className="flex items-center gap-1 text-[11px] font-bold text-slate-400">
                        <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>
                        {new Date(project.deadline).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          ))
        )}
      </div>

      <Pagination page={page} totalPages={totalPages} totalItems={totalItems} limit={limit} setPage={setPage} setLimit={setLimit} itemName="projects" />
    </div>
  );
};

const MemberCard: React.FC<{ member: any; leaderAccent: string }> = ({ member, leaderAccent }) => {
  if (!member) return null;

  return (
    <div className="group flex flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm transition duration-300 hover:border-blue-400 hover:shadow-xl">
      <div className="p-6">
        <div className="flex items-center gap-4">
          <Avatar name={member.name || '?'} color={leaderAccent} size={56} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <div className="truncate text-lg font-bold text-[#0F172A]">{member.name}</div>
              <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-bold text-slate-500 border border-slate-200">ID: {member.id}</span>
            </div>
            <div className="mt-0.5 truncate text-sm font-medium text-blue-600">{member.designation}</div>
          </div>
        </div>

        <div className="mt-6 rounded-xl bg-slate-50 p-4 border border-slate-100">
          <div className="mb-2 flex items-center justify-between text-xs font-semibold">
            <span className="text-slate-500 uppercase tracking-wider text-[10px]">Task Progress</span>
            <span className="text-[#0F172A] font-bold">
              {member.completed_tasks} / {member.total_tasks} done
            </span>
          </div>
          <ProgressBar value={member.total_tasks ? member.completed_tasks / member.total_tasks : 0} color={leaderAccent} />
        </div>

        <div className="mt-6 space-y-3 border-t border-slate-100 pt-5">
          <div className="flex items-center justify-between text-xs font-bold uppercase tracking-wider text-slate-400">
            <span>Tasks in this project</span>
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[#0F172A]">{member.tasks?.length || 0}</span>
          </div>
          {(!member.tasks || member.tasks.length === 0) ? (
            <div className="rounded-xl border border-dashed border-slate-200 p-4 text-center text-sm italic text-slate-400">
              No tasks assigned yet.
            </div>
          ) : (
            <div className="space-y-2 max-h-[220px] overflow-y-auto pr-2 scrollbar-thin scrollbar-thumb-slate-200">
              {member.tasks.map((task: any) => (
                <div key={task.id} className="flex items-start justify-between gap-3 rounded-xl border border-slate-100 bg-white p-3 shadow-sm transition hover:border-slate-200">
                  <div className="flex min-w-0 items-start gap-2.5">
                    <span className="mt-1 h-2 w-2 shrink-0 rounded-full" style={{ background: task.status?.color || '#cbd5e1' }} />
                    <span className="text-sm font-semibold text-slate-700 leading-snug">{task.name}</span>
                  </div>
                  <span
                    className="shrink-0 rounded-md px-2 py-1 text-[10px] font-bold whitespace-nowrap"
                    style={{ background: `${task.status?.color || '#cbd5e1'}14`, color: task.status?.color || '#cbd5e1' }}
                  >
                    {task.status?.name || 'Unknown'}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};


const ProjectMembersView: React.FC<{ projectId: number; onBack: () => void }> = ({ projectId, onBack }) => {
  const { data: project, isLoading } = useGetTeamProjectByIdQuery(projectId);

  if (isLoading && !project) return <LoadingSpinner />;
  if (!project) return <EmptyState message="Project not found." />;

  const accent = '#2563EB'; // generic fallback or derive from leader
  const membersList = project.members?.items || [];

  return (
    <div className="space-y-6">
      <div className="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm">
        <div className="h-2 w-full" style={{ background: project.status?.color || '#cbd5e1' }} />
        <div className="p-8">
          <div className="flex flex-col gap-6 md:flex-row md:items-start md:justify-between">
            <div className="min-w-0">
              <div className="flex items-center gap-4">
                <h2 className="text-2xl font-black tracking-tight text-[#0F172A]">{project.project_name}</h2>
                <StatusPill status={project.status} />
              </div>
              <p className="mt-3 max-w-3xl text-sm leading-relaxed text-slate-500">{project.description || 'No description provided.'}</p>
            </div>
            <button
              onClick={onBack}
              className="shrink-0 rounded-xl border border-slate-300 bg-white px-5 py-2.5 text-sm font-bold text-slate-600 shadow-sm transition hover:bg-slate-50 hover:text-blue-600"
            >
              &larr; {project.leader?.name.split(' ')[0] || 'Leader'}'s Projects
            </button>
          </div>

          <div className="mt-8 grid grid-cols-2 gap-6 border-t border-slate-100 pt-8 lg:grid-cols-4">
            <div className="rounded-2xl bg-slate-50 p-4 border border-slate-100 shadow-sm">
              <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Team Leader</div>
              <div className="mt-2 flex items-center gap-3">
                <Avatar name={project.leader?.name || '?'} color={accent} size={32} />
                <span className="text-sm font-bold text-[#0F172A]">{project.leader?.name}</span>
              </div>
            </div>
            <div className="rounded-2xl bg-slate-50 p-4 border border-slate-100 shadow-sm">
              <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Deadline</div>
              <div className="mt-3 flex items-center gap-2 text-sm font-bold text-[#0F172A]">
                <svg className="h-4 w-4 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>
                {project.deadline ? new Date(project.deadline).toLocaleDateString(undefined, { month: 'long', day: 'numeric', year: 'numeric' }) : 'No Deadline'}
              </div>
            </div>
            <div className="rounded-2xl bg-slate-50 p-4 border border-slate-100 shadow-sm">
              <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Assigned Members</div>
              <div className="mt-3 flex items-center gap-2 text-sm font-bold text-[#0F172A]">
                <svg className="h-4 w-4 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z" /></svg>
                {membersList.length} members
              </div>
            </div>
            <div className="rounded-2xl bg-slate-50 p-4 border border-slate-100 shadow-sm">
              <div className="mb-2 flex items-center justify-between">
                <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Tasks Progress</div>
                <div className="text-xs font-bold text-[#0F172A]">
                  {project.task_progress?.completed || 0} / {project.task_progress?.total || 0}
                </div>
              </div>
              <ProgressBar value={(project.task_progress?.percentage || 0) / 100} color={project.status?.color || '#cbd5e1'} />
            </div>
          </div>
        </div>
      </div>

      <h3 className="text-xl font-bold tracking-tight text-[#0F172A] mt-8 mb-4 px-1">Project Members ({membersList.length})</h3>

      {membersList.length === 0 ? (
        <EmptyState message="No employee is assigned to this project yet." />
      ) : (
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 xl:grid-cols-3">
          {membersList.map((member: any) => (
            <MemberCard key={member.id} member={member} leaderAccent={accent} />
          ))}
        </div>
      )}
    </div>
  );
};


/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

export const AdminTeams: React.FC = () => {
  const navigate = useNavigate();
  const { leaderId, projectId } = useParams<{ leaderId: string; projectId: string }>();

  const goRoot = () => navigate('/admin/teams');
  const goLeader = (id: string) => navigate(`/admin/teams/${id}`);

  let title = 'Teams';
  let subtitle = 'Leaders, the projects they run, and who works on them.';
  let breadcrumb: React.ReactNode = null;
  let body: React.ReactNode;

  if (projectId && leaderId) {
    title = 'Project Overview';
    subtitle = `View team members and their assignments`;
    breadcrumb = (
      <Crumb
        items={[
          { label: 'Teams', onClick: goRoot },
          { label: 'Leader Projects', onClick: () => goLeader(leaderId) },
          { label: 'Project Details' },
        ]}
      />
    );
    body = <ProjectMembersView projectId={parseInt(projectId)} onBack={() => goLeader(leaderId)} />;
  } else if (leaderId) {
    title = `Leader's Portfolio`;
    subtitle = `All projects managed by this leader`;
    breadcrumb = <Crumb items={[{ label: 'Teams', onClick: goRoot }, { label: 'Projects' }]} />;
    body = (
      <LeaderProjectsView
        leaderId={parseInt(leaderId)}
        onOpen={pid => navigate(`/admin/teams/${leaderId}/${pid}`)}
        onBack={goRoot}
      />
    );
  } else {
    body = <LeadersView onOpen={goLeader} />;
  }

  return (
    <V2Shell title={title} subtitle={subtitle} breadcrumb={breadcrumb}>
      {/* No side padding of its own on a phone: the shell's <main> already gives 16px. */}
      <div className="w-full px-0 sm:px-6 lg:px-8 pt-2 sm:pt-6 pb-20">{body}</div>
    </V2Shell>
  );
};
