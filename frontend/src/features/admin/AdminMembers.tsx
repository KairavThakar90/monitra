import React, { useState, useMemo } from 'react';
import { V2Shell } from '../dashboard/v2/V2Shell';
import { 
  useGetMembersQuery, 
  useCreateMemberMutation, 
  useUpdateMemberMutation, 
  useDeleteMemberMutation,
  useGetMemberDetailsQuery,
} from '../../store/api/membersApi';
import type { Member } from '../../store/api/membersApi';
import { useGetProjectMetadataQuery } from '../../store/api/projectsApi';
import { useFeedback } from '../../components/FeedbackProvider';
import { InlineRefreshIndicator } from '../../components/InlineRefreshIndicator';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { PaginationArrow } from '../../components/PaginationArrow';
import { useAuth } from '../auth/authContext';
import { AppIcon } from "../../components/AppIcon";
import { FieldError, SEARCH_MAX_LENGTH, useFormValidation, validateSearchTerm } from '../../validation';

const GRADIENT_CYAN_PURPLE = 'bg-gradient-to-r from-[#0ea5e9] via-[#3b82f6] to-[#8b5cf6]';

/**
 * Badge tone per role. A role with no entry falls back to slate rather than
 * being invisible — the roles themselves come from the server, so this map
 * is allowed to lag behind it, but the row must still render.
 */
const ROLE_TONES: Record<string, string> = {
  administrator: 'text-purple-600',
  hr: 'text-amber-600',
  leader: 'text-blue-600',
  employee: 'text-slate-600',
};

const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  if ((status || '').toLowerCase() === 'active') {
    return <span className="inline-flex items-center rounded-md bg-emerald-50 px-2.5 py-1 text-[11px] font-bold tracking-wider text-emerald-600 border border-emerald-200">Active</span>;
  }
  return <span className="inline-flex items-center rounded-md bg-slate-50 px-2.5 py-1 text-[11px] font-bold tracking-wider text-slate-500 border border-slate-200">Inactive</span>;
};

const formatDate = (dateStr: string | null) => {
  if (!dateStr) return '-';
  const parts = dateStr.split('-');
  if (parts.length !== 3) return dateStr;
  const date = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]));
  const day = String(date.getDate()).padStart(2, '0');
  const month = date.toLocaleString('en-US', { month: 'short' });
  const year = date.getFullYear();
  return `${day} ${month} ${year}`;
};

const LoadingSpinner: React.FC = () => (
  <div className="flex min-h-28 items-center justify-center" role="status" aria-label="Loading">
    <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-500 border-t-transparent" />
  </div>
);

const Pagination: React.FC<{
  page: number;
  totalPages: number;
  totalItems: number;
  limit: number;
  setPage: (page: number) => void;
  setLimit: (limit: number) => void;
}> = ({ page, totalPages, totalItems, limit, setPage, setLimit }) => {
  const startItem = totalItems === 0 ? 0 : (page - 1) * limit + 1;
  const endItem = Math.min(page * limit, totalItems);
  const pages = totalPages <= 7
    ? Array.from({ length: totalPages }, (_, index) => index + 1)
    : page <= 4
      ? [1, 2, 3, 4, 5, '...', totalPages]
      : page >= totalPages - 3
        ? [1, '...', totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1, totalPages]
        : [1, '...', page - 1, page, page + 1, '...', totalPages];

  return (
    <div className="mt-8 flex flex-col items-center justify-between gap-4 border-t border-slate-200 pt-5 text-sm text-slate-500 sm:flex-row">
      <div>Showing {startItem} to {endItem} of {totalItems} members</div>
      <div className="flex items-center gap-3">
        <select value={limit} onChange={(e) => { setLimit(Number(e.target.value)); setPage(1); }} className="rounded-md border border-slate-300 py-1.5 pl-3 pr-8 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500">
          <option value={12}>12</option>
          <option value={20}>20</option>
          <option value={50}>50</option>
          <option value={100}>100</option>
        </select>
        <div className="flex items-center gap-1">
          <PaginationArrow direction="prev" disabled={page === 1} onClick={() => setPage(page - 1)} />
          {pages.map((visiblePage, index) => visiblePage === '...' ? (
            <span key={`ellipsis-${index}`} className="flex h-8 w-8 items-center justify-center text-slate-400">...</span>
          ) : (
            <button key={visiblePage} onClick={() => setPage(visiblePage as number)} className={`flex h-8 w-8 items-center justify-center rounded text-sm font-semibold transition ${visiblePage === page ? 'bg-blue-500 text-white shadow-sm' : 'text-slate-600 hover:bg-slate-100'}`}>{visiblePage}</button>
          ))}
          <PaginationArrow direction="next" disabled={page === totalPages} onClick={() => setPage(page + 1)} />
        </div>
      </div>
    </div>
  );
};



