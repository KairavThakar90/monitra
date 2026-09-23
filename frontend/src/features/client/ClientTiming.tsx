import React, { useState } from 'react';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { Card, EmptyState, ErrorNote } from '../member/MemberUi';
import { RankedBars } from '../dashboard/v2/charts';
import { series } from '../dashboard/v2/theme';
import { DateRangeFilter } from '../dashboard/v2/filters';
import { useGetMyProjectsQuery } from '../../store/api/clientPortalApi';
import { formatHMS } from '../../utils/duration';
import { CLIENT_DEFAULT_RANGE, longDate } from './clientRange';

/** Tracked hours per shared project, over a date range — the client-portal
 * equivalent of the member's Time Tracking / Project-Wise report. */
export const ClientTiming: React.FC = () => {
  const [range, setRange] = useState(CLIENT_DEFAULT_RANGE);
  const { data, isFetching, isError } = useGetMyProjectsQuery({ start_date: range.from, end_date: range.to });
  const projects = data?.items ?? [];
  const totalSeconds = projects.reduce((sum, p) => sum + p.total_tracked_seconds, 0);

  return (
    <ClientShell title="Timing" subtitle={`Tracked time by project, ${longDate(range.from)} – ${longDate(range.to)}`}>
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

        {isError && <ErrorNote message="Timing could not be loaded. Please try again." />}

        <div className={`space-y-6 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
          <ClientKpiCard title="Total Tracked, All Shared Projects" value={formatHMS(totalSeconds)} />

          <Card title="Time by Project">
            {projects.length === 0 ? (
              <EmptyState message="No tracked time for this range." hint="Try a different date range." />
            ) : (
              <RankedBars
                items={projects
                  .slice()
                  .sort((a, b) => b.total_tracked_seconds - a.total_tracked_seconds)
                  .map((project) => ({
                    id: String(project.id),
                    name: project.project_name,
                    value: project.total_tracked_hours,
                    meta: `${project.member_count} member${project.member_count === 1 ? '' : 's'} active`,
                  }))}
                color={series[0]}
                formatValue={(n) => `${n}h`}
              />
            )}
          </Card>
        </div>
      </div>
    </ClientShell>
  );
};
