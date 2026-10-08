import { IST_TIME_ZONE, formatISTDate, formatISTTime12 } from '../../utils/duration';
import type { ActivityLogEntry, ActivityLogMemberGroup } from '../../store/api/activityLogsApi';

/**
 * How the Logs page words and arranges the activity trail.
 *
 * Pure functions, kept apart from the screen so the wording and the grouping
 * can be tested without rendering anything. Nothing here invents data: an
 * action the page has no label for is shown in its own words, and a row with
 * no recorded client simply has no source.
 */

/** A module's name and the tint its badge carries. */
export const MODULE_STYLES: Record<string, { label: string; text: string; bg: string; dot: string }> = {
  auth: { label: 'Sign-in', text: '#1D4ED8', bg: '#EFF6FF', dot: '#3B82F6' },
  desktop: { label: 'Desktop app', text: '#6D28D9', bg: '#F5F3FF', dot: '#8B5CF6' },
  timer: { label: 'Timer', text: '#047857', bg: '#ECFDF5', dot: '#10B981' },
  manual_time: { label: 'Manual time', text: '#B45309', bg: '#FFFBEB', dot: '#F59E0B' },
  project: { label: 'Project', text: '#0E7490', bg: '#ECFEFF', dot: '#06B6D4' },
  task: { label: 'Task', text: '#4338CA', bg: '#EEF2FF', dot: '#6366F1' },
  member: { label: 'Member', text: '#BE123C', bg: '#FFF1F2', dot: '#F43F5E' },
  client: { label: 'Client', text: '#0F766E', bg: '#F0FDFA', dot: '#14B8A6' },
  feedback: { label: 'Feedback', text: '#A16207', bg: '#FEFCE8', dot: '#EAB308' },
  screenshot: { label: 'Screenshot', text: '#0369A1', bg: '#F0F9FF', dot: '#0EA5E9' },
  system: { label: 'System', text: '#334155', bg: '#F1F5F9', dot: '#64748B' },
};

const FALLBACK_STYLE = { text: '#334155', bg: '#F1F5F9', dot: '#94A3B8' };

