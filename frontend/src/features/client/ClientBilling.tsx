import React from 'react';
import { ClientShell } from './ClientShell';
import { ClientKpiCard } from './ClientKpiCard';
import { Card, EmptyState, ErrorNote, Spinner } from '../member/MemberUi';
import { useGetMyBillingQuery } from '../../store/api/clientPortalApi';
import type { MyBillingProject } from '../../store/api/clientPortalApi';

/**
 * Billing usage for the client's shared *billable* projects: each project's
 * budgeted, used and remaining hours, broken down by task.
 *
 * Everything here is all-time, not the date-ranged view the other portal
 * pages show — a budget is spent across a project's whole life, so this page
 * deliberately has no date filter. Internal (free-billing) projects never
 * appear: they have no budget and no billable time, and showing them with
 * zeros would fabricate a billing story that does not exist. The page itself
 * only renders when the admin granted `share_billing` for this client.
 */

const hoursLabel = (hours: number | null): string => (hours === null ? '—' : `${hours}h`);

/** Remaining hours, honest about overspend: negative renders as "Over by X". */
const RemainingLabel: React.FC<{ remaining: number | null }> = ({ remaining }) => {
  if (remaining === null) return <span className="text-[#94A3B8]">—</span>;
  if (remaining < 0) {
    return <span className="font-semibold text-rose-600">Over by {Math.abs(remaining)}h</span>;
  }
  return <span className="font-semibold text-emerald-700">{remaining}h</span>;
};

const UsageBar: React.FC<{ total: number | null; used: number }> = ({ total, used }) => {
  if (total === null || total <= 0) return null;
  const percent = Math.min(100, Math.round((used / total) * 100));
  const over = used > total;
  return (
    <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-[#F1F5F9]">
      <div
        className={`h-full rounded-full ${over ? 'bg-rose-500' : 'bg-[#2563EB]'}`}
        style={{ width: `${percent}%` }}
      />
    </div>
  );
};

const ProjectBillingCard: React.FC<{ project: MyBillingProject }> = ({ project }) => (
  <Card>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h3 className="text-base font-bold text-[#0F172A]">{project.project_name}</h3>
        <p className="mt-0.5 text-xs capitalize text-[#64748B]">{project.status} · Fixed billing</p>
      </div>
      <div className="flex flex-wrap items-center gap-4 text-sm">
        <div className="text-right">
          <div className="text-[10px] font-bold uppercase tracking-wider text-[#94A3B8]">Budget</div>
          <div className="font-semibold text-[#0F172A]">{hoursLabel(project.total_hours)}</div>
        </div>
        <div className="text-right">
          <div className="text-[10px] font-bold uppercase tracking-wider text-[#94A3B8]">Used</div>
          <div className="font-semibold text-[#0F172A]">{project.used_hours}h</div>
        </div>
        {/* Internal time (the project's default tasks) is shown for context but
            is not taken from the budget -- the same rule the admin's Project
            Management table applies to Remaining. */}
        {project.internal_hours !== undefined && (
          <div className="text-right" title="Time on internal tasks. Not counted against the budget.">
            <div className="text-[10px] font-bold uppercase tracking-wider text-[#94A3B8]">Internal</div>
            <div className="font-semibold text-[#64748B]">{project.internal_hours}h</div>
          </div>
        )}
        <div className="text-right">
          <div className="text-[10px] font-bold uppercase tracking-wider text-[#94A3B8]">Remaining</div>
          <RemainingLabel remaining={project.remaining_hours} />
        </div>
      </div>
    </div>
    <UsageBar total={project.total_hours} used={project.used_hours} />

    {project.tasks.length === 0 ? (
      <p className="mt-4 text-sm text-[#94A3B8]">No tasks on this project yet.</p>
    ) : (
      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-[11px] font-bold uppercase tracking-wider text-[#64748B]">
            <tr className="border-b border-[#F1F5F9]">
              <th className="py-2 pr-4">Task</th>
              <th className="py-2 pr-4">Status</th>
              <th className="py-2 pr-4 text-right">Hours Used</th>
              <th className="py-2 text-right">Remaining</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[#F1F5F9]">
            {project.tasks.map((task) => (
              <React.Fragment key={task.id}>
                <tr>
                  <td className="py-2.5 pr-4 font-medium text-[#0F172A]">{task.task_name}</td>
                  <td className="py-2.5 pr-4 capitalize text-[#64748B]">{task.status.replace('_', ' ')}</td>
                  <td className="py-2.5 pr-4 text-right text-[#0F172A]">{task.used_hours}h</td>
                  <td className="py-2.5 text-right">
                    <RemainingLabel remaining={task.remaining_hours} />
                  </td>
                </tr>
                {/* Who worked on this task. Absent entirely (not zeroed) when
                    the admin has not shared Member Details with this client. */}
                {(task.members ?? []).map((member) => (
                  <tr key={`${task.id}-${member.id}`} className="bg-[#F8FAFC]/60">
                    <td className="py-1.5 pl-6 pr-4 text-xs text-[#475569]" colSpan={2}>
                      <span className="mr-2 inline-block h-1.5 w-1.5 rounded-full bg-[#94A3B8] align-middle" />
                      {member.name}
                    </td>
                    <td className="py-1.5 pr-4 text-right text-xs text-[#475569]">{member.used_hours}h</td>
                    <td className="py-1.5" />
                  </tr>
                ))}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </div>
    )}
  </Card>
);

export const ClientBilling: React.FC = () => {
  const { data, isLoading, isError } = useGetMyBillingQuery();

  const shared = data?.permissions.share_billing ?? true;
  const projects = data?.items ?? [];
  const budgeted = projects.filter((p) => p.total_hours !== null);
  const totalBudget = budgeted.reduce((sum, p) => sum + (p.total_hours ?? 0), 0);
  const totalUsed = projects.reduce((sum, p) => sum + p.used_hours, 0);

  return (
    <ClientShell
      title="Billing"
      subtitle="Budgeted, used and remaining hours for your billable projects — all-time, broken down by task"
    >
      <div className="w-full space-y-6 pb-20">
        {isError && <ErrorNote message="Billing details could not be loaded. Please try again." />}

        {isLoading ? (
          <Spinner label="Loading billing details…" />
        ) : !shared ? (
          <Card>
            <EmptyState
              message="Billing details are not shared for your account."
              hint="Ask your admin to enable it if you need this."
            />
          </Card>
        ) : projects.length === 0 ? (
          <Card>
            <EmptyState
              message="No billable projects are shared with you."
              hint="Only fixed-billing projects appear here — internal projects have no billing to show."
            />
          </Card>
        ) : (
          <>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <ClientKpiCard title="Billable Projects" value={projects.length} />
              <ClientKpiCard
                title="Total Budgeted Hours"
                value={budgeted.length ? `${Math.round(totalBudget * 100) / 100}h` : '—'}
              />
              <ClientKpiCard title="Hours Used" value={`${Math.round(totalUsed * 100) / 100}h`} />
            </div>

            {data && !data.permissions.share_member_details && (
              <p className="text-xs text-[#94A3B8]">
                The per-member breakdown is not shared for your account — each
                task shows its totals only.
              </p>
            )}

            {projects.map((project) => (
              <ProjectBillingCard key={project.id} project={project} />
            ))}
          </>
        )}
      </div>
    </ClientShell>
  );
};
