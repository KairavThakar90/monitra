import React, { useState, useMemo, useEffect, useCallback } from 'react';
import { V2Shell } from '../dashboard/v2/V2Shell';
import { useGetTimeTrackingDetailsQuery, useGetTimeTrackingQuery, type TimeTrackingProject } from '../../store/api/timeTrackingApi';
import { useGetAllMembersQuery, useGetMembersQuery } from '../../store/api/membersApi';
import { useGetAllProjectsQuery } from '../../store/api/projectsApi';
import { MemberMultiSelect } from '../dashboard/v2/filters';
import { useCreateManualTimeEntryRequestMutation, useGetManualTimeEntryRequestsQuery, useApproveManualTimeEntryRequestMutation, useRejectManualTimeEntryRequestMutation, useDeleteManualTimeEntryRequestMutation } from '../../store/api/manualTimeEntryApi';
import { useFeedback } from '../../components/FeedbackProvider';
import { useAuth } from '../auth/authContext';
import { InlineRefreshIndicator } from '../../components/InlineRefreshIndicator';
import { formatHMS, formatISTDate, formatISTTime, istTodayISO, istWallClockToUtcISO } from '../../utils/duration';
import { PaginationArrow } from '../../components/PaginationArrow';
import { FieldError, SEARCH_MAX_LENGTH, useFormValidation, validateSearchTerm } from '../../validation';

// The IST calendar day, read when needed. This used to be the UTC day frozen
// at module load, which called the wrong day "today" between 00:00 and 05:30
// IST and never advanced while the tab stayed open.
const todayStr = () => istTodayISO();

export interface TimeEntry {
  id: string;
  employeeId: string;
  date: string; // YYYY-MM-DD
  clockIn: string;
  lunchStart: string;
  lunchEnd: string;
  clockOut: string;
  employeeName?: string;
  totalHours?: string;
}

/**
 * Accepts both the 24h value an `<input type="time">` produces ("09:30") and
 * the 12h text the table renders ("09:30 AM"). Returns minutes past midnight,
 * or null when the value is missing or unparseable.
 */
const parseTimeToMinutes = (value: string): number | null => {
  if (!value || value === '-') return null;

  const twelveHour = value.match(/^(\d{1,2}):(\d{2})\s*(AM|PM)$/i);
  if (twelveHour) {
    let hours = parseInt(twelveHour[1], 10);
    const isPm = twelveHour[3].toUpperCase() === 'PM';
    if (isPm && hours < 12) hours += 12;
    if (!isPm && hours === 12) hours = 0;
    return hours * 60 + parseInt(twelveHour[2], 10);
  }

  const twentyFourHour = value.match(/^(\d{1,2}):(\d{2})$/);
  if (!twentyFourHour) return null;
  const hours = parseInt(twentyFourHour[1], 10);
  const minutes = parseInt(twentyFourHour[2], 10);
  if (hours > 23 || minutes > 59) return null;
  return hours * 60 + minutes;
};

/**
 * Worked minutes between clock-in and clock-out, less an unpaid lunch when both
 * lunch times are given and sit inside the working window. Returns null when the
 * inputs cannot describe a real shift, so callers can say why rather than
 * showing a made-up total.
 */
const workedMinutes = (clockIn: string, clockOut: string, lunchStart: string, lunchEnd: string): number | null => {
  const start = parseTimeToMinutes(clockIn);
  const end = parseTimeToMinutes(clockOut);
  if (start === null || end === null || end <= start) return null;

  let total = end - start;

  const lunchFrom = parseTimeToMinutes(lunchStart);
  const lunchTo = parseTimeToMinutes(lunchEnd);
  if (lunchFrom !== null && lunchTo !== null && lunchTo > lunchFrom && lunchFrom >= start && lunchTo <= end) {
    total -= lunchTo - lunchFrom;
  }

  return total > 0 ? total : null;
};

const formatMinutes = (minutes: number) => `${Math.floor(minutes / 60)}h ${minutes % 60}m`;

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

// Timestamps are stored in UTC and displayed in IST, via the shared helpers
// so every view agrees. Durations are never timezone-converted.
const formatDate = (dateStr: string) => formatISTDate(dateStr); // e.g. "12 Jun 2026"

const formatDateTime = (dateStr: string | null) => formatISTTime(dateStr);

