import React, { useState } from 'react';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { Card, EmptyState, ErrorNote } from '../member/MemberUi';
import { RankedBars } from '../dashboard/v2/charts';
import { series } from '../dashboard/v2/theme';
import { DateRangeFilter } from '../dashboard/v2/filters';
import { useGetMyMemberHoursQuery } from '../../store/api/clientPortalApi';
import { formatHMS } from '../../utils/duration';
import { CLIENT_DEFAULT_RANGE, longDate } from './clientRange';

/** Tracked hours per team member, across every shared project, over a date
 * range — the client-portal equivalent of the member/admin reports' own
 * per-member ranking. */
export const ClientMembers: React.FC = () => {
  const [range, setRange] = useState(CLIENT_DEFAULT_RANGE);
  const { data, isFetching, isError } = useGetMyMemberHoursQuery({ start_date: range.from, end_date: range.to });
  const members = data?.items ?? [];
  const totalSeconds = members.reduce((sum, m) => sum + m.total_tracked_seconds, 0);

  return (
    <ClientShell
      title="Members"
      subtitle={`Team members working on your shared projects, ${longDate(range.from)} – ${longDate(range.to)}`}
    >
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

        {isError && <ErrorNote message="Members could not be loaded. Please try again." />}

        <div className={`space-y-6 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <ClientKpiCard title="Total Member Hours" value={formatHMS(totalSeconds)} />
            <ClientKpiCard title="Members Active" value={members.length} />
          </div>

          <Card title="Time by Member">
            {members.length === 0 ? (
              <EmptyState message="No member activity for this range." hint="Try a different date range." />
            ) : (
              <RankedBars
                items={members.map((member) => ({
                  id: String(member.id),
                  name: member.name,
                  value: member.total_tracked_hours,
                  meta: `${member.project_count} project${member.project_count === 1 ? '' : 's'}`,
                }))}
                color={series[1]}
                formatValue={(n) => `${n}h`}
                avatars
              />
            )}
          </Card>
        </div>
      </div>
    </ClientShell>
  );
};
