import { describe, expect, it } from 'vitest';

import type { Project, ProjectTask, ProjectUser } from '../../../store/api/projectsApi';
import { ALL_TIME_RANGE, rangeFor } from '../../dashboard/v2/filters';
import {
  filterByCreated,
  filterByHolders,
  filterByProjectIds,
  filterProjects,
  holdersOf,
  memberOptions,
  sameMembers,
  taskHolderOptions,
} from '../assignTasks';

const person = (id: number, name: string, role = 'employee'): ProjectUser => ({
  id, name, email: `${name.toLowerCase()}@example.com`, role,
});

const task = (id: number, name: string, over: Partial<ProjectTask> = {}): ProjectTask => ({
  id,
  project_id: 1,
  name,
  assignee: null,
  status: { id: 1, name: 'Todo', color: '#CBD5E1' },
  estimated_hours: null,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
  ...over,
});

const project = (id: number, name: string, tasks: ProjectTask[], employees: ProjectUser[] = []): Project => ({
  id,
  project_name: name,
  description: '',
  status: { id: 1, name: 'Active', color: '#10B981' },
  leader: null,
  employees,
  deadline: null,
  billing_type: 'free',
  fixed_hours: null,
  organization_id: 1,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
  tasks,
  employee_count: employees.length,
  task_count: tasks.length,
});

const ana = person(1, 'Ana');
const ben = person(2, 'Ben');
const cal = person(3, 'Cal');

describe('holdersOf', () => {
  it('is the whole set when the server sends one', () => {
    expect(holdersOf(task(1, 't', { assignee: ana, assignees: [ana, ben] })).map((p) => p.id)).toEqual([1, 2]);
  });

  it('falls back to the lone assignee from a backend that has no set, never to nobody', () => {
    expect(holdersOf(task(1, 't', { assignee: ana })).map((p) => p.id)).toEqual([1]);
  });

  it('is empty for an unassigned task', () => {
    expect(holdersOf(task(1, 't'))).toEqual([]);
    expect(holdersOf(task(1, 't', { assignees: [] }))).toEqual([]);
  });
});

describe('memberOptions', () => {
  it('offers only the project\'s employees: the backend refuses anyone else', () => {
    const p = project(1, 'P', [], [ana, person(9, 'Lead', 'leader'), person(8, 'Root', 'administrator'), ben]);
    expect(memberOptions(p, undefined).map((m) => m.id)).toEqual([1, 2]);
  });

  it('keeps a current holder who has left the project, so they can be seen and removed', () => {
    const p = project(1, 'P', [], [ana]);
    const t = task(1, 't', { assignee: ben, assignees: [ben] });
    expect(memberOptions(p, t).map((m) => m.id)).toEqual([1, 2]);
  });

  it('lists each person once even when they are both a member and a holder', () => {
    const p = project(1, 'P', [], [ana, ben]);
    const t = task(1, 't', { assignees: [ana] });
    expect(memberOptions(p, t).map((m) => m.id)).toEqual([1, 2]);
  });

  it('is sorted by name and empty when there is no project', () => {
    const p = project(1, 'P', [], [cal, ana, ben]);
    expect(memberOptions(p, undefined).map((m) => m.name)).toEqual(['Ana', 'Ben', 'Cal']);
    expect(memberOptions(undefined, undefined)).toEqual([]);
  });
});

describe('filterProjects', () => {
  const projects = [
    project(1, 'Website rebuild', [task(10, 'Design homepage', { assignees: [ana] }), task(11, 'Write copy')]),
    project(2, 'Mobile app', [task(20, 'Login screen', { assignees: [ben, cal] })]),
  ];

  it('returns the input untouched for an empty search', () => {
    expect(filterProjects(projects, '   ')).toBe(projects);
  });

  it('keeps every task of a project whose own name matches', () => {
    const result = filterProjects(projects, 'website');
    expect(result.map((p) => p.id)).toEqual([1]);
    expect(result[0].tasks).toHaveLength(2);
  });

  it('keeps only the matching tasks of a project that matches through them', () => {
    const result = filterProjects(projects, 'homepage');
    expect(result.map((p) => p.id)).toEqual([1]);
    expect(result[0].tasks!.map((t) => t.id)).toEqual([10]);
  });

  it('matches on a member\'s name', () => {
    const result = filterProjects(projects, 'cal');
    expect(result.map((p) => p.id)).toEqual([2]);
    expect(result[0].tasks!.map((t) => t.id)).toEqual([20]);
  });

  it('is case-insensitive and drops projects with no match', () => {
    expect(filterProjects(projects, 'LOGIN').map((p) => p.id)).toEqual([2]);
    expect(filterProjects(projects, 'zzz')).toEqual([]);
  });

  it('does not mutate the projects it was given', () => {
    filterProjects(projects, 'homepage');
    expect(projects[0].tasks).toHaveLength(2);
  });
});

describe('sameMembers', () => {
  it('ignores order but not membership', () => {
    expect(sameMembers([1, 2], [2, 1])).toBe(true);
    expect(sameMembers([1, 2], [1, 3])).toBe(false);
    expect(sameMembers([1], [1, 2])).toBe(false);
    expect(sameMembers([], [])).toBe(true);
  });
});

describe('filterByProjectIds', () => {
  const projects = [project(1, 'A', []), project(2, 'B', []), project(3, 'C', [])];

  it('an empty selection means every project', () => {
    expect(filterByProjectIds(projects, [])).toBe(projects);
  });

  it('keeps only the selected projects, matching the picker string ids', () => {
    expect(filterByProjectIds(projects, ['1', '3']).map((p) => p.id)).toEqual([1, 3]);
  });
});

