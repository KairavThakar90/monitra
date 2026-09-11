/**
 * Creating a task has to show the task.
 *
 * The reported symptom was "after creating a task it takes a few seconds to
 * appear". The cause was not slowness in the create itself: the Task Listing
 * screen does not render the project list at all, it renders
 * `getProjectTaskSummary` — a report that joins every task to the time tracked
 * against it. `createTask` patched the project caches, which that screen never
 * reads, so the new row only arrived when the report had been fetched again.
 *
 * These tests pin the two halves of the fix: the row lands in the report's
 * cache as soon as the server confirms the task, and a project list that was
 * fetched *without* its tasks does not have an array invented for it.
 */
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../baseApi';
import { projectsApi } from '../projectsApi';
import { reportsApi } from '../reportsApi';

const PROJECT_ID = 7;

/** What `POST /projects/{id}/tasks` answers with. */
const CREATED_TASK = {
  id: 501,
  project_id: PROJECT_ID,
  name: 'Design the homepage',
  assignee: null,
  status: { id: 1, name: 'Todo', color: '#CBD5E1' },
  created_at: '2026-09-11T09:30:00Z',
  updated_at: '2026-09-11T09:30:00Z',
};

const summaryArgs = { page: 1, limit: 10 };

const summaryPage = () => ({
  projects: [
    {
      id: PROJECT_ID,
      project_name: 'Website rebuild',
      created_date: '2026-01-01',
      status: { id: 1, name: 'Active', color: '#22C55E' },
      total_task_count: 1,
      total_task_seconds: 3600,
      total_task_hours: 1,
      total_task_time: '01:00:00',
      tasks: [
        {
          id: 500,
          task_name: 'Write the brief',
          task_created_date: '2026-01-02',
          total_tracked_seconds: 3600,
          total_tracked_hours: 1,
          total_tracked_time: '01:00:00',
        },
      ],
    },
  ],
  pagination: { page: 1, limit: 10, total_projects: 1, total_pages: 1 },
});

const slimProject = () => ({
  id: PROJECT_ID,
  project_name: 'Website rebuild',
  description: '',
  status: { id: 1, name: 'Active', color: '#22C55E' },
  leader: null,
  employees: [],
  deadline: null,
  billing_type: 'free',
  fixed_hours: null,
  organization_id: 1,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  // `include_tasks=false` — not asked for, so not present.
  tasks: null,
  employee_count: 0,
  task_count: 1,
});

const makeStore = () =>
  configureStore({
    reducer: { [baseApi.reducerPath]: baseApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(baseApi.middleware),
  });

let store: ReturnType<typeof makeStore>;

beforeEach(() => {
  const entries = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => entries.set(key, value),
    removeItem: (key: string) => entries.delete(key),
    clear: () => entries.clear(),
  });
  // A real Response, not a hand-rolled object: fetchBaseQuery calls
  // `response.clone()`, and a fresh one is needed per call because reading the
  // body consumes it.
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(CREATED_TASK), {
    status: 201,
    headers: { 'content-type': 'application/json' },
  })));
  store = makeStore();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const createTask = () =>
  store.dispatch(
    projectsApi.endpoints.createTask.initiate({
      projectId: PROJECT_ID,
      body: { name: CREATED_TASK.name, assignee_id: null, status_id: 1 },
    }),
  );

const summaryCache = () =>
  reportsApi.endpoints.getProjectTaskSummary.select(summaryArgs)(store.getState() as never)
    .data as any;

const allProjectsCache = (includeTasks: boolean) =>
  projectsApi.endpoints.getAllProjects.select({ includeTasks })(store.getState() as never)
    .data as any;

describe('createTask', () => {
  it('puts the new task into the task-summary report the screen actually renders', async () => {
    await store.dispatch(
      baseApi.util.upsertQueryData('getProjectTaskSummary' as never, summaryArgs as never, summaryPage() as never),
    );

    await createTask();

    const project = summaryCache().projects[0];
    expect(project.tasks.map((task: any) => task.task_name)).toEqual([
      'Write the brief',
      'Design the homepage',
    ]);
    expect(project.total_task_count).toBe(2);
  });

  it('reports the new task as having no tracked time, because it has none', async () => {
    await store.dispatch(
      baseApi.util.upsertQueryData('getProjectTaskSummary' as never, summaryArgs as never, summaryPage() as never),
    );

    await createTask();

    const added = summaryCache().projects[0].tasks.at(-1);
    expect(added.total_tracked_seconds).toBe(0);
    expect(added.total_tracked_time).toBe('00:00:00');
    expect(added.id).toBe(CREATED_TASK.id);
  });

  it('leaves a project it cannot see on the current page alone', async () => {
    const otherPage = summaryPage();
    otherPage.projects[0].id = 99;

    await store.dispatch(
      baseApi.util.upsertQueryData('getProjectTaskSummary' as never, summaryArgs as never, otherPage as never),
    );

    await createTask();

    // No row is invented for a project that is not on this page.
    expect(summaryCache().projects[0].tasks).toHaveLength(1);
    expect(summaryCache().projects[0].total_task_count).toBe(1);
  });

  it('does not invent a task array on a project list fetched without one', async () => {
    await store.dispatch(
      baseApi.util.upsertQueryData(
        'getAllProjects' as never,
        { includeTasks: false } as never,
        [slimProject()] as never,
      ),
    );

    await createTask();

    const projects = allProjectsCache(false);
    // Still "you did not ask", not "this project has exactly one task".
    expect(projects[0].tasks).toBeNull();
    // The count is the number the list column renders, so it still moves.
    expect(projects[0].task_count).toBe(2);
  });

  it('appends to the task array when the list was fetched with one', async () => {
    const withTasks = { ...slimProject(), tasks: [] as any[] };

    await store.dispatch(
      baseApi.util.upsertQueryData(
        'getAllProjects' as never,
        { includeTasks: true } as never,
        [withTasks] as never,
      ),
    );

    await createTask();

    const projects = allProjectsCache(true);
    expect(projects[0].tasks.map((task: any) => task.id)).toEqual([CREATED_TASK.id]);
    expect(projects[0].task_count).toBe(2);
  });
});