const applyDatePreset = (preset: string) => {
  const today = new Date();
  let start = new Date(today);
  let end = new Date(today);

  switch (preset) {
    case 'Today':
      break;
    case 'Yesterday':
      start.setDate(today.getDate() - 1);
      end = new Date(start);
      break;
    case 'Last 7 days':
      start.setDate(today.getDate() - 6);
      break;
    case 'Last week':
      start.setDate(today.getDate() - 13);
      end.setDate(today.getDate() - 7);
      break;
    case 'Last 2 weeks':
      start.setDate(today.getDate() - 13);
      break;
    case 'This month':
      start = new Date(today.getFullYear(), today.getMonth(), 1);
      end = new Date(today.getFullYear(), today.getMonth() + 1, 0);
      break;
    case 'Last month':
      start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
      end = new Date(today.getFullYear(), today.getMonth(), 0);
      break;
  }
  return {
    start: start.toISOString().split('T')[0],
    end: end.toISOString().split('T')[0]
  };
};

const MemberProfileView: React.FC<{ member: Member }> = ({ member }) => {
  const initialRange = applyDatePreset('Last 7 days');
  const [datePreset, setDatePreset] = useState('Last 7 days');
  const [startDate, setStartDate] = useState(initialRange.start);
  const [endDate, setEndDate] = useState(initialRange.end);
  const [dateFilterOpen, setDateFilterOpen] = useState(false);
  const [appUsageOpen, setAppUsageOpen] = useState(true);
  const [urlUsageOpen, setUrlUsageOpen] = useState(true);

  const { data, isLoading, isFetching } = useGetMemberDetailsQuery({
    id: member.id,
    start_date: startDate || undefined,
    end_date: endDate || undefined,
  });

  const memberDetails = data?.member || member;
  const showLoader = isLoading && !data;

  return (
    <div className="w-full space-y-6 pb-20">
      
      {/* Date Filter */}
      <div className="flex justify-end relative">
        <button
          type="button"
          onClick={() => setDateFilterOpen(!dateFilterOpen)}
          className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-bold text-[#0ea5e9] shadow-sm transition hover:bg-slate-50 hover:text-[#0ea5e9]"
        >
          <span>
            {datePreset === 'All Time'
              ? 'All Time'
              : datePreset === 'Custom'
                ? `${formatDate(startDate)} - ${formatDate(endDate)}`
                : datePreset}
          </span>
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
        </button>
        
        {dateFilterOpen && (
          <>
            <div className="fixed inset-0 z-10" onClick={() => setDateFilterOpen(false)} />
            <div className="absolute right-0 top-12 z-20 flex flex-col sm:flex-row w-[calc(100vw-2rem)] sm:w-[480px] overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl animate-in fade-in slide-in-from-top-2 max-w-sm sm:max-w-none">
              <div className="w-full sm:w-1/3 border-b sm:border-b-0 sm:border-r border-slate-100 bg-slate-50 p-2 space-y-1 overflow-x-auto sm:overflow-visible flex sm:block gap-2">
                {['All Time', 'Today', 'Yesterday', 'Last 7 days', 'Last week', 'Last 2 weeks', 'This month', 'Last month'].map(preset => (
                  <button
                    key={preset}
                    onClick={() => {
                      setDatePreset(preset);
                      if (preset === 'All Time') {
                        setStartDate('');
                        setEndDate('');
                      } else {
                        const { start, end } = applyDatePreset(preset);
                        setStartDate(start);
                        setEndDate(end);
                      }
                    }}
                    className={`shrink-0 w-auto sm:w-full rounded-md px-3 py-2 text-left text-xs font-semibold transition ${datePreset === preset ? 'bg-white border border-slate-200 text-[#0ea5e9] shadow-sm' : 'text-slate-600 hover:bg-slate-200/50'}`}
                  >
                    {preset}
                  </button>
                ))}
              </div>
              <div className="w-full sm:w-2/3 p-4 flex flex-col bg-white">
                <h4 className="mb-4 text-sm font-bold text-slate-800">Custom Range</h4>
                <div className="space-y-4">
                  <div>
                    <label className="mb-1 block text-xs font-semibold text-slate-500">Start Date</label>
                    <input 
                      type="date"
                      value={startDate}
                      onChange={(e) => {
                        setStartDate(e.target.value);
                        setDatePreset('Custom');
                      }}
                      className="w-full rounded-md border border-slate-200 px-3 py-1.5 text-sm outline-none focus:border-[#38bdf8] focus:ring-1 focus:ring-[#38bdf8]"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs font-semibold text-slate-500">End Date</label>
                    <input 
                      type="date"
                      value={endDate}
                      onChange={(e) => {
                        setEndDate(e.target.value);
                        setDatePreset('Custom');
                      }}
                      className="w-full rounded-md border border-slate-200 px-3 py-1.5 text-sm outline-none focus:border-[#38bdf8] focus:ring-1 focus:ring-[#38bdf8]"
                    />
                  </div>
                </div>
                <div className="mt-6 flex justify-end gap-2">
                  <button 
                    onClick={() => {
                      setStartDate('');
                      setEndDate('');
                      setDatePreset('All Time');
                      setDateFilterOpen(false);
                    }} 
                    className="rounded-md px-4 py-2 text-xs font-bold text-slate-500 hover:bg-slate-100 transition"
                  >
                    Clear
                  </button>
                  <button onClick={() => setDateFilterOpen(false)} className="rounded-md bg-[#38bdf8] px-4 py-2 text-xs font-bold text-white hover:bg-[#0284c7] transition shadow-sm">
                    Apply
                  </button>
                </div>
              </div>
            </div>
          </>
        )}
      </div>

      <div className="flex flex-col xl:flex-row gap-6 relative">
        {isFetching && !showLoader && (
           <div className="absolute top-0 right-0 z-10 p-2">
             <InlineRefreshIndicator active={true} />
           </div>
        )}

        {/* Left Column: Profile Card */}
        <div className="w-full xl:w-1/3 space-y-6">
          <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm text-center sticky top-6">
            <div className={`mx-auto flex h-24 w-24 items-center justify-center rounded-2xl text-3xl font-black text-white shadow-md ${GRADIENT_CYAN_PURPLE}`}>
              {(memberDetails.name || 'U').substring(0, 2).toUpperCase()}
            </div>
            <h2 className="mt-5 text-2xl font-black text-slate-800">{memberDetails.name}</h2>
            <p className="text-sm font-semibold text-slate-500">{memberDetails.designation || memberDetails.role}</p>
            
            <div className="mt-5 flex justify-center gap-2">
              <span className={`inline-flex items-center rounded-md px-2.5 py-1 text-[11px] font-bold tracking-wider uppercase border ${memberDetails.status === 'active' ? 'bg-emerald-50 text-emerald-600 border-emerald-200' : 'bg-rose-50 text-rose-600 border-rose-200'}`}>
                {memberDetails.status}
              </span>
              <span className="inline-flex items-center rounded-md bg-slate-100 px-2.5 py-1 text-[11px] font-bold tracking-wider text-slate-600 border border-slate-200 uppercase">
                {memberDetails.role}
              </span>
            </div>

            <div className="mt-8 divide-y divide-slate-100 border-t border-slate-100 text-left">
              <div className="py-3 flex justify-between items-center">
                <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Email</span>
                <span className="text-sm font-semibold text-slate-700">{memberDetails.email}</span>
              </div>
              <div className="py-3 flex justify-between items-center">
                <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Date of Joining</span>
                <span className="text-sm font-semibold text-slate-700">{formatDate(memberDetails.date_of_joining)}</span>
              </div>
              <div className="py-3 flex justify-between items-center">
                <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Date of Birth</span>
                <span className="text-sm font-semibold text-slate-700">{formatDate(memberDetails.date_of_birth)}</span>
              </div>
              {memberDetails.organization && (
                <div className="py-3 flex justify-between items-center">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Organization</span>
                  <span className="text-sm font-semibold text-slate-700">{memberDetails.organization.name}</span>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Right Column: API Data Tables */}
        <div className="w-full xl:w-2/3 space-y-6">
          {showLoader ? (
            <div className="flex justify-center p-20">
              <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-500 border-t-transparent"></div>
            </div>
          ) : (
            <>
              {/* Daily Activity */}
           <div className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden">
  <div className="border-b border-slate-100 bg-slate-50 px-6 py-4">
    <h3 className="text-sm font-bold text-slate-800">Daily Activity</h3>
  </div>

  <div className="max-h-[500px] overflow-auto">
    <table className="w-full text-left text-sm whitespace-nowrap">
      <thead className="border-b border-slate-100 text-[11px] font-bold uppercase tracking-wider text-slate-400">
        <tr>
          <th className="sticky top-0 z-10 bg-white px-6 py-3">Date</th>
          <th className="sticky top-0 z-10 bg-white px-6 py-3 text-right">
            Activity %
          </th>
          <th className="sticky top-0 z-10 bg-white px-6 py-3 text-right">
            Keystrokes
          </th>
          <th className="sticky top-0 z-10 bg-white px-6 py-3 text-right">
            Mouse Clicks
          </th>
          <th className="sticky top-0 z-10 bg-white px-6 py-3 text-right">
            Mouse Moves
          </th>
        </tr>
      </thead>

      <tbody className="divide-y divide-slate-100">
        {data?.daily_activity?.length === 0 && (
          <tr>
            <td
              colSpan={5}
              className="px-6 py-8 text-center font-medium text-slate-500"
            >
              No activity data found.
            </td>
          </tr>
        )}

        {data?.daily_activity?.map((act, i) => (
          <tr key={i} className="transition hover:bg-slate-50/50">
            <td className="px-6 py-4 font-semibold text-slate-700">
              {formatDate(act.date)}
            </td>

            <td className="px-6 py-4 text-right">
              <span
                className={`inline-flex items-center justify-center rounded-md px-2 py-1 text-xs font-bold ${
                  act.activity_percentage >= 50
                    ? 'bg-emerald-50 text-emerald-600'
                    : 'bg-rose-50 text-rose-600'
                }`}
              >
                {act.activity_percentage}%
              </span>
            </td>

            <td className="px-6 py-4 text-right font-medium text-slate-600">
              {act.keyboard_strokes.toLocaleString()}
            </td>

            <td className="px-6 py-4 text-right font-medium text-slate-600">
              {act.mouse_clicks.toLocaleString()}
            </td>

            <td className="px-6 py-4 text-right font-medium text-slate-600">
              {act.mouse_movements.toLocaleString()}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  </div>
</div>


              {/* Application Usage */}
              <div className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden flex flex-col max-h-[500px]">
                <div 
                  className="border-b border-slate-100 bg-slate-50 px-6 py-4 flex items-center justify-between cursor-pointer hover:bg-slate-100 transition sticky top-0 z-20"
                  onClick={() => setAppUsageOpen(!appUsageOpen)}
                >
                  <h3 className="text-sm font-bold text-slate-800">Application Usage</h3>
                  <svg className={`h-5 w-5 text-slate-400 transition-transform ${appUsageOpen ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </div>
                {appUsageOpen && (
                  <div className="overflow-auto flex-1">
                    <table className="w-full text-left text-sm whitespace-nowrap">
                      <thead className="sticky top-0 z-10 bg-slate-50 shadow-[0_1px_0_0_#f1f5f9] text-[11px] font-bold uppercase tracking-wider text-slate-400">
                      <tr>
                        <th className="px-6 py-3">Date</th>
                        <th className="px-6 py-3">Application</th>
                        <th className="px-6 py-3 text-right">Duration</th>
                        <th className="px-6 py-3 text-right">Usage %</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {data?.application_usage?.length === 0 && (
                        <tr><td colSpan={4} className="px-6 py-8 text-center text-slate-500 font-medium">No application data found.</td></tr>
                      )}
                      {data?.application_usage?.map((usage, i) => (
                        <React.Fragment key={i}>
                          {usage.applications.map((app, j) => (
                            <tr key={`${i}-${j}`} className="hover:bg-slate-50/50 transition">
                              {j === 0 && (
                                <td className="px-6 py-4 font-semibold text-slate-700 align-top" rowSpan={usage.applications.length}>
                                  {formatDate(usage.date)}
                                </td>
                              )}
                              <td className="px-6 py-4 font-semibold text-slate-800">
                                <div className="flex items-center gap-2">
                                  <div className="h-2 w-2 rounded-full bg-blue-500"></div>
                                  <AppIcon name={app.application_name} size={20} />
                                  {app.application_name}
                                </div>
                              </td>
                              <td className="px-6 py-4 text-right font-medium text-slate-600">{app.duration}</td>
                              <td className="px-6 py-4 text-right">
                                <div className="flex items-center justify-end gap-2">
                                  <div className="h-1.5 w-16 bg-slate-100 rounded-full overflow-hidden">
                                    <div className="h-full bg-[#0ea5e9] rounded-full" style={{ width: `${app.usage_percentage}%` }}></div>
                                  </div>
                                  <span className="text-xs font-bold text-slate-500 w-8">{app.usage_percentage}%</span>
                                </div>
                              </td>
                            </tr>
                          ))}
                        </React.Fragment>
                      ))}
                    </tbody>
                  </table>
                  </div>
                )}
              </div>

              {/* URL Usage */}
              <div className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden flex flex-col max-h-[500px]">
                <div 
                  className="border-b border-slate-100 bg-slate-50 px-6 py-4 flex items-center justify-between cursor-pointer hover:bg-slate-100 transition sticky top-0 z-20"
                  onClick={() => setUrlUsageOpen(!urlUsageOpen)}
                >
                  <h3 className="text-sm font-bold text-slate-800">Website & URL Usage</h3>
                  <svg className={`h-5 w-5 text-slate-400 transition-transform ${urlUsageOpen ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </div>
                {urlUsageOpen && (
                  <div className="overflow-auto flex-1">
                    <table className="w-full text-left text-sm whitespace-nowrap">
                      <thead className="sticky top-0 z-10 bg-slate-50 shadow-[0_1px_0_0_#f1f5f9] text-[11px] font-bold uppercase tracking-wider text-slate-400">
                      <tr>
                        <th className="px-6 py-3">Date</th>
                        <th className="px-6 py-3">Browser</th>
                        <th className="px-6 py-3">URL / Domain</th>
                        <th className="px-6 py-3 text-right">Duration</th>
                        <th className="px-6 py-3 text-right">Usage %</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {data?.url_usage?.length === 0 && (
                        <tr><td colSpan={5} className="px-6 py-8 text-center text-slate-500 font-medium">No URL data found.</td></tr>
                      )}
                      {data?.url_usage?.map((usage, i) => (
                        <React.Fragment key={i}>
                          {usage.urls.map((url, j) => (
                            <tr key={`${i}-${j}`} className="hover:bg-slate-50/50 transition">
                              {j === 0 && (
                                <td className="px-6 py-4 font-semibold text-slate-700 align-top" rowSpan={usage.urls.length}>
                                  {formatDate(usage.date)}
                                </td>
                              )}
                              <td className="px-6 py-4 font-medium text-slate-500">{url.browser_name}</td>
                              <td className="px-6 py-4">
                                <div className="font-bold text-slate-800 max-w-xs truncate" title={url.page_title}>{url.domain}</div>
                                <div className="text-xs text-slate-400 max-w-xs truncate mt-0.5" title={url.url}>{url.url}</div>
                              </td>
                              <td className="px-6 py-4 text-right font-medium text-slate-600">{url.duration}</td>
                              <td className="px-6 py-4 text-right">
                                <div className="flex items-center justify-end gap-2">
                                  <div className="h-1.5 w-16 bg-slate-100 rounded-full overflow-hidden">
                                    <div className="h-full bg-purple-500 rounded-full" style={{ width: `${url.usage_percentage}%` }}></div>
                                  </div>
                                  <span className="text-xs font-bold text-slate-500 w-8">{url.usage_percentage}%</span>
                                </div>
                              </td>
                            </tr>
                          ))}
                        </React.Fragment>
                      ))}
                    </tbody>
                  </table>
                  </div>
                )}
              </div>

            </>
          )}
        </div>
      </div>
    </div>
  );
};

export const AdminMembers: React.FC = () => {
  const { showToast, confirmAction } = useFeedback();
  const [selectedProfileId, setSelectedProfileId] = useState<number | null>(null);

  const [search, setSearch] = useState('');
  const [searchError, setSearchError] = useState<string | null>(null);
  const [filterRole, setFilterRole] = useState('All');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);

  // One request for the finished search term instead of one per keystroke.
  const debouncedSearch = useDebouncedValue(search);

  /**
   * A rejected search term is held back from the query rather than sent: the
   * box says why, and the last good result set stays on screen instead of the
   * table blanking out behind an error the user cannot see.
   */
  const searchCheck = validateSearchTerm(debouncedSearch, { fieldLabel: 'Search' });
  const searchTerm = searchCheck.ok ? searchCheck.value : '';

  // The set of roles is the server's to define. `/project-management/metadata`
  // returns it, so a role added there reaches both pickers with no frontend
  // change; hardcoding the list here is what hid HR from this page.
  const { data: metadata } = useGetProjectMetadataQuery();
  const roles = metadata?.roles ?? [];

  const { data, isLoading, isFetching, isError } = useGetMembersQuery({
    page,
    limit: pageSize,
    role: filterRole,
    status: 'All',
    search: searchTerm,
  });

  // Only block on the very first load. Once rows are on screen a refetch runs
  // behind them, and mutations are applied to the cache optimistically, so
  // there is nothing left to wait for.
  const showFirstLoad = isLoading && !data;
  const isRevalidating = isFetching && !showFirstLoad;
  
  // Who may *change* the directory, as opposed to read it. The backend gates
  // create/update/deactivate on `manage_employees` (app/api/members.py), and HR
  // deliberately holds `view_employees` without it: HR sees every member's
  // details and gets no write affordance, rather than a button that 403s.
  const { currentUser } = useAuth();
  const canManageMembers = !!currentUser?.permissions?.['manage_employees'];

  const [createMember] = useCreateMemberMutation();
  const [updateMember, { isLoading: isUpdatingMember }] = useUpdateMemberMutation();
  const [deleteMember, { isLoading: isDeletingMember }] = useDeleteMemberMutation();

  // Drawer state
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [drawerMode, setDrawerMode] = useState<'create' | 'edit'>('create');
  const [editingId, setEditingId] = useState<number | null>(null);

  // Form state
  const [formName, setFormName] = useState('');
  const [formEmail, setFormEmail] = useState('');
  const [formRole, setFormRole] = useState('employee');
  const [formStatus, setFormStatus] = useState('active');
  const [formDOJ, setFormDOJ] = useState('');
  const [formDOB, setFormDOB] = useState('');
  const [formDesignation, setFormDesignation] = useState('');

  // The role list is the server's, so the enum's permitted values are read from
  // it each render rather than hardcoded. A member whose stored role the server
  // no longer lists keeps that role as a valid option, matching the picker
  // below — opening and saving their record must not silently reassign them.
  const roleValues = roles.map((role) => role.value);
  const allowedRoles = formRole && !roleValues.includes(formRole)
    ? [...roleValues, formRole]
    : roleValues;

  const memberForm = useFormValidation({
    name: { rule: 'name', label: 'Employee name', required: true },
    email: { rule: 'email', label: 'Email address', required: true },
    role: { rule: 'enum', label: 'Role', required: true, allowed: allowedRoles },
    status: { rule: 'enum', label: 'Status', required: true, allowed: ['active', 'inactive'] },
    designation: { rule: 'name', label: 'Designation' },
    dateOfJoining: { rule: 'date', label: 'Date of joining' },
    dateOfBirth: { rule: 'date', label: 'Date of birth' },
  });

  const openCreateDrawer = () => {
    setDrawerMode('create');
    setEditingId(null);
    setFormName('');
    setFormEmail('');
    setFormRole('employee');
    setFormStatus('active');
    setFormDOJ('');
    setFormDOB('');
    setFormDesignation('');
    memberForm.clear();
    setIsDrawerOpen(true);
  };

  const openEditDrawer = (member: Member) => {
    setDrawerMode('edit');
    setEditingId(member.id);
    setFormName(member.name);
    setFormEmail(member.email);
    setFormRole(member.role);
    setFormStatus(member.status);
    setFormDOJ(member.date_of_joining || '');
    setFormDOB(member.date_of_birth || '');
    setFormDesignation(member.designation || '');
    memberForm.clear();
    setIsDrawerOpen(true);
  };

  const handleSaveMember = async (e: React.FormEvent) => {
    e.preventDefault();

    // This used to be `if (!formName || !formEmail) return;` — a silent return
    // that left the drawer open with no explanation and no saved member. Every
    // field is checked now, and each failure is shown against its own input.
    const check = memberForm.validateAll({
      name: formName,
      email: formEmail,
      role: formRole,
      status: formStatus,
      designation: formDesignation,
      dateOfJoining: formDOJ,
      dateOfBirth: formDOB,
    });
    if (!check.ok) {
      showToast('Please correct the highlighted fields.', 'error');
      return;
    }

    // The same payload shape as before, built from the normalised values: the
    // name is trimmed and its whitespace collapsed, and the email's domain is
    // lower-cased, exactly as the backend would have done on receipt.
    const payload = {
      name: check.values.name as string,
      email: check.values.email as string,
      role: formRole,
      status: formStatus,
      date_of_joining: (check.values.dateOfJoining as string) || null,
      date_of_birth: (check.values.dateOfBirth as string) || null,
      designation: check.values.designation as string,
    };

    try {
      if (drawerMode === 'create') {
        await createMember(payload).unwrap();
      } else if (drawerMode === 'edit' && editingId) {
        await updateMember({ id: editingId, body: payload }).unwrap();
      }
      setIsDrawerOpen(false);
      showToast(drawerMode === 'create' ? 'Member created successfully.' : 'Member updated successfully.', 'success');
    } catch (err) {
      console.error('Failed to save member', err);
      showToast('Unable to save member. Please try again.', 'error');
    }
  };

  const handleDeleteMember = async (id: number) => {
    if (await confirmAction('Delete member?', 'This member will be permanently removed from the directory.')) {
      try {
        await deleteMember(id).unwrap();
        showToast('Member deleted successfully.', 'success');
      } catch (err) {
        console.error('Failed to delete member', err);
        showToast('Unable to delete member. Please try again.', 'error');
      }
    }
  };

  const filteredItems = useMemo(() => {
    return data?.items || [];
  }, [data?.items]);

  const totalPages = data?.pages || 1;


  const selectedMember = selectedProfileId ? data?.items?.find(m => m.id === selectedProfileId) : null;

  if (selectedMember) {
    return (
      <V2Shell
        title="Member Profile"
        subtitle="View detailed activity, assigned projects, and statistics."
        actions={
          <button
            onClick={() => setSelectedProfileId(null)}
            className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm font-bold text-slate-700 shadow-sm transition hover:bg-slate-50"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M15 19l-7-7 7-7"/></svg>
            Back to Directory
          </button>
        }
      >
        <MemberProfileView member={selectedMember} />
      </V2Shell>
    );
  }

  return (
    <V2Shell
      title="Members Directory"
      subtitle={canManageMembers
        ? "Manage employees, their roles, and company details."
        : "View employees, their roles, and company details."}
      actions={
        canManageMembers ? (
          <div className="flex items-center gap-3">
            <InlineRefreshIndicator active={isRevalidating || isUpdatingMember || isDeletingMember} />
            <button
              onClick={openCreateDrawer}
              className={`rounded-lg px-4 py-2 text-sm font-bold text-white shadow-md transition hover:opacity-90 ${GRADIENT_CYAN_PURPLE}`}
            >
              + Add Member
            </button>
          </div>
        ) : undefined
      }
    >
      <div className="w-full px-4 sm:px-6 lg:px-8 pt-6 space-y-6 pb-20">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex flex-1 items-center gap-2 px-2">
            <svg className="h-5 w-5 text-slate-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input
              type="text"
              placeholder="Search members by name or email..."
              value={search}
              maxLength={SEARCH_MAX_LENGTH}
              aria-invalid={searchError ? true : undefined}
              aria-describedby={searchError ? 'member-search-error' : undefined}
              onChange={(e) => {
                const next = e.target.value;
                setSearch(next);
                const result = validateSearchTerm(next, { fieldLabel: 'Search' });
                setSearchError(result.ok ? null : result.error);
                setPage(1);
              }}
              className="flex-1 bg-transparent text-sm outline-none placeholder:text-slate-400 text-slate-700"
            />
          </div>
          {searchError && (
            <div className="px-2">
              <FieldError id="member-search-error" message={searchError} />
            </div>
          )}
          
          <div className="h-8 w-px bg-slate-200 hidden lg:block"></div>

          <div className="flex items-center gap-3 pr-2">
            <span className="text-[11px] font-bold text-slate-500 uppercase tracking-wider">ROLE:</span>
            <div className="relative">
              <select
                value={filterRole}
                onChange={(e) => { setFilterRole(e.target.value); setPage(1); }}
                className="appearance-none rounded-lg border border-slate-200 bg-white py-2 pl-3 pr-9 text-sm font-semibold text-slate-700 outline-none shadow-sm transition hover:bg-slate-50 focus:border-[#38bdf8] focus:ring-2 focus:ring-[#38bdf8]/15"
              >
                <option value="All">All Roles</option>
                {roles.map(role => (
                  <option key={role.id} value={role.value}>{role.role_type}</option>
                ))}
              </select>
              <svg
                className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-600"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth="2.5"
                aria-hidden="true"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 9l6 6 6-6" />
              </svg>
            </div>
          </div>
        </div>

        <div className="relative overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="overflow-x-auto pb-4">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-50 text-slate-500 border-b border-slate-200">
                <tr>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Employee</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Role</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Status</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Designation</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Date of Joining</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Date of Birth</th>
                  {canManageMembers && (
                    <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px] text-right">Action</th>
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {showFirstLoad ? (
                  <tr>
                    <td colSpan={canManageMembers ? 7 : 6} className="px-6 py-8">
                      <LoadingSpinner />
                    </td>
                  </tr>
                ) : isError ? (
                  <tr>
                    <td colSpan={canManageMembers ? 7 : 6} className="px-6 py-12 text-center text-red-500">
                      Failed to fetch members. Please try again.
                    </td>
                  </tr>
                ) : filteredItems.length > 0 ? (
                  filteredItems.map(member => (
                    <tr key={member.id} className="transition hover:bg-slate-50/50">
                      <td className="px-6 py-4">
                        <div className="flex items-center gap-3">
                          <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-xs font-bold text-white shadow-sm ${GRADIENT_CYAN_PURPLE}`}>
                            {(member.name || 'U').substring(0, 2).toUpperCase()}
                          </div>
                          <div className="min-w-0">
                            <div className="font-bold text-slate-800 truncate cursor-pointer hover:text-blue-600 hover:underline transition" onClick={() => setSelectedProfileId(member.id)}>{member.name || '-'}</div>
                            <div className="text-xs text-slate-500 truncate mt-0.5">{member.email || '-'}</div>
                          </div>
                        </div>
                      </td>
                      <td className="px-6 py-4">
                        <span className={`inline-flex items-center rounded bg-slate-100 px-2 py-0.5 text-xs font-semibold ${
                          ROLE_TONES[(member.role || '').toLowerCase()] || 'text-slate-600'
                        }`}>
                          {roles.find(role => role.value === (member.role || '').toLowerCase())?.role_type
                            || (member.role || '').charAt(0).toUpperCase() + (member.role || '').slice(1)
                            || '-'}
                        </span>
                      </td>
                      <td className="px-6 py-4">
                        <StatusBadge status={member.status} />
                      </td>
                      <td className="px-6 py-4 font-medium text-slate-600">{member.designation || '-'}</td>
                      <td className="px-6 py-4 font-medium text-slate-600">{formatDate(member.date_of_joining)}</td>
                      <td className="px-6 py-4 font-medium text-slate-600">{formatDate(member.date_of_birth)}</td>
                      {canManageMembers && (
                        <td className="px-6 py-4 text-right">
                          <div className="flex items-center justify-end gap-2">
                            <button
                              onClick={() => openEditDrawer(member)}
                              className="rounded px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-[#14B8A6] border border-[#14B8A6]/30 transition hover:bg-[#14B8A6]/10"
                            >
                              Edit
                            </button>
                            <button
                              onClick={() => handleDeleteMember(member.id)}
                              className="rounded px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-rose-500 border border-rose-200 transition hover:bg-rose-50"
                            >
                              Delete
                            </button>
                          </div>
                        </td>
                      )}
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={canManageMembers ? 7 : 6} className="px-6 py-12 text-center text-slate-500">
                      No members found matching your criteria.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          
        </div>
        {totalPages > 1 && (
          <Pagination page={page} totalPages={totalPages} totalItems={data?.total || 0} limit={pageSize} setPage={setPage} setLimit={setPageSize} />
        )}
      </div>

      {/* Right Slide-over Drawer for Create / Edit */}
      <div className={`fixed inset-0 z-50 overflow-hidden ${isDrawerOpen ? 'pointer-events-auto' : 'pointer-events-none'}`}>
        <div 
          className={`absolute inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity duration-300 ${isDrawerOpen ? 'opacity-100' : 'opacity-0'}`} 
          onClick={() => setIsDrawerOpen(false)} 
        />
        <div className={`absolute inset-y-0 right-0 w-full max-w-md bg-white shadow-2xl transition-transform duration-300 ease-in-out ${isDrawerOpen ? 'translate-x-0' : 'translate-x-full'}`}>
          <div className="flex h-full flex-col">
            <div className="flex items-center justify-between border-b border-slate-100 px-6 py-5">
              <h3 className="text-lg font-bold text-slate-800">
                {drawerMode === 'create' ? 'Add New Member' : 'Edit Member'}
              </h3>
              <button type="button" onClick={() => setIsDrawerOpen(false)} className="text-slate-400 hover:text-slate-600">
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            
            <div className="flex-1 overflow-y-auto">
              <form id="member-form" onSubmit={handleSaveMember} className="p-6 space-y-6">
                <div>
                  <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Employee Name</label>
                  <input
                    required
                    type="text"
                    value={formName}
                    onChange={e => setFormName(e.target.value)}
                    onBlur={() => memberForm.validateField('name', formName)}
                    {...memberForm.fieldProps('name')}
                    className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium"
                    placeholder="E.g. John Doe"
                  />
                  <FieldError id={memberForm.errorId('name')} message={memberForm.errors.name} />
                </div>

                <div>
                  <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Email Address</label>
                  <input
                    required
                    type="email"
                    value={formEmail}
                    onChange={e => setFormEmail(e.target.value)}
                    onBlur={() => memberForm.validateField('email', formEmail)}
                    {...memberForm.fieldProps('email')}
                    className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium"
                    placeholder="john.doe@company.com"
                  />
                  <FieldError id={memberForm.errorId('email')} message={memberForm.errors.email} />
                </div>
                
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Role</label>
                    <select
                      required
                      value={formRole}
                      onChange={e => setFormRole(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium bg-white"
                    >
                      {/* The member's stored role is kept as an option even
                          when the server no longer lists it, so opening and
                          saving their record cannot silently reassign them. */}
                      {formRole && !roles.some(role => role.value === formRole) && (
                        <option value={formRole}>
                          {formRole.charAt(0).toUpperCase() + formRole.slice(1)}
                        </option>
                      )}
                      {roles.map(role => (
                        <option key={role.id} value={role.value}>{role.role_type}</option>
                      ))}
                    </select>
                    <FieldError id={memberForm.errorId('role')} message={memberForm.errors.role} />
                  </div>
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Status</label>
                    <select
                      required
                      value={formStatus}
                      onChange={e => setFormStatus(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium bg-white"
                    >
                      <option value="active">Active</option>
                      <option value="inactive">Inactive</option>
                    </select>
                    <FieldError id={memberForm.errorId('status')} message={memberForm.errors.status} />
                  </div>
                </div>

                <div className="grid grid-cols-1 gap-4 pt-2 border-t border-slate-100">
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Designation</label>
                    <input
                      type="text"
                      value={formDesignation}
                      onChange={e => setFormDesignation(e.target.value)}
                      onBlur={() => memberForm.validateField('designation', formDesignation)}
                      {...memberForm.fieldProps('designation')}
                      className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium text-slate-700"
                      placeholder="e.g. Full Stack Developer"
                    />
                    <FieldError
                      id={memberForm.errorId('designation')}
                      message={memberForm.errors.designation}
                    />
                  </div>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-2 border-t border-slate-100">
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Date of Joining</label>
                    <input
                      type="date"
                      value={formDOJ}
                      onChange={e => setFormDOJ(e.target.value)}
                      onBlur={() => memberForm.validateField('dateOfJoining', formDOJ)}
                      {...memberForm.fieldProps('dateOfJoining')}
                      className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium text-slate-700"
                    />
                    <FieldError
                      id={memberForm.errorId('dateOfJoining')}
                      message={memberForm.errors.dateOfJoining}
                    />
                  </div>
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Date of Birth</label>
                    <input
                      type="date"
                      value={formDOB}
                      onChange={e => setFormDOB(e.target.value)}
                      onBlur={() => memberForm.validateField('dateOfBirth', formDOB)}
                      {...memberForm.fieldProps('dateOfBirth')}
                      className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium text-slate-700"
                    />
                    <FieldError
                      id={memberForm.errorId('dateOfBirth')}
                      message={memberForm.errors.dateOfBirth}
                    />
                  </div>
                </div>
              </form>
            </div>
            
            <div className="border-t border-slate-100 p-6 bg-slate-50">
              <button
                type="submit"
                form="member-form"
                className={`w-full rounded-lg px-6 py-3 text-sm font-bold text-white shadow-md hover:opacity-90 transition-opacity ${GRADIENT_CYAN_PURPLE}`}
              >
                {drawerMode === 'create' ? 'Save Member' : 'Save Changes'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </V2Shell>
  );
};