const SearchablePicker: React.FC<{
  label: string;
  placeholder: string;
  options: { id: string; label: string }[];
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}> = ({ label, placeholder, options, value, onChange, disabled = false }) => {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const pickerRef = React.useRef<HTMLDivElement | null>(null);
  const selected = options.find((option) => option.id === value);
  const filteredOptions = useMemo(
    () => options.filter((option) => option.label.toLowerCase().includes(query.trim().toLowerCase())),
    [options, query],
  );

  useEffect(() => {
    if (!open) return;
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', closeOnOutsideClick);
    return () => document.removeEventListener('mousedown', closeOnOutsideClick);
  }, [open]);

  return (
    <div ref={pickerRef} className="relative">
      <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">{label}</label>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        className={`flex w-full items-center justify-between gap-3 rounded-lg border bg-white px-4 py-2.5 text-left text-sm font-medium outline-none transition ${open ? 'border-[#3B82F6] ring-2 ring-[#3B82F6]/15' : 'border-slate-300 hover:border-slate-400'} ${disabled ? 'cursor-not-allowed bg-slate-50 text-slate-400' : 'text-slate-700'}`}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className={selected ? 'truncate' : 'truncate text-slate-400'}>{selected?.label || placeholder}</span>
        <svg className={`h-4 w-4 shrink-0 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <div className="absolute left-0 right-0 top-full z-40 mt-2 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl">
          <div className="border-b border-slate-100 p-2.5">
            <input
              autoFocus
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={`Search ${label.toLowerCase()}...`}
              className="w-full rounded-lg bg-slate-50 px-3 py-2 text-sm font-semibold text-slate-700 outline-none placeholder:text-slate-400 focus:ring-2 focus:ring-blue-500/20"
            />
          </div>
          <div className="max-h-60 overflow-y-auto p-1.5" role="listbox">
            {filteredOptions.length ? filteredOptions.map((option) => (
              <button
                key={option.id}
                type="button"
                role="option"
                aria-selected={option.id === value}
                onClick={() => { onChange(option.id); setOpen(false); setQuery(''); }}
                className={`block w-full truncate rounded-lg px-3 py-2 text-left text-sm transition hover:bg-slate-50 ${option.id === value ? 'bg-blue-50 font-bold text-blue-700' : 'font-semibold text-slate-700'}`}
                title={option.label}
              >
                {option.label}
              </button>
            )) : <p className="px-3 py-5 text-center text-xs font-semibold text-slate-400">No options found.</p>}
          </div>
        </div>
      )}
    </div>
  );
};


/**
 * One project inside the Time Tracking Details dialog, as a collapsible panel.
 *
 * The dialog used to print every project's totals and its whole task table at
 * once, so an employee on four projects opened to several screens of scrolling
 * before the reader could see what they had worked on. The header alone --
 * project, status, tracked time -- answers that, and the tasks are one click
 * away.
 *
 * Panels are independent rather than mutually exclusive: comparing two
 * projects' task lists is the reason somebody opens this dialog, and a strict
 * one-at-a-time accordion would close the first panel to open the second.
 */
const ProjectAccordionItem: React.FC<{
  project: TimeTrackingProject;
  isOpen: boolean;
  onToggle: () => void;
}> = ({ project, isOpen, onToggle }) => {
  const statusColor = project.status?.color || "#94a3b8";
  const panelId = `time-details-project-${project.id}`;

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={isOpen}
        aria-controls={panelId}
        className="flex w-full flex-wrap items-center gap-3 px-4 py-3 text-left transition hover:bg-slate-50"
      >
        <svg
          className={`h-4 w-4 shrink-0 text-slate-400 transition-transform duration-200 ${isOpen ? "rotate-90" : ""}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
        <h4 className="min-w-0 flex-1 truncate text-sm font-bold text-slate-800">{project.name}</h4>
        {/* Total hours belong in the header: collapsed, this is the whole
            answer for this project. */}
        <span className="shrink-0 text-sm font-bold tabular-nums text-slate-700">{project.total_time}</span>
        <span
          className="shrink-0 rounded-full px-2.5 py-1 text-[11px] font-bold"
          style={{ color: statusColor, backgroundColor: `${statusColor}18` }}
        >
          {project.status?.name || "Unknown"}
        </span>
      </button>

      <div id={panelId} hidden={!isOpen} className="border-t border-slate-100 px-4 pb-4 pt-3">
        {/* Total hours is not repeated here -- the header carries it, and it
            is the one figure that has to be readable while collapsed. */}
        <div className="grid gap-2 sm:grid-cols-3">
          <div className="rounded-lg bg-slate-50 px-3 py-2">
            <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Total seconds</div>
            <div className="mt-1 text-xs font-semibold text-slate-700">{project.total_seconds}</div>
          </div>
        </div>
        <div className="mt-4 overflow-hidden rounded-lg border border-slate-100">
          <div className="border-b border-slate-100 bg-slate-50 px-3 py-2 text-[10px] font-bold uppercase tracking-wider text-slate-400">
            Tasks
          </div>
          {(project.tasks?.length || 0) ? (project.tasks || []).map((task) => (
            <div key={task.id} className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-3 py-3 last:border-b-0">
              <div className="min-w-0">
                <div className="truncate text-xs font-bold text-slate-800">{task.name}</div>
                <div className="mt-1 text-[11px] font-semibold text-slate-500">{task.total_time} ({task.total_seconds} seconds)</div>
              </div>
              <span
                className="shrink-0 rounded-full px-2.5 py-1 text-[10px] font-bold"
                style={{ color: (task.status?.color || "#94a3b8"), backgroundColor: `${(task.status?.color || "#94a3b8")}18` }}
              >
                {task.status?.name || "Unknown"}
              </span>
            </div>
          )) : <p className="px-3 py-4 text-xs text-slate-500">No tasks recorded.</p>}
        </div>
      </div>
    </div>
  );
};


const PAGE_SIZE = 50;
const GRADIENT_CYAN_PURPLE = "bg-gradient-to-r from-[#0ea5e9] to-[#8b5cf6]";

export const AdminTimeTracking: React.FC = () => {
  const [search, setSearch] = useState('');
  const [searchError, setSearchError] = useState<string | null>(null);
  
  // By default show today date
  const todayRange = applyDatePreset('Today');
  const [datePreset, setDatePreset] = useState('Today');
  const [filterStartDate, setFilterStartDate] = useState(todayRange.start);
  const [filterEndDate, setFilterEndDate] = useState(todayRange.end);
  const [dateFilterOpen, setDateFilterOpen] = useState(false);

  const [page, setPage] = useState(1);
  const [selectedEmployeeId, setSelectedEmployeeId] = useState<number | null>(null);
  /**
   * The Time Entries table's own member filter, distinct from
   * `selectedEmployeeId` above -- that one only opens the details dialog for
   * one employee and affects nothing else. Empty means everyone, the same
   * convention every other filter in the app uses.
   */
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  const { data: allMembers = [] } = useGetAllMembersQuery();
  /**
   * Which projects are expanded in the details dialog. Empty is the default --
   * every project starts collapsed, including after the dialog is reopened on
   * a different employee, which is why this is cleared alongside the id rather
   * than left to carry one employee's open panels onto the next.
   */
  const [expandedProjectIds, setExpandedProjectIds] = useState<number[]>([]);

  const openEmployeeDetails = (employeeId: number) => {
    setExpandedProjectIds([]);
    setSelectedEmployeeId(employeeId);
  };

  const closeEmployeeDetails = () => {
    setExpandedProjectIds([]);
    setSelectedEmployeeId(null);
  };

  const toggleProject = (projectId: number) =>
    setExpandedProjectIds((open) =>
      open.includes(projectId) ? open.filter((id) => id !== projectId) : [...open, projectId]
    );

  const { data: trackingData, isLoading, isFetching, isError } = useGetTimeTrackingQuery({
    start_date: filterStartDate || undefined,
    end_date: filterEndDate || undefined,
    page,
    limit: PAGE_SIZE,
  });
  const { data: employeeDetails, isLoading: isLoadingDetails } = useGetTimeTrackingDetailsQuery({
    employeeId: selectedEmployeeId || 0,
    start_date: filterStartDate || undefined,
    end_date: filterEndDate || undefined,
  }, { skip: selectedEmployeeId === null });

  // Keep the previous range's rows visible while the new one loads instead of
  // covering the table every time a date filter changes.
  const showFirstLoad = isLoading && !trackingData;

  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [formEmployeeId, setFormEmployeeId] = useState('');
  const [formProjectId, setFormProjectId] = useState('');
  const [formTaskId, setFormTaskId] = useState('');
  const [formDate, setFormDate] = useState(todayStr());
  const [formClockIn, setFormClockIn] = useState('09:00');
  const [formClockOut, setFormClockOut] = useState('18:00');
  const [formError, setFormError] = useState<string | null>(null);

  const { showToast } = useFeedback();
  const { currentUser } = useAuth();

  const [activeTab, setActiveTab] = useState<'entries' | 'requests'>('entries');
  const [requestsPage, setRequestsPage] = useState(1);
  // Deliberately independent of `search` and `selectedEmployeeId`: those
  // belong to the Time Entries table and its per-employee details dialog.
  // This tab had no filter control of its own and was reusing that leftover
  // state instead -- so opening any employee's details, or typing into the
  // employee-name search box, silently narrowed the approval queue to one
  // person (or hid every request whose description didn't match a name
  // search) with no visible indication why. An admin reviewing requests must
  // always see every pending request in the organization by default.
  const { data: requestsData, isLoading: isLoadingRequests } = useGetManualTimeEntryRequestsQuery({
    page: requestsPage,
    limit: PAGE_SIZE,
  });
  
  const [createManualTimeEntry, { isLoading: isSaving }] = useCreateManualTimeEntryRequestMutation();
  const [approveRequest] = useApproveManualTimeEntryRequestMutation();
  const [rejectRequest] = useRejectManualTimeEntryRequestMutation();
  const [deleteRequest] = useDeleteManualTimeEntryRequestMutation();

  const handleApprove = async (id: number) => {
    try {
      await approveRequest(id).unwrap();
      showToast('Approved manual request', 'success');
    } catch (error: any) {
      if (error?.status === 409 || error?.data?.detail === 'Conflict' || error?.data?.detail?.includes('overlap')) {
        showToast('Conflict: This entry overlaps with existing tracked time.', 'error');
      } else {
        showToast(error?.data?.detail || 'Failed to approve request', 'error');
      }
    }
  };

  const handleReject = async (id: number) => {
    try {
      await rejectRequest(id).unwrap();
      showToast('Rejected manual request', 'success');
    } catch (error: any) {
      showToast(error?.data?.detail || 'Failed to reject request', 'error');
    }
  };

  const handleDeleteRequest = async (id: number) => {
    try {
      await deleteRequest(id).unwrap();
      showToast('Deleted manual request', 'success');
    } catch (error: any) {
      showToast(error?.data?.detail || 'Failed to delete request', 'error');
    }
  };



  // Everything the drawer offers comes from these two calls. `GET /projects`
  // already embeds `employees`, `leader` and `tasks[].assignee`, so the whole
  // employee -> project -> task cascade is a client-side narrowing of data we
  // have rather than three round trips.
  const { data: membersResponse, isLoading: isLoadingMembers } = useGetMembersQuery({ limit: 100 });

  /**
   * Whether this user may file a manual entry against somebody else's
   * timesheet. A leader may not: they see their team's tracked time and
   * approve or reject the requests their members file, but the entry itself
   * has to originate with the person whose day it describes — the backend
   * refuses the write, so the picker must not offer it either.
   */
  const canLogForOthers = !!currentUser?.permissions?.['manual_time_entries:create_for_others'];

  const memberOptions = useMemo(() => {
    const items = membersResponse?.items ?? [];
    if (canLogForOthers) return items;
    // Own row only. Falling back to a synthesized row keeps the required
    // select from being empty if the directory has not loaded yet.
    const own = items.filter(member => member.id === currentUser?.id);
    return own.length || !currentUser ? own : [{ ...(items[0] ?? {}), id: currentUser.id, name: currentUser.name }];
  }, [membersResponse, canLogForOthers, currentUser]);

  const { data: projectsResponse, isLoading: isLoadingProjects } = useGetAllProjectsQuery();
  const allProjects = useMemo(() => projectsResponse ?? [], [projectsResponse]);

  // Rows carry their own name from the API; the lookup is the fallback for any
  // row that does not. Stable identity so the filter memo below can depend on it.
  const getEmployeeName = useCallback(
    (id: string, name?: string) => name || memberOptions.find(m => String(m.id) === id)?.name || id,
    [memberOptions]
  );

  /** Projects the selected employee is on — as a member or as the leader. */
  const employeeProjects = useMemo(() => {
    if (!formEmployeeId) return [];
    const employeeId = Number(formEmployeeId);
    return allProjects.filter(project =>
      project.leader?.id === employeeId ||
      (project.employees || []).some(employee => employee.id === employeeId)
    );
  }, [allProjects, formEmployeeId]);

  /** Every task on the selected project, regardless of assignee. */
  const employeeTasks = useMemo(() => {
    if (!formProjectId) return [];
    const project = employeeProjects.find(p => String(p.id) === formProjectId);
    return project?.tasks || [];
  }, [employeeProjects, formProjectId]);

  // Projects and tasks arrive after the drawer can be opened, and changing the
  // employee re-narrows both lists. Drop any selection that is no longer on
  // offer so the form can never submit an id the dropdown is not showing.
  useEffect(() => {
    if (formProjectId && !employeeProjects.some(p => String(p.id) === formProjectId)) {
      setFormProjectId('');
    }
  }, [employeeProjects, formProjectId]);

  useEffect(() => {
    if (formTaskId && !employeeTasks.some(t => String(t.id) === formTaskId)) {
      setFormTaskId('');
    }
  }, [employeeTasks, formTaskId]);

  const draftMinutes = workedMinutes(formClockIn, formClockOut, '', '');

  /**
   * The three pickers and the work date go through the framework; the
   * shift-window checks below stay where they are, because "clock out must be
   * later than clock in" is a rule about this form's business meaning rather
   * than about whether a value is well-formed input.
   */
  const entryForm = useFormValidation({
    employeeId: { rule: 'identifier', label: 'Employee', required: true },
    projectId: { rule: 'identifier', label: 'Project', required: true },
    taskId: { rule: 'identifier', label: 'Task', required: true },
    workDate: { rule: 'date', label: 'Work date', required: true },
  });


  const apiEntries = useMemo(() => (trackingData?.items || []).map((entry) => ({
    id: `${entry.employee_id}-${entry.date}-${entry.start_time || 'entry'}`,
    employeeId: String(entry.employee_id),
    employeeName: entry.name,
    date: entry.date,
    clockIn: formatISTTime(entry.start_time),
    lunchStart: '-',
    lunchEnd: '-',
    clockOut: entry.end_time ? formatISTTime(entry.end_time) : '',
    totalHours: formatHMS(entry.total_seconds),
  })), [trackingData?.items]);

  const filteredEntries = useMemo(() => {
    return apiEntries.filter(e => {
      if (search && !getEmployeeName(e.employeeId, e.employeeName).toLowerCase().includes(search.toLowerCase())) return false;

      if (selectedMembers.length > 0 && !selectedMembers.includes(e.employeeId)) return false;

      if (filterStartDate && e.date < filterStartDate) return false;
      if (filterEndDate && e.date > filterEndDate) return false;

      return true;
    }).sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime());
  }, [apiEntries, getEmployeeName, search, selectedMembers, filterStartDate, filterEndDate]);

  const totalPages = trackingData?.pagination?.total_pages || Math.ceil(filteredEntries.length / PAGE_SIZE) || 1;
  const paginatedEntries = trackingData ? filteredEntries : filteredEntries.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);

    const check = entryForm.validateAll({
      employeeId: formEmployeeId,
      projectId: formProjectId,
      taskId: formTaskId,
      workDate: formDate,
    });
    if (!check.ok) {
      // The same summary the drawer has always shown, now with each rejected
      // picker also marked in place.
      setFormError('Pick an employee, a project and a task before saving.');
      return;
    }
    if (formDate > todayStr()) {
      setFormError('Work date cannot be in the future.');
      return;
    }
    if (draftMinutes === null) {
      setFormError('Clock out must be later than clock in, and lunch must fall inside the shift.');
      return;
    }
    if (draftMinutes > 24 * 60) {
      setFormError('A single entry cannot cover more than 24 hours.');
      return;
    }


    try {
      await createManualTimeEntry({
        project_id: check.values.projectId as number,
        task_id: check.values.taskId as number,
        work_date: check.values.workDate as string,
        total_seconds: draftMinutes * 60,
        user_id: check.values.employeeId as number,
        // The admin types IST wall-clock times; labelling them "Z" would
        // claim they were UTC and shift every manual entry by 5h30m.
        start_time: istWallClockToUtcISO(formDate, formClockIn),
        end_time: istWallClockToUtcISO(formDate, formClockOut),
        description: 'Manual entry created from admin panel.',
        is_billable: true,
      }).unwrap();

      showToast('Manual time entry requested successfully.', 'success');
      setActiveTab('requests');
      setIsDrawerOpen(false);
    } catch (error: any) {
      setFormError(error?.data?.detail?.message || error?.data?.detail || 'Failed to save the manual time entry.');
    }
  };

  const closeDrawer = () => setIsDrawerOpen(false);
  const openDrawer = () => {
    // Default to the signed-in user: the only employee the create API can
    // currently file time against.
    setFormEmployeeId(currentUser ? String(currentUser.id) : '');
    setFormProjectId('');
    setFormTaskId('');
    setFormDate(todayStr());
    setFormClockIn('09:00');
    setFormClockOut('18:00');
    setFormError(null);
    entryForm.clear();
    setIsDrawerOpen(true);
  };

  return (
    <V2Shell title="Time Tracking" subtitle="Monitor and manage employee time logs">
      <div className="w-full px-4 sm:px-6 lg:px-8 pt-6 space-y-6 pb-20">
      
      <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex flex-1 items-center gap-2 px-2">
          <svg className="h-5 w-5 text-slate-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            type="text"
            placeholder="Search by employee name..."
            value={search}
            maxLength={SEARCH_MAX_LENGTH}
            aria-invalid={searchError ? true : undefined}
            aria-describedby={searchError ? 'time-search-error' : undefined}
            onChange={(e) => {
              const next = e.target.value;
              setSearch(next);
              const result = validateSearchTerm(next, { fieldLabel: 'Search' });
              setSearchError(result.ok ? null : result.error);
              setPage(1);
            }}
            className="flex-1 bg-transparent text-sm outline-none placeholder:text-slate-400 text-slate-700"
          />
          <FieldError id="time-search-error" message={searchError} />
        </div>

        <div className="h-8 w-px bg-slate-200 hidden lg:block"></div>

        <div className="flex flex-wrap items-center gap-4">
          {/* Date Range Filter */}
          <div className="relative">
            <button
              onClick={() => setDateFilterOpen(!dateFilterOpen)}
              className="flex items-center gap-2 rounded-lg border border-[#38bdf8] bg-white px-3 py-1.5 text-sm font-medium text-slate-700 transition hover:bg-slate-50 shadow-sm"
            >
              <span className="text-[#0ea5e9]">
                {datePreset !== 'All Time' ? datePreset : (filterStartDate ? `${formatDate(filterStartDate)} to ${formatDate(filterEndDate) || 'Any'}` : 'Filter Date')}
              </span>
              <svg className="h-4 w-4 text-[#0ea5e9]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
              </svg>
            </button>
            
            {dateFilterOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setDateFilterOpen(false)} />
                <div className="absolute right-0 sm:right-auto lg:right-0 z-20 mt-2 flex flex-col sm:flex-row w-[calc(100vw-2rem)] sm:w-[480px] overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl animate-in fade-in slide-in-from-top-2 max-w-sm sm:max-w-none">
                  <div className="w-full sm:w-1/3 border-b sm:border-b-0 sm:border-r border-slate-100 bg-slate-50 p-2 space-y-1 overflow-x-auto sm:overflow-visible flex sm:block gap-2">
                    {['All Time', 'Today', 'Yesterday', 'Last 7 days', 'Last week', 'Last 2 weeks', 'This month', 'Last month'].map(preset => (
                      <button
                        key={preset}
                        onClick={() => {
                          setDatePreset(preset);
                          if (preset === 'All Time') {
                            setFilterStartDate('');
                            setFilterEndDate('');
                          } else {
                            const { start, end } = applyDatePreset(preset);
                            setFilterStartDate(start);
                            setFilterEndDate(end);
                          }
                          setPage(1);
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
                            value={filterStartDate}
                            onChange={(e) => {
                              setFilterStartDate(e.target.value);
                              setDatePreset('Custom');
                              setPage(1);
                            }}
                            className="w-full rounded-md border border-slate-200 px-3 py-1.5 text-sm outline-none focus:border-[#38bdf8] focus:ring-1 focus:ring-[#38bdf8]"
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-xs font-semibold text-slate-500">End Date</label>
                          <input 
                            type="date"
                            value={filterEndDate}
                            onChange={(e) => {
                              setFilterEndDate(e.target.value);
                              setDatePreset('Custom');
                              setPage(1);
                            }}
                            className="w-full rounded-md border border-slate-200 px-3 py-1.5 text-sm outline-none focus:border-[#38bdf8] focus:ring-1 focus:ring-[#38bdf8]"
                          />
                        </div>
                      </div>
                      <div className="mt-6 flex justify-end gap-2">
                        <button 
                          onClick={() => {
                            setFilterStartDate('');
                            setFilterEndDate('');
                            setDatePreset('All Time');
                            setPage(1);
                          }}
                          className="rounded-md px-3 py-1.5 text-xs font-semibold text-slate-500 hover:bg-slate-100"
                        >
                          Clear
                        </button>
                        <button 
                          onClick={() => setDateFilterOpen(false)}
                          className="rounded-md bg-[#0ea5e9] px-4 py-1.5 text-xs font-bold text-white shadow-sm hover:bg-sky-500 transition"
                        >
                          Apply
                        </button>
                      </div>
                  </div>
                </div>
              </>
            )}
          </div>

          <MemberMultiSelect
            members={allMembers}
            selected={selectedMembers}
            onChange={(ids) => { setSelectedMembers(ids); setPage(1); }}
          />

          <button
            onClick={openDrawer}
            className={`rounded-lg px-4 py-2 text-sm font-bold text-white shadow-md transition hover:opacity-90 ${GRADIENT_CYAN_PURPLE}`}
          >
            + Add Manually Time
          </button>
        </div>
      </div>

              <div className="mt-6 mb-4 flex gap-4 border-b border-slate-200">
          <button
            onClick={() => setActiveTab('entries')}
            className={`pb-3 text-sm font-bold transition ${activeTab === 'entries' ? 'border-b-2 border-indigo-500 text-indigo-600' : 'text-slate-500 hover:text-slate-700'}`}
          >
            Time Entries
          </button>
          <button
            onClick={() => setActiveTab('requests')}
            className={`pb-3 text-sm font-bold transition flex items-center gap-2 ${activeTab === 'requests' ? 'border-b-2 border-indigo-500 text-indigo-600' : 'text-slate-500 hover:text-slate-700'}`}
          >
            Manual Requests
            {requestsData?.items.filter(r => r.approval_status === 'pending').length ? (
              <span className="flex h-5 w-5 items-center justify-center rounded-full bg-amber-100 text-[10px] font-bold text-amber-600">
                {requestsData.items.filter(r => r.approval_status === 'pending').length}
              </span>
            ) : null}
          </button>
        </div>
        <div className="relative overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm min-h-[400px]">
        {showFirstLoad ? (
          <div className="absolute inset-0 z-10 flex items-start justify-center bg-white/55 pt-20 backdrop-blur-[2px]">
            <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-500 border-t-transparent" role="status" aria-label="Loading time tracking" />
          </div>
        ) : (
          <div className="pointer-events-none absolute right-3 top-3 z-10">
            <InlineRefreshIndicator active={isFetching} />
          </div>
        )}
                  {activeTab === 'entries' ? (
            <div className={`overflow-x-auto pb-4 transition-all duration-300 ${isFetching ? "blur-[2px] opacity-60 pointer-events-none" : ""}`}>
              <table className="w-full text-left text-sm whitespace-nowrap">
                <thead className="bg-slate-50 text-slate-500 border-b border-slate-200">
                  <tr>
                    <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Employee</th>
                    <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Date</th>
                    <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Start Time (Clock In)</th>
                    <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Clock Out</th>
                    <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Total Time</th>
                    <th className="px-6 py-4 text-right font-bold uppercase tracking-wider text-[11px]">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {isError ? (
                    <tr><td colSpan={6} className="px-6 py-12 text-center text-rose-500">Unable to load time tracking data.</td></tr>
                  ) : paginatedEntries.map(entry => (
                    <tr key={entry.id} className="transition hover:bg-slate-50/80">
                      <td className="px-6 py-4">
                        <div className="font-semibold text-slate-800">{getEmployeeName(entry.employeeId, entry.employeeName)}</div>
                      </td>
                      <td className="px-6 py-4">
                        <div className="font-medium text-slate-600">{formatDate(entry.date)}</div>
                      </td>
                      <td className="px-6 py-4">
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-bold text-emerald-700">
                          {entry.clockIn}
                        </span>
                      </td>
                      <td className="px-6 py-4">
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2.5 py-1 text-xs font-bold text-amber-700">
                          {entry.clockOut}
                        </span>
                      </td>
                      <td className="px-6 py-4 font-bold text-slate-800">
                        {entry.totalHours}
                      </td>
                      <td className="px-6 py-4 text-right">
                        <button
                          onClick={() => openEmployeeDetails(Number(entry.employeeId))}
                          className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-bold text-slate-600 shadow-sm transition hover:border-slate-300 hover:text-slate-900"
                        >
                          View Details
                        </button>
                      </td>
                    </tr>
                  ))}
                  {!isError && paginatedEntries.length === 0 && !showFirstLoad && (
                    <tr>
                      <td colSpan={6} className="px-6 py-12 text-center text-slate-500">
                        No time tracking data found for the selected period.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
              
              {/* Pagination for Time Entries */}
              {totalPages > 1 && (
                <div className="flex items-center justify-between border-t border-slate-100 px-6 py-4">
                  <div className="text-sm font-medium text-slate-500">
                    Showing <span className="font-bold text-slate-900">{(page - 1) * PAGE_SIZE + 1}</span> to <span className="font-bold text-slate-900">{Math.min(page * PAGE_SIZE, trackingData?.pagination?.total || filteredEntries.length)}</span> of <span className="font-bold text-slate-900">{trackingData?.pagination?.total || filteredEntries.length}</span> entries
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setPage(p => Math.max(1, p - 1))}
                      disabled={page === 1}
                      className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-bold text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
                    >
                      Previous
                    </button>
                    <button
                      onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                      disabled={page === totalPages}
                      className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-bold text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
                    >
                      Next
                    </button>
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className={`overflow-x-auto pb-4 transition-all duration-300 ${isFetching ? "blur-[2px] opacity-60 pointer-events-none" : ""}`}>
              <table className="w-full text-left text-sm whitespace-nowrap">
                <thead className="bg-slate-50 text-slate-500 border-b border-slate-200">
                  <tr>
                    <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Employee</th>
                    <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Project / Task</th>
                    <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Date</th>
                    <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Time</th>
                    <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Status</th>
                    <th className="px-6 py-4 text-right font-bold uppercase tracking-wider text-[11px]">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {isLoadingRequests ? (
                    <tr><td colSpan={6} className="px-6 py-12 text-center text-slate-500">Loading requests...</td></tr>
                  ) : requestsData?.items.map(req => (
                    <tr key={req.id} className="transition hover:bg-slate-50/80">
                      <td className="px-6 py-4">
                        <div className="font-semibold text-slate-800">{req.member_name}</div>
                        <div className="text-xs text-slate-500">{req.member_email}</div>
                      </td>
                      <td className="px-6 py-4">
                        <div className="font-semibold text-slate-700">{req.project_name}</div>
                        <div className="text-xs text-slate-500">{req.task_name}</div>
                      </td>
                      <td className="px-6 py-4">
                        <div className="font-medium text-slate-600">{formatDate(req.work_date)}</div>
                      </td>
                      <td className="px-6 py-4 font-bold text-slate-800">
                        {formatMinutes(Math.floor(req.total_seconds / 60))}
                      </td>
                      <td className="px-6 py-4">
                        {req.approval_status === 'pending' ? (
                          <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2.5 py-1 text-xs font-bold text-amber-700">
                            {req.has_conflict && <span title="Conflict with existing entry" className="mr-1">⚠️</span>}
                            Pending
                          </span>
                        ) : req.approval_status === 'approved' ? (
                          <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-bold text-emerald-700">
                            Approved
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1.5 rounded-full bg-rose-50 px-2.5 py-1 text-xs font-bold text-rose-700">
                            Rejected
                          </span>
                        )}
                      </td>
                      <td className="px-6 py-4 text-right">
                        {req.approval_status === 'pending' && (
                          <div className="flex justify-end gap-2">
                            <button
                              onClick={() => handleApprove(req.id)}
                              className="rounded-md bg-emerald-100 px-2 py-1 text-xs font-bold text-emerald-700 hover:bg-emerald-200"
                            >
                              Approve
                            </button>
                            <button
                              onClick={() => handleReject(req.id)}
                              className="rounded-md bg-rose-100 px-2 py-1 text-xs font-bold text-rose-700 hover:bg-rose-200"
                            >
                              Reject
                            </button>
                            <button
                              onClick={() => handleDeleteRequest(req.id)}
                              className="rounded-md bg-slate-100 px-2 py-1 text-xs font-bold text-slate-700 hover:bg-slate-200"
                              title="Delete request completely"
                            >
                              ×
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                  {!isLoadingRequests && (!requestsData?.items || requestsData.items.length === 0) && (
                    <tr>
                      <td colSpan={6} className="px-6 py-12 text-center text-slate-500">
                        No manual time entry requests found.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>

              {/* Pagination for Requests */}
              {requestsData && requestsData.pagination.total_pages > 1 && (
                <div className="flex items-center justify-between border-t border-slate-100 px-6 py-4">
                  <div className="text-sm font-medium text-slate-500">
                    Showing <span className="font-bold text-slate-900">{(requestsData.pagination.page - 1) * requestsData.pagination.limit + 1}</span> to <span className="font-bold text-slate-900">{Math.min(requestsData.pagination.page * requestsData.pagination.limit, requestsData.pagination.total)}</span> of <span className="font-bold text-slate-900">{requestsData.pagination.total}</span> requests
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setRequestsPage(p => Math.max(1, p - 1))}
                      disabled={requestsData.pagination.page === 1}
                      className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-bold text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
                    >
                      Previous
                    </button>
                    <button
                      onClick={() => setRequestsPage(p => Math.min(requestsData.pagination.total_pages, p + 1))}
                      disabled={requestsData.pagination.page === requestsData.pagination.total_pages}
                      className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-bold text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
                    >
                      Next
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between rounded-xl border border-slate-200 bg-white px-6 py-4 shadow-sm">
          <span className="text-xs font-medium text-slate-500">
            Showing {((page - 1) * PAGE_SIZE) + 1} to {Math.min(page * PAGE_SIZE, trackingData?.pagination?.total || filteredEntries.length)} of {trackingData?.pagination?.total || filteredEntries.length} Entries
          </span>
          <div className="flex items-center gap-1">
            <PaginationArrow direction="prev" disabled={page === 1} onClick={() => setPage(p => Math.max(1, p - 1))} />
            <div className="flex h-8 items-center justify-center px-3 text-sm font-semibold text-slate-700">
              {page} / {totalPages}
            </div>
            <PaginationArrow direction="next" disabled={page === totalPages} onClick={() => setPage(p => Math.min(totalPages, p + 1))} />
          </div>
        </div>
      )}

      {selectedEmployeeId !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-sm">
          <div className="max-h-[90vh] w-full max-w-2xl overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl" role="dialog" aria-modal="true" aria-label="Employee time details">
            <div className="flex items-start justify-between border-b border-slate-100 bg-slate-50 px-6 py-5">
              <div>
                <h2 className="text-xl font-black text-slate-800">Time Tracking Details</h2>
                <p className="mt-1 text-sm font-semibold text-slate-500">Employee summary for the selected date range</p>
              </div>
              <button type="button" onClick={closeEmployeeDetails} className="rounded-lg p-2 text-slate-400 transition hover:bg-white hover:text-slate-700" aria-label="Close time details">
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
              </button>
            </div>
            <div className="max-h-[calc(90vh-86px)] overflow-y-auto p-6">
              {isLoadingDetails && !employeeDetails ? <div className="flex justify-center py-16"><div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-500 border-t-transparent" /></div> : employeeDetails ? (
                <>
                  <div className="flex items-center gap-4 rounded-xl border border-blue-100 bg-blue-50/60 p-4">
                    <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-r from-[#0ea5e9] to-[#8b5cf6] text-sm font-black text-white">{(employeeDetails.employee?.name || "U").slice(0, 2).toUpperCase()}</div>
                    <div><h3 className="font-black text-slate-800">{employeeDetails.employee?.name || "Unknown"}</h3><p className="text-sm text-slate-500">{employeeDetails.employee?.designation || employeeDetails.employee?.email || "No details"}</p></div>
                  </div>
                  <div className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-5">
                    {[['Start date', formatDate(employeeDetails?.start_date)], ['End date', formatDate(employeeDetails?.end_date)], ['Start time', formatDateTime(employeeDetails?.summary?.start_time)], ['End time', formatDateTime(employeeDetails?.summary?.end_time)], ['Total time', employeeDetails?.summary?.total_time]].map(([label, value]) => <div key={label} className="min-h-[86px] rounded-xl border border-slate-100 bg-slate-50 p-4"><div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">{label}</div><div className="mt-2 text-sm font-bold leading-5 text-slate-800">{value}</div></div>)}
                  </div>
                  <h3 className="mt-6 text-xs font-black uppercase tracking-widest text-blue-500">Projects</h3>
                  {(employeeDetails?.projects?.length || 0) ? (
                    <div className="mt-3 space-y-3">
                      {employeeDetails?.projects?.map((project) => (
                        <ProjectAccordionItem
                          key={project.id}
                          project={project}
                          isOpen={expandedProjectIds.includes(project.id)}
                          onToggle={() => toggleProject(project.id)}
                        />
                      ))}
                    </div>
                  ) : <p className="mt-3 rounded-xl border border-dashed border-slate-200 p-6 text-center text-sm text-slate-500">No projects recorded.</p>}
                </>
              ) : <p className="py-16 text-center text-sm text-slate-500">No details found.</p>}
            </div>
          </div>
        </div>
      )}

      {/* Slide-over Drawer for Adding Time */}
      <div className={`fixed inset-0 z-50 overflow-hidden ${isDrawerOpen ? 'pointer-events-auto' : 'pointer-events-none'}`}>
        <div className={`absolute inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity duration-300 ${isDrawerOpen ? 'opacity-100' : 'opacity-0'}`} onClick={closeDrawer} />
        <div className={`absolute inset-y-0 right-0 w-full max-w-md bg-white shadow-2xl transition-transform duration-300 ease-in-out ${isDrawerOpen ? 'translate-x-0' : 'translate-x-full'}`}>
          <div className="flex h-full flex-col">
            <div className="flex items-center justify-between border-b border-slate-100 px-6 py-5">
              <h3 className="text-lg font-bold text-slate-800">Add Manually Time</h3>
              <button type="button" onClick={closeDrawer} className="text-slate-400 hover:text-slate-600">
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            
            <div className="flex-1 overflow-y-auto">
              <form id="time-form" onSubmit={handleSave} className="p-6 space-y-6">
                
                <div>
                  <SearchablePicker
                    label="Employee Name"
                    placeholder={isLoadingMembers ? 'Loading employees...' : 'Select an employee'}
                    options={memberOptions.map(m => ({ id: String(m.id), label: m.name }))}
                    value={formEmployeeId}
                    onChange={value => { setFormEmployeeId(value); setFormProjectId(''); setFormTaskId(''); setFormError(null); }}
                    disabled={!canLogForOthers}
                  />
                  {!isLoadingMembers && memberOptions.length === 0 && (
                    <p className="mt-2 text-xs font-semibold text-slate-500">No employees found.</p>
                  )}
                  <FieldError id={entryForm.errorId('employeeId')} message={entryForm.errors.employeeId} />
                  {!canLogForOthers && (
                    <p className="mt-2 text-xs font-semibold text-slate-500">
                      You can add manual time for yourself only. A member files their own request; you approve or reject it.
                    </p>
                  )}
                </div>

                <div>
                  <SearchablePicker
                    label="Project Name"
                    placeholder={!formEmployeeId ? 'Select an employee first' : isLoadingProjects ? 'Loading projects...' : 'Select a project'}
                    options={employeeProjects.map(p => ({ id: String(p.id), label: p.project_name }))}
                    value={formProjectId}
                    onChange={value => { setFormProjectId(value); setFormTaskId(''); setFormError(null); }}
                    disabled={!formEmployeeId || isLoadingProjects}
                  />
                  <FieldError id={entryForm.errorId('projectId')} message={entryForm.errors.projectId} />
                  {formEmployeeId && !isLoadingProjects && employeeProjects.length === 0 && (
                    <p className="mt-2 text-xs font-semibold text-amber-600">
                      This employee is not assigned to any project.
                    </p>
                  )}
                </div>

                <div>
                  <SearchablePicker
                    label="Task Name"
                    placeholder={formProjectId ? 'Select a task' : 'Select a project first'}
                    options={employeeTasks.map(t => ({ id: String(t.id), label: t.name }))}
                    value={formTaskId}
                    onChange={value => { setFormTaskId(value); setFormError(null); }}
                    disabled={!formProjectId}
                  />
                  <FieldError id={entryForm.errorId('taskId')} message={entryForm.errors.taskId} />
                  {formProjectId && employeeTasks.length === 0 && (
                    <p className="mt-2 text-xs font-semibold text-amber-600">
                      No tasks found on this project.
                    </p>
                  )}
                </div>

                <div>
                  <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500" htmlFor="mt-date">Date</label>
                  <input
                    id="mt-date"
                    required
                    type="date"
                    max={todayStr()}
                    value={formDate}
                    onChange={e => { setFormDate(e.target.value); setFormError(null); }}
                    onBlur={() => entryForm.validateField('workDate', formDate)}
                    {...entryForm.fieldProps('workDate')}
                    className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium"
                  />
                  <FieldError id={entryForm.errorId('workDate')} message={entryForm.errors.workDate} />
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500" htmlFor="mt-clock-in">Start Time (Clock In)</label>
                    <input
                      id="mt-clock-in"
                      required
                      type="time"
                      value={formClockIn}
                      onChange={e => { setFormClockIn(e.target.value); setFormError(null); }}
                      className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium"
                    />
                  </div>
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500" htmlFor="mt-clock-out">End Time (Clock Out)</label>
                    <input
                      id="mt-clock-out"
                      required
                      type="time"
                      value={formClockOut}
                      onChange={e => { setFormClockOut(e.target.value); setFormError(null); }}
                      className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium"
                    />
                  </div>
                </div>



                <div className="flex items-center justify-between rounded-lg bg-slate-50 px-4 py-3">
                  <span className="text-xs font-bold uppercase tracking-wider text-slate-500">Total logged</span>
                  <span className="text-sm font-black text-slate-800">
                    {draftMinutes === null ? '-' : formatMinutes(draftMinutes)}
                  </span>
                </div>



                {formError && (
                  <p className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-xs font-semibold leading-5 text-rose-700" role="alert">
                    {formError}
                  </p>
                )}

              </form>
            </div>
            
            <div className="border-t border-slate-100 p-6 bg-slate-50">
              <div className="flex gap-3">
                <button
                  type="button"
                  onClick={closeDrawer}
                  className="flex-1 rounded-lg border border-slate-200 bg-white py-2.5 text-sm font-bold text-slate-600 transition hover:bg-slate-50"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  form="time-form"
                  disabled={isSaving}
                  className={`flex-1 rounded-lg py-2.5 text-sm font-bold text-white shadow-md transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${GRADIENT_CYAN_PURPLE}`}
                >
                  {isSaving ? 'Saving...' : 'Save Entry'}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      </div>
    </V2Shell>
  );
};