describe('filterByCreated', () => {
  const at = (day: string, time = '12:00:00') => new Date(`${day}T${time}`).toISOString();
  const projects = [
    project(1, 'A', [task(10, 'old', { created_at: at('2026-09-01') }), task(11, 'new', { created_at: at('2026-10-01') })]),
    project(2, 'B', [task(20, 'older', { created_at: at('2026-08-15') })]),
  ];
  const span = (from: string, to: string) => ({ preset: 'custom' as const, from, to });

  it('All Time filters nothing', () => {
    expect(filterByCreated(projects, ALL_TIME_RANGE)).toBe(projects);
    expect(rangeFor('all', ALL_TIME_RANGE)).toEqual(ALL_TIME_RANGE);
  });

  it('keeps only tasks created in the range and drops a project left with none', () => {
    const result = filterByCreated(projects, span('2026-09-15', '2026-10-31'));
    expect(result.map((p) => p.id)).toEqual([1]);
    expect(result[0].tasks!.map((t) => t.id)).toEqual([11]);
  });

  it('is inclusive at both ends, in the viewer own day', () => {
    const edge = [project(1, 'A', [task(1, 'start', { created_at: at('2026-10-01', '00:00:00') }), task(2, 'end', { created_at: at('2026-10-02', '23:59:59') })])];
    expect(filterByCreated(edge, span('2026-10-01', '2026-10-02'))[0].tasks).toHaveLength(2);
    expect(filterByCreated(edge, span('2026-10-02', '2026-10-02'))[0].tasks!.map((t) => t.id)).toEqual([2]);
  });

  it('a task with an unreadable date never matches a bounded range', () => {
    const bad = [project(1, 'A', [task(1, 'x', { created_at: 'not a date' })])];
    expect(filterByCreated(bad, span('2026-01-01', '2026-12-31'))).toEqual([]);
  });

  it('does not mutate the projects it was given', () => {
    filterByCreated(projects, span('2026-10-01', '2026-10-01'));
    expect(projects[0].tasks).toHaveLength(2);
  });
});

describe('filterByHolders', () => {
  const alpha = project(1, 'Alpha', [
    task(1, 'Wire up login', { assignees: [ana] }),
    task(2, 'Write the docs', { assignees: [ben] }),
    task(3, 'Fix the build', { assignees: [ana, ben] }),
    task(4, 'Triage the backlog'),                                   // unassigned: shared by the whole project
  ]);
  const beta = project(2, 'Beta', [task(5, 'Design the logo', { assignees: [cal] })]);

  it('changes nothing for an empty selection', () => {
    const projects = [alpha, beta];
    expect(filterByHolders(projects, [])).toBe(projects);
  });

  it('keeps only the tasks the chosen member holds, and drops a project left with none', () => {
    const result = filterByHolders([alpha, beta], ['1']);

    expect(result.map((p) => p.project_name)).toEqual(['Alpha']);
    expect(result[0].tasks!.map((t) => t.name)).toEqual(['Wire up login', 'Fix the build']);
  });

  it('shows a task held by several members when any one of them is chosen', () => {
    const result = filterByHolders([alpha, beta], ['2']);
    expect(result[0].tasks!.map((t) => t.name)).toEqual(['Write the docs', 'Fix the build']);
  });

  it('is the union of everyone chosen', () => {
    const result = filterByHolders([alpha, beta], ['1', '3']);

    expect(result.map((p) => p.project_name)).toEqual(['Alpha', 'Beta']);
    expect(result[0].tasks!.map((t) => t.name)).toEqual(['Wire up login', 'Fix the build']);
    expect(result[1].tasks!.map((t) => t.name)).toEqual(['Design the logo']);
  });

  it('does not show an unassigned task, which no one member holds', () => {
    const names = filterByHolders([alpha], ['1', '2']).flatMap((p) => p.tasks!.map((t) => t.name));
    expect(names).not.toContain('Triage the backlog');
  });

  it('finds the holder of a task from a backend that sends only the lone assignee', () => {
    const old = project(3, 'Old', [task(6, 'Legacy task', { assignee: cal })]);
    expect(filterByHolders([old], ['3'])[0].tasks!.map((t) => t.name)).toEqual(['Legacy task']);
  });

  it('answers with nothing for a member who holds nothing, and does not change the input', () => {
    const before = JSON.stringify([alpha, beta]);
    expect(filterByHolders([alpha, beta], ['99'])).toEqual([]);
    expect(JSON.stringify([alpha, beta])).toBe(before);
  });

  it('does not match an id that merely contains the chosen one', () => {
    const eleven = person(11, 'Eve');
    const p = project(4, 'Gamma', [task(7, 'Eleven task', { assignees: [eleven] })]);
    expect(filterByHolders([p], ['1'])).toEqual([]);
  });
});

describe('taskHolderOptions', () => {
  it('offers the employees of the projects and whoever holds a task, each once, by name', () => {
    const manager = person(9, 'Zed', 'manager');
    const left = person(7, 'Gus');                                   // holds a task but is no longer on the project
    const projects = [
      project(1, 'Alpha', [task(1, 't', { assignees: [left] })], [cal, ana, manager]),
      project(2, 'Beta', [], [ana, ben]),
    ];

    expect(taskHolderOptions(projects).map((p) => p.name)).toEqual(['Ana', 'Ben', 'Cal', 'Gus']);
  });

  it('leaves out someone who cannot hold a task and holds none', () => {
    const client = person(8, 'Client Co', 'client');
    const manager = person(9, 'Zed', 'manager');
    const projects = [project(1, 'Alpha', [task(1, 't', { assignees: [ana] })], [ana, client, manager])];

    expect(taskHolderOptions(projects).map((p) => p.id)).toEqual([1]);
  });

  it('is empty when there are no projects', () => {
    expect(taskHolderOptions([])).toEqual([]);
  });
});
