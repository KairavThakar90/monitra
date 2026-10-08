/**
 * The rules behind the Assign Tasks screen that are not rendering.
 *
 * Kept out of the components so they can be tested without mounting anything,
 * and so the dialog and the page cannot disagree about who holds a task.
 */
import type { Project, ProjectTask, ProjectUser } from '../../store/api/projectsApi';
import type { DateRange } from '../dashboard/v2/filters';

/**
 * Everyone holding a task, primary assignee first.
 *
 * `assignees` is the whole set. A response from a backend that predates
 * multi-member tasks carries only `assignee`, and that one person is still
 * holding the task -- falling back to it means "one member", never "nobody".
 */
export const holdersOf = (task: ProjectTask): ProjectUser[] =>
  task.assignees ?? (task.assignee ? [task.assignee] : []);

/**
 * Who the Members picker offers for a task.
 *
 * The project's members with the `employee` role: the backend lets only an
 * active employee who belongs to the project hold a task, so offering anyone
 * else would be a choice the save then refuses. Plus whoever already holds the
 * task, even if they have since left the project -- they must stay visible so
 * they can be seen and removed, and the backend lets an existing holder stay.
 */
export const memberOptions = (
  project: Project | undefined,
  task: ProjectTask | undefined,
): ProjectUser[] => {
  const options = new Map<number, ProjectUser>();
  for (const member of project?.employees ?? []) {
    if (member.role === 'employee') options.set(member.id, member);
  }
  for (const holder of task ? holdersOf(task) : []) {
    if (!options.has(holder.id)) options.set(holder.id, holder);
  }
  return [...options.values()].sort((a, b) => (a.name || '').localeCompare(b.name || ''));
};

/**
 * The projects (and, inside them, the tasks) matching a search.
 *
 * A project whose own name matches keeps all its tasks; otherwise it is kept
 * with only the tasks whose name -- or whose assignee's name -- matches, and
 * dropped when none does. An empty search returns the input untouched.
 */
export const filterProjects = (projects: Project[], query: string): Project[] => {
  const needle = query.trim().toLowerCase();
  if (!needle) return projects;
  const result: Project[] = [];
  for (const project of projects) {
    if ((project.project_name || '').toLowerCase().includes(needle)) {
      result.push(project);
      continue;
    }
    const tasks = (project.tasks ?? []).filter(
      (task) =>
        (task.name || '').toLowerCase().includes(needle) ||
        holdersOf(task).some((holder) => (holder.name || '').toLowerCase().includes(needle)),
    );
    if (tasks.length > 0) result.push({ ...project, tasks });
  }
  return result;
};

/**
 * The people the Assign Tasks member filter offers: everyone who holds a task or could be given one --
 * the `employee` members of the projects, the same people the Assign dialog offers (see `memberOptions`)
 * -- plus anyone holding a task who has since left their project, so a filter on them still works.
 * Someone who cannot hold a task and holds none is not listed: choosing them could only ever show an
 * empty page. Each person once, by name.
 */
export const taskHolderOptions = (projects: Project[]): ProjectUser[] => {
  const people = new Map<number, ProjectUser>();
  for (const project of projects) {
    for (const member of project.employees ?? []) {
      if (member.role === 'employee') people.set(member.id, member);
    }
    for (const task of project.tasks ?? []) {
      for (const holder of holdersOf(task)) {
        if (!people.has(holder.id)) people.set(holder.id, holder);
      }
    }
  }
  return [...people.values()].sort((a, b) => (a.name || '').localeCompare(b.name || ''));
};

/**
 * The projects with only the tasks held by one of the members in `ids`, dropping a project left with
 * none. An empty selection means "everyone" and filters nothing, as in every other member filter.
 *
 * "Held by" is meant strictly: a task nobody is assigned to is shared by the whole project but is
 * not held by any one member, so it is not shown while a member is chosen. A task held by several
 * members is shown when any of them is chosen.
 */
export const filterByHolders = (projects: Project[], ids: string[]): Project[] => {
  if (ids.length === 0) return projects;
  const result: Project[] = [];
  for (const project of projects) {
    const tasks = (project.tasks ?? []).filter((task) => holdersOf(task).some((holder) => ids.includes(String(holder.id))));
    if (tasks.length > 0) result.push({ ...project, tasks });
  }
  return result;
};

/** Two lists of ids name the same people, whatever the order. */
export const sameMembers = (a: number[], b: number[]): boolean =>
  a.length === b.length && a.every((id) => b.includes(id));

/**
 * Only the projects whose id is in `ids`. An empty selection means "all
 * projects", as in every other project filter.
 */
export const filterByProjectIds = (projects: Project[], ids: string[]): Project[] =>
  ids.length === 0 ? projects : projects.filter((project) => ids.includes(String(project.id)));

/** A timestamp's calendar date in the viewer's own time zone, as `YYYY-MM-DD`. */
const localDate = (timestamp: string): string => {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
};

/**
 * The projects with only the tasks **created** inside `range`, dropping a
 * project left with none. The page has no tracked-time data, so creation is
 * the date a task honestly has; the "All Time" range (empty bounds) filters
 * nothing. Both ends are inclusive, matching the date picker.
 */
export const filterByCreated = (projects: Project[], range: DateRange): Project[] => {
  if (!range.from || !range.to) return projects;
  const result: Project[] = [];
  for (const project of projects) {
    const tasks = (project.tasks ?? []).filter((task) => {
      const created = localDate(task.created_at);
      return created !== '' && created >= range.from && created <= range.to;
    });
    if (tasks.length > 0) result.push({ ...project, tasks });
  }
  return result;
};
