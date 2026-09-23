import React, { useState } from 'react';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { Card, EmptyState, ErrorNote } from '../member/MemberUi';
import { RankedBars } from '../dashboard/v2/charts';
import { series } from '../dashboard/v2/theme';
import { DateRangeFilter } from '../dashboard/v2/filters';
import { useGetMyTaskHoursQuery } from '../../store/api/clientPortalApi';
import { formatHMS } from '../../utils/duration';
import { CLIENT_DEFAULT_RANGE, longDate } from './clientRange';

/** Tracked hours per task, across every shared project, over a date range —
 * the client-portal equivalent of the member/admin reports' Top Tasks. */
export const ClientTasks: React.FC = () => {
  const [range, setRange] = useState(CLIENT_DEFAULT_RANGE);
  const { data, isFetching, isError } = useGetMyTaskHoursQuery({ start_date: range.from, end_date: range.to });
  const tasks = data?.items ?? [];
  const totalSeconds = tasks.reduce((sum, t) => sum + t.total_tracked_seconds, 0);

  return (
    <ClientShell
      title="Tasks"
      subtitle={`Tasks worked on across your shared projects, ${longDate(range.from)} – ${longDate(range.to)}`}
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

        {isError && <ErrorNote message="Tasks could not be loaded. Please try again." />}

        <div className={`space-y-6 transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <ClientKpiCard title="Total Task Hours" value={formatHMS(totalSeconds)} />
            <ClientKpiCard title="Tasks Worked" value={tasks.length} />
          </div>

          <Card title="Top Tasks">
            {tasks.length === 0 ? (
              <EmptyState message="No task activity for this range." hint="Try a different date range." />
            ) : (
              <RankedBars
                items={tasks.map((task) => ({
                  id: String(task.id),
                  name: task.task_name,
                  value: task.total_tracked_hours,
                  meta: task.project_name ?? 'Unknown project',
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
