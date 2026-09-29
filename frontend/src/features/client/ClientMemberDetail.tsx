import React, { useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { Card, EmptyState, ErrorNote, Spinner } from '../member/MemberUi';
import { ClientTable } from './ClientTable';
import { DateRangeFilter, rangeForSpan } from '../dashboard/v2/filters';
import { useGetMyMemberDetailQuery } from '../../store/api/clientPortalApi';
import { CLIENT_DEFAULT_RANGE, formatSharedHMS, longDate } from './clientRange';

/**
 * One team member, as the client may know them: who they are, which shared
 * projects they are on, and a date-wise record of their activity — per day,
 * first activity, last activity, sessions and total tracked time.
 *
 * "Activity" is tracked time against this client's shared projects, never
 * the member's whole day. Times are IST, the same clock every day-wise
 * figure in Monitra is expressed in.
 */
export const ClientMemberDetail: React.FC = () => {
  const { memberId } = useParams<{ memberId: string }>();
  const [searchParams] = useSearchParams();
  const id = Number(memberId);

  const initialRange = (() => {
    const start = searchParams.get('start');
    const end = searchParams.get('end');
    return start && end ? rangeForSpan(start, end) : CLIENT_DEFAULT_RANGE;
  })();
  const [range, setRange] = useState(initialRange);

  const { data, isLoading, isFetching, isError } = useGetMyMemberDetailQuery(
    { memberId: id, start_date: range.from, end_date: range.to },
    { skip: !Number.isFinite(id) },
  );

  const backLink = (
    <Link to="/client/members" className="text-sm font-semibold text-[#2563EB] hover:text-blue-700">
      &larr; Back to members
    </Link>
  );

  if (isLoading) {
    return (
      <ClientShell title="Member" actions={backLink}>
        <Spinner label="Loading member…" />
      </ClientShell>
    );
  }

  if (isError || !data) {
    return (
      <ClientShell title="Member" actions={backLink}>
        <ErrorNote message="This member could not be loaded, or is not part of your shared projects." />
      </ClientShell>
    );
  }

  const timingShared = data.permissions.share_timing;

  return (
    <ClientShell title={data.name} subtitle={data.designation ?? undefined} actions={backLink}>
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
        <p className="text-xs font-semibold text-[#94A3B8]">
          {longDate(range.from)} – {longDate(range.to)}
        </p>

        <div className={`space-y-6 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <ClientKpiCard title="Total Tracked" value={formatSharedHMS(data.total_tracked_seconds)} />
            <ClientKpiCard title="Days Active" value={data.days_active ?? 'Not shared'} />
            <ClientKpiCard title="Your Projects" value={data.projects.length} />
          </div>

          <Card title="Projects">
            {data.projects.length === 0 ? (
              <EmptyState message="This member is not on any of your shared projects." />
            ) : (
              <ul className="divide-y divide-[#F1F5F9]">
                {data.projects.map((project) => (
                  <li key={project.id} className="flex items-center justify-between py-2.5 text-sm">
                    <span className="font-medium text-[#0F172A]">{project.project_name}</span>
                    <span className="text-xs font-semibold text-[#94A3B8]">
                      {project.assigned ? 'Assigned' : 'Worked in range'}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {!timingShared ? (
            <Card title="Date-wise Activity">
              <EmptyState
                message="Timing is not shared for your account."
                hint="Ask your admin to enable it if you need this."
              />
            </Card>
          ) : data.days.length === 0 ? (
            <Card title="Date-wise Activity">
              <EmptyState
                message="No activity on your projects in this range."
                hint="Try a different date range."
              />
            </Card>
          ) : (
            <ClientTable
              headers={[
                { label: 'Date' },
                { label: 'First Activity' },
                { label: 'Last Activity' },
                { label: 'Sessions', align: 'right' },
                { label: 'Hours', align: 'right' },
                { label: 'Tracked Time', align: 'right' },
              ]}
            >
              {data.days.map((day) => (
                <tr key={day.date}>
                  <td className="px-4 py-3 font-medium text-[#0F172A]">{longDate(day.date)}</td>
                  <td className="px-4 py-3 text-[#475569]">{day.first_activity}</td>
                  <td className="px-4 py-3 text-[#475569]">{day.last_activity ?? 'In progress'}</td>
                  <td className="px-4 py-3 text-right text-[#475569]">{day.session_count}</td>
                  <td className="px-4 py-3 text-right font-semibold text-[#0F172A]">{day.total_tracked_hours}h</td>
                  <td className="px-4 py-3 text-right text-[#475569]">{formatSharedHMS(day.total_tracked_seconds)}</td>
                </tr>
              ))}
            </ClientTable>
          )}
        </div>
      </div>
    </ClientShell>
  );
};
