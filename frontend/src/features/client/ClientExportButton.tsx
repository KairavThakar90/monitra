import React from 'react';

/** Same button design as the admin/member Reports pages' "Export CSV". */
export const ClientExportButton: React.FC<{ onClick: () => void; disabled?: boolean }> = ({ onClick, disabled }) => (
  <button
    onClick={onClick}
    disabled={disabled}
    className="flex items-center gap-1.5 rounded-lg bg-[#0F172A] px-4 py-2 text-xs font-bold text-white shadow-sm transition hover:bg-[#1E293B] disabled:cursor-not-allowed disabled:opacity-40"
  >
    <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="2.5"
        d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3"
      />
    </svg>
    Export CSV
  </button>
);
