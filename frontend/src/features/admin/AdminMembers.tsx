import React, { useState, useMemo } from 'react';
import { V2Shell } from '../dashboard/v2/V2Shell';
import { 
  useGetMembersQuery, 
  useGetMemberAccessSummaryQuery,
  useCreateMemberMutation, 
  useUpdateMemberMutation, 
  useUpdateMemberAccessMutation,
  useLazyGetMembersQuery,
  useDeleteMemberMutation,
  useGetMemberDetailsQuery,
} from '../../store/api/membersApi';
import type { Member } from '../../store/api/membersApi';
import { useGetProjectMetadataQuery } from '../../store/api/projectsApi';
import { isTeamScoped } from '../../utils/roles';
import { MemberLogModal } from './MemberLogModal';
import { useFeedback } from '../../components/FeedbackProvider';
import { InlineRefreshIndicator } from '../../components/InlineRefreshIndicator';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { Pagination } from '../../components/Pagination';
import { useAuth } from '../auth/authContext';
import { DateRangeFilter, DEFAULT_RANGE, type DateRange } from '../dashboard/v2/filters';
import { AppIcon } from "../../components/AppIcon";
import { FieldError, SEARCH_MAX_LENGTH, useFormValidation, validateSearchTerm } from '../../validation';

/**
 * How often the Add Task / Login headcounts are re-read while the page is in
 * view. The counts also refresh at once after any change made here, when the
 * tab regains focus and when it is opened; polling is what carries a change
 * another administrator made. It pauses while the tab is in the background.
 */
const ACCESS_SUMMARY_POLL_MS = 15_000;

const GRADIENT_CYAN_PURPLE = 'bg-gradient-to-r from-[#0ea5e9] via-[#3b82f6] to-[#8b5cf6]';

/** "(20)" beside a column title: how many active members hold that permission. */
const HeaderCount: React.FC<{ value: number | undefined; meaning: string }> = ({ value, meaning }) =>
  typeof value === 'number' ? (
    <span
      className="ml-1.5 tabular-nums text-slate-700"
      title={`${value} active ${value === 1 ? 'member is' : 'members are'} ${meaning}`}
    >
      ({value})
    </span>
  ) : null;

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
  // Not a staff role: an external client account, listed beside the team of
  // the projects it was given access to.
  client: 'text-teal-600',
};

const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  if ((status || '').toLowerCase() === 'active') {
    return <span className="inline-flex items-center rounded-md bg-emerald-50 px-2.5 py-1 text-[11px] font-bold tracking-wider text-emerald-600 border border-emerald-200">Active</span>;
  }
  return <span className="inline-flex items-center rounded-md bg-slate-50 px-2.5 py-1 text-[11px] font-bold tracking-wider text-slate-500 border border-slate-200">Inactive</span>;
};

/**
 * The Members directory's Allow / Not allow switch for adding tasks.
 *
 * Every member may add tasks by default. Switching one off withdraws task
 * creation from that person everywhere -- the desktop's Add Task, this
 * client's task listing and the WFPM integration -- because the backend
 * enforces it on every task-create route (`users.can_add_tasks`, read by
 * `require_permission`). The role is untouched: their `tasks:create` stays
 * in the permission map, and turning the switch back on restores creation
 * without any other change.
 *
 * Editable by whoever holds `manage_member_access` (administrators and HR);
 * everyone else sees the state rather than a button that 403s.
 */
