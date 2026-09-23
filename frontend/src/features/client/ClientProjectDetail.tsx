import React, { useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { Card, EmptyState, ErrorNote, Spinner } from '../member/MemberUi';
import { RankedBars } from '../dashboard/v2/charts';
import { series } from '../dashboard/v2/theme';
import { DateRangeFilter, rangeForSpan } from '../dashboard/v2/filters';
import { useGetMyProjectDetailQuery } from '../../store/api/clientPortalApi';
import { formatHMS } from '../../utils/duration';
import { CLIENT_DEFAULT_RANGE, longDate } from './clientRange';

export const ClientProjectDetail: React.FC = () => {
  const { projectId } = useParams<{ projectId: string }>();
  const [searchParams] = useSearchParams();
  const id = Number(projectId);

  const initialRange = (() => {
    const start = searchParams.get('start');
    const end = searchParams.get('end');
    return start && end ? rangeForSpan(start, end) : CLIENT_DEFAULT_RANGE;
  })();
  const [range, setRange] = useState(initialRange);

  const { data, isLoading, isFetching, isError } = useGetMyProjectDetailQuery(
    { projectId: id, start_date: range.from, end_date: range.to },
    { skip: !Number.isFinite(id) },
  );

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
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#E2E8F0] bg-white p-2 pl-4 shadow-sm">
          <DateRangeFilter value={range} onChange={setRange} />
          <button
            onClick={() => setRange(CLIENT_DEFAULT_RANGE)}
            className="rounded-lg border border-[#E2E8F0] px-4 py-2 text-[13px] font-bold text-[#64748B] transition hover:bg-[#F8FAFC] hover:text-[#0F172A]"
          >
            Reset
          </button>
        </div>
        <p className="text-xs font-semibold text-[#94A3B8]">
          {longDate(range.from)} – {longDate(range.to)}
        </p>

        <div className={`space-y-6 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <ClientKpiCard title="Hours Tracked" value={formatHMS(data.total_tracked_seconds)} />
            <ClientKpiCard title="Team Members" value={data.total_members} />
            <ClientKpiCard title="Tasks" value={data.tasks.length} />
            <ClientKpiCard title="Status" value={data.status} />
          </div>

          <Card title="Time by Member">
            {data.members.length === 0 ? (
              <EmptyState message="No members are staffed on this project yet." />
            ) : (
              <RankedBars
                items={data.members.map((member) => ({
                  id: String(member.id),
                  name: member.name,
                  value: member.total_tracked_hours,
                  meta: member.designation ?? '',
                }))}
                color={series[1]}
                formatValue={(n) => `${n}h`}
                avatars
              />
            )}
          </Card>

          <Card title="Time by Task">
            {data.tasks.length === 0 ? (
              <EmptyState message="No active tasks on this project yet." />
            ) : (
              <RankedBars
                items={data.tasks.map((task) => ({
                  id: String(task.id),
                  name: task.task_name,
                  value: task.total_tracked_hours,
                  meta: task.status,
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
