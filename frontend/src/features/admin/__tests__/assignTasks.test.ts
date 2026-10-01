import { describe, expect, it } from 'vitest';

import type { Project, ProjectTask, ProjectUser } from '../../../store/api/projectsApi';
import { ALL_TIME_RANGE, rangeFor } from '../../dashboard/v2/filters';
import { filterByCreated, filterByProjectIds, filterProjects, holdersOf, memberOptions, sameMembers } from '../assignTasks';

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