const AddTaskSwitch: React.FC<{
  allowed: boolean;
  editable: boolean;
  busy: boolean;
  onChange: (allowed: boolean) => void;
  /** What the switch governs, for its accessible label and tooltip. */
  subject?: string;
  /** The same, as the thing being allowed ("to add tasks"). */
  allowPhrase?: string;
  /** Shown instead of a working switch, e.g. on the administrator's own row. */
  lockedReason?: string;
}> = ({ allowed, editable, busy, onChange, subject = 'adding tasks', allowPhrase = 'to add tasks', lockedReason }) => {
  const pill = allowed
    ? <span className="inline-flex items-center rounded-md bg-emerald-50 px-2.5 py-1 text-[11px] font-bold tracking-wider text-emerald-600 border border-emerald-200">Allowed</span>
    : <span className="inline-flex items-center rounded-md bg-rose-50 px-2.5 py-1 text-[11px] font-bold tracking-wider text-rose-500 border border-rose-200">Excluded</span>;
  if (!editable) return pill;
  return (
    <div className="flex items-center gap-3">
      {pill}
      <button
        type="button"
        role="switch"
        aria-checked={allowed}
        aria-label={allowed ? `Exclude this member from ${subject}` : `Allow this member ${allowPhrase}`}
        title={lockedReason ?? (allowed ? `Click to exclude this member from ${subject}` : `Click to allow this member ${subject}`)}
        disabled={busy || !!lockedReason}
        onClick={() => onChange(!allowed)}
        className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500/40 disabled:cursor-not-allowed disabled:opacity-60 ${
          allowed ? 'bg-blue-600' : 'bg-slate-300'
        }`}
      >
        <span
          className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
            allowed ? 'translate-x-5' : 'translate-x-0.5'
          }`}
        />
      </button>
    </div>
  );
};

