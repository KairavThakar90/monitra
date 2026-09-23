import React from 'react';

/** The same stat-tile shape `MemberDashboard`'s `kpiCard` renders, without
 * the trend/delta (the client portal has no "previous period" comparison
 * wired up yet — an honest omission, not a placeholder value). */
export const ClientKpiCard: React.FC<{ title: string; value: string | number }> = ({ title, value }) => (
  <div className="flex flex-col justify-between rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm">
    <div className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">{title}</div>
    <div className="mt-1 text-3xl font-extrabold text-[#0F172A]">{value}</div>
  </div>
);
