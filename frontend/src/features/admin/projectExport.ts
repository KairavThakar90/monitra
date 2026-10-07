import type { Project } from '../../store/api/projectsApi';
import { istDateISO } from '../../utils/duration';
import { projectCategoryLabel } from '../../utils/projectCategory';

/**
 * What the Project Management export writes, apart from the dialog that asks for
 * it (`ProjectExportDialog`): the columns on offer, each project's value for
 * them, and the spreadsheet-safety rule for text. Plain functions, so they can
 * be tested without rendering anything -- the same split the Reports page has
 * between `ExportDialog` and `timesheetExport`.
 */

export type ExportColumnKey =
  | 'project' | 'organization' | 'description' | 'status' | 'owner' | 'leader' | 'team' | 'tasks' | 'billing' | 'created' | 'deadline';

export const EXPORT_COLUMNS: { key: ExportColumnKey; label: string }[] = [
  { key: 'project', label: 'Project' },
  { key: 'organization', label: 'Organization' },
  { key: 'description', label: 'Description' },
  { key: 'status', label: 'Status' },
  { key: 'owner', label: 'Owner' },
  { key: 'leader', label: 'Leader' },
  { key: 'team', label: 'Team Members' },
  { key: 'tasks', label: 'Tasks' },
  { key: 'billing', label: 'Billing' },
  { key: 'created', label: 'Created' },
  { key: 'deadline', label: 'Deadline' },
];

/**
 * Text bound for a spreadsheet cell, made inert if it would be read as a
 * formula.
 *
 * Project names, descriptions and people's names are typed by users, and a cell
 * that begins with `=`, `+`, `-` or `@` is evaluated by Excel and Sheets on
 * open (`=HYPERLINK(...)` can send data elsewhere). A leading apostrophe makes
 * the application show the text as typed. Applied only here, to what this file
 * writes; it is output encoding, not a change to anything stored.
 */
export const csvText = (value: string): string => (/^[=+\-@\t\r]/.test(value) ? `'${value}` : value);

/** One project's value for each exportable column. */
export const PROJECT_EXPORT_VALUES: Record<ExportColumnKey, (project: Project) => string | number> = {
  project: (project) => csvText(project.project_name),
  // Blank, not a dash or a made-up label, for a project that has none.
  organization: (project) => (project.category ? projectCategoryLabel(project.category) : ''),
  description: (project) => csvText(project.description || ''),
  status: (project) => csvText(project.status?.name || ''),
  owner: (project) => csvText(project.owner?.name || 'Unassigned'),
  leader: (project) => csvText(project.leader?.name || 'Unassigned'),
  team: (project) => csvText((project.employees || []).map((employee) => employee.name).join('; ')),
  tasks: (project) => project.task_count ?? (project.tasks || []).length,
  billing: (project) =>
    project.billing_type === 'fixed'
      ? `${project.fixed_hours || 0} Hours`
      : project.billing_type === 'non_billing' ? 'Non Billing' : 'Free Time',
  // The IST day it was created -- the one the table shows and the date filter
  // compares -- as ISO. Blank, not a made-up day, if the API sent none.
  created: (project) => istDateISO(project.created_at),
  // ISO, so a spreadsheet sorts and parses it; a deadline is a date, not an instant.
  deadline: (project) => (project.deadline ? project.deadline.split('T')[0] : 'No Deadline'),
};

export const buildProjectRows = (projects: Project[], columns: ExportColumnKey[]): (string | number)[][] =>
  projects.map((project) => columns.map((key) => PROJECT_EXPORT_VALUES[key](project)));