/** The server's reason when it gave one (a string `detail`), else `fallback`. */
const accessErrorMessage = (err: unknown, fallback: string): string => {
  const detail = (err as { data?: { detail?: unknown } } | null)?.data?.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
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

const MemberProfileView: React.FC<{ member: Member }> = ({ member }) => {
  const [dateRange, setDateRange] = useState<DateRange>(DEFAULT_RANGE);
  const startDate = dateRange.from;
  const endDate = dateRange.to;
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
      <div className="flex justify-end">
        <DateRangeFilter value={dateRange} onChange={setDateRange} />
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
  const [logMember, setLogMember] = useState<{ id: number; name: string } | null>(null);

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

  // The headcounts beside Add Task and Login. Deliberately not derived from
  // `data` above: that is one page, filtered by search and role, and these
  // numbers must not move when the table is narrowed. An error or a first load
  // shows no number at all rather than a made-up zero.
  const { data: accessSummary } = useGetMemberAccessSummaryQuery(undefined, {
    pollingInterval: ACCESS_SUMMARY_POLL_MS,
    skipPollingIfUnfocused: true,
    refetchOnMountOrArgChange: true,
  });

  // Only block on the very first load. Once rows are on screen a refetch runs
  // behind them, and mutations are applied to the cache optimistically, so
  // there is nothing left to wait for.
  const showFirstLoad = isLoading && !data;
  const isRevalidating = isFetching && !showFirstLoad;
  
  // Who may *change* the directory, as opposed to read it. The backend gates
  // create/update/delete on `manage_employees` (app/api/members.py), and HR
  // deliberately holds `view_employees` without it: HR sees every member's
  // details and gets no write affordance, rather than a button that 403s.
  const { currentUser } = useAuth();
  const canManageMembers = !!currentUser?.permissions?.['manage_employees'];
  // The two Members-directory switches (sign-in, Add Task) are a narrower
  // right than editing a member: HR holds this one and not the one above.
  const canManageAccess = !!currentUser?.permissions?.['manage_member_access'];

  const [createMember] = useCreateMemberMutation();
  const [updateMember, { isLoading: isUpdatingMember }] = useUpdateMemberMutation();
  const [updateMemberAccess, { isLoading: isUpdatingAccess }] = useUpdateMemberAccessMutation();
  const [fetchMembersPage] = useLazyGetMembersQuery();
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

  // The row flips at once (`updateMember` patches every cached list
  // optimistically) and is rolled back with a toast if the server refuses.
  const [pendingAddTaskId, setPendingAddTaskId] = useState<number | null>(null);
  const handleSetAddTask = async (member: Member, allowed: boolean) => {
    if ((member.can_add_tasks !== false) === allowed) return;
    setPendingAddTaskId(member.id);
    try {
      const result = await updateMemberAccess({ member_ids: [member.id], can_add_tasks: allowed }).unwrap();
      if (result.failed.length) throw new Error(result.failed[0].detail);
      showToast(
        allowed
          ? `${member.name} is now allowed to add tasks.`
          : `${member.name} is now excluded from adding tasks.`,
        'success',
      );
    } catch (err) {
      console.error('Failed to update the add-task permission', err);
      showToast(accessErrorMessage(err, 'Unable to update the add-task permission. Please try again.'), 'error');
    } finally {
      setPendingAddTaskId(null);
    }
  };

  // The Login switch. Excluding signs the member out of the desktop and the
  // web within seconds and stops a running timer, so it asks first; allowing
  // them back is harmless and immediate. The row flips optimistically.
  const [pendingLoginId, setPendingLoginId] = useState<number | null>(null);
  const handleSetLogin = async (member: Member, allowed: boolean) => {
    if ((member.can_login !== false) === allowed) return;
    if (
      !allowed &&
      !(await confirmAction(
        `Exclude ${member.name} from logging in?`,
        'They will be signed out of the desktop app and the website right away, and any running timer will be stopped. They cannot sign in again until you allow them.',
      ))
    ) {
      return;
    }
    setPendingLoginId(member.id);
    try {
      const result = await updateMemberAccess({ member_ids: [member.id], can_login: allowed }).unwrap();
      if (result.failed.length) throw new Error(result.failed[0].detail);
      showToast(
        allowed
          ? `${member.name} is now allowed to log in.`
          : `${member.name} has been excluded and signed out.`,
        'success',
      );
    } catch (err) {
      console.error('Failed to update the login permission', err);
      showToast(accessErrorMessage(err, 'Unable to update the login permission. Please try again.'), 'error');
    } finally {
      setPendingLoginId(null);
    }
  };

  // Multi-select. The ids are held across pages, so a selection can be built up
  // from several; the header box selects or clears only the rows on this page.
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  // The last switches seen for members who may not be on screen (another page,
  // or picked by "select all"). Rows on the current page are read live from the
  // list instead, so this only has to cover the rest.
  const [knownMembers, setKnownMembers] = useState<Record<number, Member>>({});
  const rememberMembers = (members: Member[]) =>
    setKnownMembers((current) => {
      const next = { ...current };
      members.forEach((m) => { next[m.id] = m; });
      return next;
    });
  const toggleSelected = (member: Member) => {
    rememberMembers([member]);
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(member.id)) next.delete(member.id);
      else next.add(member.id);
      return next;
    });
  };

  // "Select all N": the header box only knows the rows on screen, so this walks
  // the same filtered list (role + search) page by page -- the API caps a page
  // at 100 -- and selects every id in it.
  const [isSelectingAll, setIsSelectingAll] = useState(false);
  const selectEveryMember = async () => {
    setIsSelectingAll(true);
    try {
      const ids = new Set<number>();
      const everyone: Member[] = [];
      let pageNumber = 1;
      let pages = 1;
      do {
        // eslint-disable-next-line no-await-in-loop
        const result = await fetchMembersPage(
          { page: pageNumber, limit: 100, role: filterRole, status: 'All', search: searchTerm },
          true,
        ).unwrap();
        result.items.forEach((m) => { ids.add(m.id); everyone.push(m); });
        pages = result.pages || 1;
        pageNumber += 1;
      } while (pageNumber <= pages);
      rememberMembers(everyone);
      setSelectedIds(ids);
    } catch (err) {
      console.error('Failed to select every member', err);
      showToast('Unable to select every member. Please try again.', 'error');
    } finally {
      setIsSelectingAll(false);
    }
  };

  // One request for the whole selection. The server saves each member on its
  // own and reports the ones it refused (your own login, an administrator's
  // access when you are HR, someone outside your scope); those stay selected
  // so the choice is visible and can be retried or cleared.
  const handleBulkAccess = async (switches: { can_login?: boolean; can_add_tasks?: boolean }) => {
    const ids = Array.from(selectedIds);
    if (!ids.length) return;
    if (
      switches.can_login === false &&
      !(await confirmAction(
        `Exclude ${ids.length} member${ids.length === 1 ? '' : 's'} from logging in?`,
        'They will be signed out of the desktop app and the website right away, and any running timer will be stopped. They cannot sign in again until you allow them.',
      ))
    ) {
      return;
    }
    try {
      const result = await updateMemberAccess({ member_ids: ids, ...switches }).unwrap();
      rememberMembers(result.updated);
      const refused = new Set(result.failed.map((f) => f.id));
      setSelectedIds(new Set(ids.filter((id) => refused.has(id))));
      if (!result.failed.length) {
        showToast(`Updated ${result.updated.length} member${result.updated.length === 1 ? '' : 's'}.`, 'success');
      } else {
        const reasons = Array.from(new Set(result.failed.map((f) => f.detail))).join(' ');
        showToast(
          `Updated ${result.updated.length}; ${result.failed.length} could not be changed. ${reasons}`,
          result.updated.length ? 'info' : 'error',
        );
      }
    } catch (err) {
      console.error('Failed to update member access in bulk', err);
      showToast(accessErrorMessage(err, 'Unable to update the selected members. Please try again.'), 'error');
    }
  };

  const handleDeleteMember = async (id: number) => {
    if (
      await confirmAction(
        'Delete member?',
        'This member and everything recorded against them — tracked time, manual time and screenshots — will be permanently deleted. This cannot be undone. To keep their history, set them to Inactive instead.',
      )
    ) {
      try {
        await deleteMember(id).unwrap();
        // A deleted member must not stay selected for a later bulk change.
        setSelectedIds((current) => {
          if (!current.has(id)) return current;
          const next = new Set(current);
          next.delete(id);
          return next;
        });
        showToast('Member deleted successfully.', 'success');
      } catch (err) {
        console.error('Failed to delete member', err);
        // The server says why when it refuses (they lead a project, a timer is running).
        showToast(accessErrorMessage(err, 'Unable to delete member. Please try again.'), 'error');
      }
    }
  };

  const filteredItems = useMemo(() => {
    return data?.items || [];
  }, [data?.items]);

  const totalPages = data?.pages || 1;

  // The header box is about the whole directory (every page of the current
  // role / search filter), not the 20 rows on screen.
  const totalMembers = data?.total ?? 0;
  const allSelected = totalMembers > 0 && selectedIds.size >= totalMembers;
  const someSelected = selectedIds.size > 0 && !allSelected;
  const toggleEveryone = () => {
    if (allSelected) setSelectedIds(new Set());
    else void selectEveryMember();
  };

  // True when pressing Allow (`target` true) or Exclude (`target` false) for
  // this switch would change at least one selected member. A member whose state
  // is not known counts as a change, so the button is never wrongly dead.
  const rowsOnPage = new Map(filteredItems.map((m) => [m.id, m]));
  const wouldChange = (key: 'can_login' | 'can_add_tasks', target: boolean) =>
    Array.from(selectedIds).some((id) => {
      const member = rowsOnPage.get(id) ?? knownMembers[id];
      return !member || (member[key] !== false) !== target;
    });
  // Placeholder rows span every column, including the optional ones.
  const columnCount = 8 + (canManageAccess ? 1 : 0) + (canManageMembers ? 1 : 0);


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
        : isTeamScoped(currentUser)
          // What `GET /members` answers a leader with: the people on the
          // projects they lead, and the clients an administrator shared those
          // projects with.
          ? "View your team and the clients of the projects you lead."
          : "View employees, their roles, and company details."}
      actions={
        canManageMembers ? (
          <div className="flex items-center gap-3">
            <InlineRefreshIndicator active={isRevalidating || isUpdatingMember || isDeletingMember} />
            {/* <button
              onClick={openCreateDrawer}
              className={`rounded-lg px-4 py-2 text-sm font-bold text-white shadow-md transition hover:opacity-90 ${GRADIENT_CYAN_PURPLE}`}
            >
              + Add Member
            </button> */}
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
                {/* Clients are listed in the directory but are not a role the
                    Add / Edit form offers, so the server's role list omits them. */}
                {!roles.some(role => role.value === 'client') && <option value="client">Client</option>}
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

        {canManageAccess && selectedIds.size > 0 && (
          <div
            role="toolbar"
            aria-label="Bulk member access"
            className="sticky top-3 z-20 flex flex-wrap items-center gap-x-6 gap-y-3 rounded-2xl border border-blue-200 bg-white/95 px-5 py-3 shadow-lg shadow-blue-900/5 backdrop-blur"
          >
            <div className="flex items-center gap-3">
              <span className="flex h-9 min-w-9 items-center justify-center rounded-full bg-blue-600 px-2.5 text-sm font-bold text-white">
                {selectedIds.size}
              </span>
              <div className="leading-tight">
                <div className="text-sm font-bold text-slate-800">{selectedIds.size} selected</div>
                <div className="text-[11px] font-medium text-slate-500">
                  {isSelectingAll
                    ? 'Selecting every member…'
                    : allSelected
                      ? 'Every member in this list'
                      : `of ${totalMembers} members`}
                </div>
              </div>
            </div>

            <div className="hidden h-8 w-px bg-slate-200 md:block" />

            {([
              { label: 'Login', key: 'can_login' as const, noun: 'allowed to log in' },
              { label: 'Add Task', key: 'can_add_tasks' as const, noun: 'allowed to add tasks' },
            ]).map(({ label, key, noun }) => {
              const canAllow = wouldChange(key, true);
              const canExclude = wouldChange(key, false);
              return (
                <div key={key} className="flex items-center gap-2.5">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-slate-500">{label}:</span>
                  <div className="inline-flex overflow-hidden rounded-lg border border-slate-200 shadow-sm">
                    <button
                      type="button"
                      disabled={isUpdatingAccess || !canAllow}
                      title={canAllow ? undefined : `Every selected member is already ${noun}`}
                      onClick={() => handleBulkAccess({ [key]: true })}
                      className="px-4 py-2 text-[11px] font-bold uppercase tracking-wider text-emerald-700 transition hover:bg-emerald-50 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-300 disabled:hover:bg-slate-50"
                    >
                      Allow
                    </button>
                    <button
                      type="button"
                      disabled={isUpdatingAccess || !canExclude}
                      title={canExclude ? undefined : `Every selected member is already excluded`}
                      onClick={() => handleBulkAccess({ [key]: false })}
                      className="border-l border-slate-200 px-4 py-2 text-[11px] font-bold uppercase tracking-wider text-rose-600 transition hover:bg-rose-50 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-300 disabled:hover:bg-slate-50"
                    >
                      Exclude
                    </button>
                  </div>
                </div>
              );
            })}

            <button
              type="button"
              onClick={() => setSelectedIds(new Set())}
              className="ml-auto inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-[11px] font-bold uppercase tracking-wider text-slate-500 transition hover:bg-slate-100 hover:text-slate-700"
            >
              <span aria-hidden="true">&times;</span> Clear selection
            </button>
          </div>
        )}

        <div className="relative overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="overflow-x-auto pb-4">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-50 text-slate-500 border-b border-slate-200">
                <tr>
                  {canManageAccess && (
                    <th className="w-10 pl-6 py-4">
                      <input
                        type="checkbox"
                        aria-label="Select all members"
                        title={allSelected ? 'Clear the selection' : `Select all ${totalMembers} members`}
                        ref={(el) => { if (el) el.indeterminate = someSelected; }}
                        checked={allSelected}
                        disabled={!filteredItems.length || isSelectingAll}
                        onChange={toggleEveryone}
                        className="h-4 w-4 cursor-pointer rounded border-slate-300 text-blue-600 focus:ring-blue-500/40"
                      />
                    </th>
                  )}
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Employee</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Role</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Status</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Designation</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Date of Joining</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Date of Birth</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">
                    Add Task<HeaderCount value={accessSummary?.add_task_allowed} meaning="allowed to add tasks" />
                  </th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">
                    Login<HeaderCount value={accessSummary?.login_allowed} meaning="allowed to log in" />
                  </th>
                  {canManageMembers && (
                    <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px] text-right">Action</th>
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {showFirstLoad ? (
                  <tr>
                    <td colSpan={columnCount} className="px-6 py-8">
                      <LoadingSpinner />
                    </td>
                  </tr>
                ) : isError ? (
                  <tr>
                    <td colSpan={columnCount} className="px-6 py-12 text-center text-red-500">
                      Failed to fetch members. Please try again.
                    </td>
                  </tr>
                ) : filteredItems.length > 0 ? (
                  filteredItems.map(member => (
                    <tr key={member.id} className={`transition hover:bg-slate-50/50 ${selectedIds.has(member.id) ? 'bg-blue-50/40' : ''}`}>
                      {canManageAccess && (
                        <td className="w-10 pl-6 py-4">
                          <input
                            type="checkbox"
                            aria-label={`Select ${member.name || 'member'}`}
                            checked={selectedIds.has(member.id)}
                            onChange={() => toggleSelected(member)}
                            className="h-4 w-4 cursor-pointer rounded border-slate-300 text-blue-600 focus:ring-blue-500/40"
                          />
                        </td>
                      )}
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
                      <td className="px-6 py-4">
                        <AddTaskSwitch
                          allowed={member.can_add_tasks !== false}
                          editable={canManageAccess}
                          busy={pendingAddTaskId === member.id}
                          onChange={(allowed) => handleSetAddTask(member, allowed)}
                        />
                      </td>
                      <td className="px-6 py-4">
                        <AddTaskSwitch
                          allowed={member.can_login !== false}
                          editable={canManageAccess}
                          busy={pendingLoginId === member.id}
                          subject="logging in"
                          allowPhrase="to log in"
                          lockedReason={member.id === currentUser?.id ? 'You cannot exclude your own account from logging in' : undefined}
                          onChange={(allowed) => handleSetLogin(member, allowed)}
                        />
                      </td>
                      {canManageMembers && (
                        <td className="px-6 py-4 text-right">
                          <div className="flex items-center justify-end gap-2">
                            <button
                              onClick={() => setLogMember({ id: member.id, name: member.name || 'Member' })}
                              className="rounded px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-indigo-600 border border-indigo-200 transition hover:bg-indigo-50"
                            >
                               Log
                            </button>
                            <button
                              onClick={() => openEditDrawer(member)}
                              className="rounded px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-[#14B8A6] border border-[#14B8A6]/30 transition hover:bg-[#14B8A6]/10"
                            >
                              Edit
                            </button>
                            <button
                              onClick={() => handleDeleteMember(member.id)}
                              disabled={member.id === currentUser?.id}
                              title={member.id === currentUser?.id ? 'You cannot delete your own account' : undefined}
                              className="rounded px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-rose-500 border border-rose-200 transition hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
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
                    <td colSpan={columnCount} className="px-6 py-12 text-center text-slate-500">
                      No members found matching your criteria.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          
        </div>
        {totalPages > 1 && (
          <Pagination page={page} totalPages={totalPages} totalItems={data?.total || 0} limit={pageSize} setPage={setPage} setLimit={setPageSize} noun="members" />
        )}
      </div>

      {/* Right Slide-over Drawer for Create / Edit */}
      {logMember && (
        <MemberLogModal memberId={logMember.id} memberName={logMember.name} onClose={() => setLogMember(null)} />
      )}
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
