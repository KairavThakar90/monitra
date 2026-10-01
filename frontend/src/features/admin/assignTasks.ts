/**
 * The rules behind the Assign Tasks screen that are not rendering.
 *
 * Kept out of the components so they can be tested without mounting anything,
 * and so the dialog and the page cannot disagree about who holds a task.
 */
import type { Project, ProjectTask, ProjectUser } from '../../store/api/projectsApi';

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

/** Two lists of ids name the same people, whatever the order. */
export const sameMembers = (a: number[], b: number[]): boolean =>
  a.length === b.length && a.every((id) => b.includes(id));