/** `manual_time_requested` -> `Manual time requested`. */
const humanize = (value: string) => {
  const words = value.replace(/_/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : value;
};

export const moduleStyle = (module: string) =>
  MODULE_STYLES[module] ?? { label: humanize(module), ...FALLBACK_STYLE };

export const moduleLabel = (module: string) => moduleStyle(module).label;

/** What each recorded action is called on screen. */
const ACTION_LABELS: Record<string, string> = {
  login: 'Signed in',
  logout: 'Signed out',
  app_opened: 'Opened the app',
  app_closed: 'Closed the app',
  timer_started: 'Started timer',
  timer_stopped: 'Stopped timer',
  entry_transferred: 'Moved time entry',
  manual_time_requested: 'Requested manual time',
  manual_time_approved: 'Approved manual time',
  manual_time_rejected: 'Rejected manual time',
  manual_time_withdrawn: 'Withdrew manual time',
  project_created: 'Created project',
  project_updated: 'Updated project',
  project_archived: 'Archived project',
  project_status_changed: 'Changed project status',
  project_leader_changed: 'Changed project leader',
  project_owner_changed: 'Changed project owner',
  project_member_assigned: 'Assigned to project',
  project_member_removed: 'Removed from project',
  task_created: 'Added task',
  task_updated: 'Updated task',
  task_archived: 'Archived task',
  task_status_changed: 'Changed task status',
  task_assigned: 'Assigned task',
  task_unassigned: 'Removed from task',
  member_created: 'Added member',
  member_updated: 'Updated member',
  member_deactivated: 'Deactivated member',
  member_deleted: 'Deleted member',
  login_excluded: 'Excluded from signing in',
  login_allowed: 'Allowed to sign in',
  add_tasks_excluded: 'Excluded from adding tasks',
  add_tasks_allowed: 'Allowed to add tasks',
  add_nonbillable_tasks_excluded: 'Excluded from adding Non billable tasks',
  add_nonbillable_tasks_allowed: 'Allowed to add Non billable tasks',
  feedback_status_changed: 'Updated feedback',
  screenshot_notice_sent: 'Sent screenshot notice',
  screenshot_deleted: 'Deleted screenshot',
  client_invited: 'Invited client',
  client_invitation_resent: 'Resent client invitation',
  client_access_changed: 'Changed client access',
  client_deactivated: 'Deactivated client',
  maintenance_enabled: 'Enabled maintenance notice',
  maintenance_disabled: 'Disabled maintenance notice',
  desktop_notification_created: 'Created desktop notification',
  desktop_notification_updated: 'Updated desktop notification',
  desktop_notification_deleted: 'Deleted desktop notification',
  desktop_notification_pushed: 'Pushed desktop notification',
};

/**
 * The action's label. An action this build has never heard of -- one a newer
 * backend started writing -- is shown in its own words rather than hidden.
 */
export const actionLabel = (action: string) => ACTION_LABELS[action] ?? humanize(action);

/**
 * The label for one row. A sign-in or sign-out made from the desktop application
 * says so -- "Signed in to the desktop app" -- because which client a person
 * signed in from is what the log is read for; every other action keeps its
 * plain label and shows its client beside it.
 */
export const entryActionLabel = (entry: Pick<ActivityLogEntry, 'action' | 'source'>) => {
  if (entry.source === 'desktop' && entry.action === 'login') return 'Signed in to the desktop app';
  if (entry.source === 'desktop' && entry.action === 'logout') return 'Signed out of the desktop app';
  return actionLabel(entry.action);
};

/** "Desktop 1.3.0", "Web", "API" -- or null when no client was recorded. */
export const sourceLabel = (entry: Pick<ActivityLogEntry, 'source' | 'client_version'>): string | null => {
  if (!entry.source) return null;
  if (entry.source === 'desktop') return entry.client_version ? `Desktop ${entry.client_version}` : 'Desktop';
  if (entry.source === 'web') return 'Web';
  if (entry.source === 'api') return 'API';
  return humanize(entry.source);
};

/** "Apollo › Guidance", "Apollo", or null when the action concerned neither. */
export const contextLabel = (entry: Pick<ActivityLogEntry, 'project_name' | 'task_name'>): string | null => {
  const parts = [entry.project_name, entry.task_name].filter(Boolean);
  return parts.length ? parts.join(' › ') : null;
};

const istDayFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: IST_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

/** The IST calendar day an instant falls on, `YYYY-MM-DD`. */
export const istDayOf = (iso: string): string => {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? '' : istDayFormatter.format(at);
};

export interface DayGroup {
  /** IST calendar day, `YYYY-MM-DD`. */
  day: string;
  entries: ActivityLogEntry[];
}

/**
 * One employee's rows split into IST days, order preserved.
 *
 * The backend sends them newest first, so the days come out newest first too
 * and each day's rows stay newest first. A UTC day boundary would put a
 * 02:00 IST sign-in under the previous day's heading.
 */
export const groupEntriesByDay = (entries: ActivityLogEntry[]): DayGroup[] => {
  const groups: DayGroup[] = [];
  for (const entry of entries) {
    const day = istDayOf(entry.created_at);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.entries.push(entry);
    else groups.push({ day, entries: [entry] });
  }
  return groups;
};

/**
 * Narrow the groups to the chosen employees. No selection means everyone --
 * and the choice only ever narrows what the backend already sent.
 */
export const filterMembers = (
  members: ActivityLogMemberGroup[],
  selectedIds: string[],
): ActivityLogMemberGroup[] =>
  selectedIds.length === 0 ? members : members.filter((member) => selectedIds.includes(String(member.user_id)));

export const countEntries = (members: ActivityLogMemberGroup[]) =>
  members.reduce((total, member) => total + member.entries.length, 0);

export const LOG_CSV_HEADERS = [
  'Employee', 'Email', 'Date (IST)', 'Time (IST)', 'Category', 'Action', 'Details', 'Project', 'Task', 'Source', 'IP address',
];

/** The rows of the export, in the order they are on screen. */
export const logsToCsvRows = (members: ActivityLogMemberGroup[]): (string | number)[][] =>
  members.flatMap((member) =>
    member.entries.map((entry) => [
      member.name,
      member.email ?? '',
      formatISTDate(entry.created_at),
      formatISTTime12(entry.created_at),
      moduleLabel(entry.module),
      entryActionLabel(entry),
      entry.description ?? '',
      entry.project_name ?? '',
      entry.task_name ?? '',
      sourceLabel(entry) ?? '',
      entry.ip_address ?? '',
    ]),
  );
