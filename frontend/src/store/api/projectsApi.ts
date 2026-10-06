import { baseApi } from './baseApi';
import { patchEveryCachedQuery } from './optimistic';
import { ENDPOINTS } from '../../api/endpoints';
import type { ProjectCategory } from '../../utils/projectCategory';

/**
 * A role the server recognises for a member. `value` is what the API stores
 * and filters on (`admin` | `hr` | `leader` | `employee`); `role_type` is the
 * label to show. The list is served by `/project-management/metadata` and is
 * the only place the set of roles is defined — never re-declare it in a
 * component, or adding a role server-side silently fails to reach the UI.
 */
export interface ProjectRole {
  id: number;
  role_type: string;
  value: string;
}

export interface ProjectMetadata {
  roles: ProjectRole[];
  project_statuses: { id: number; project_status: string; color: string }[];
  task_statuses: { id: number; task_status: string; color: string }[];
}

export interface ProjectUser {
  id: number;
  name: string;
  email: string;
  role: string;
}

export interface ProjectTask {
  id: number;
  project_id: number;
  name: string;
  /** The primary assignee — the one the desktop and WFPM read. */
  assignee: ProjectUser | null;
  /**
   * Everyone holding the task, primary first. Optional so a cached row from a
   * backend that predates multi-member tasks still reads: absent means "just
   * `assignee`", never "nobody".
   */
  assignees?: ProjectUser[];
  status: { id: number; name: string; color: string };
  /** The task's budgeted hours — what the client portal's Billing page shows
   * as the task's total. `null` when no budget was set. */
  estimated_hours: number | null;
  created_at: string;
  updated_at: string;
}

export interface Project {
  id: number;
  project_name: string;
  description: string;
  status: { id: number; name: string; color: string };
  /**
   * The member responsible for this project — a project-level relationship,
   * not a role, shaped exactly like `leader`. `null` for projects that predate
   * owners. Optional so a cached row from before the field existed still reads.
   */
  owner?: ProjectUser | null;
  leader: ProjectUser | null;
  employees: ProjectUser[];
  deadline: string | null;
  billing_type: string;
  /**
   * 'kyle' | 'st', or `null` for an uncategorised project. Optional so a
   * cached row from before the field existed still reads.
   */
  category?: ProjectCategory | null;
  fixed_hours: string | null;
  organization_id: number;
  created_at: string;
  updated_at: string;
  /**
   * `null` when the request passed `include_tasks=false` — not the same thing
   * as `[]`, which means the project genuinely has no tasks. Screens that only
   * need the number read `task_count`, which is always populated.
   */
  tasks: ProjectTask[] | null;
  employee_count: number;
  task_count: number;
}

export interface ProjectListResponse {
  items: Project[];
  pagination: {
    page: number;
    limit: number;
    total: number;
    total_pages: number;
  };
}

/** All-time tracked hours for one project — a separate call from `getProjects`
 * so that response (also read verbatim by the desktop client) never has to
 * change shape to carry it. See `ProjectManagementService.hours_summary`. */
export interface ProjectHoursSummary {
  project_id: number;
  /** Time on ordinary work tasks — excludes the four seeded default
   * (internal) tasks. This is the figure a fixed budget is measured
   * against. */
  total_used_seconds: number;
  total_used_hours: number;
  /** Time on the project's seeded default tasks (client updates, internal
   * discussion…). Optional so a backend predating the split still renders. */
  internal_seconds?: number;
  internal_hours?: number;
  /** Used + internal. */
  total_tracked_seconds?: number;
  total_tracked_hours?: number;
  /** Earliest tracked session against the project -- distinct from its
   * `created_at`. `null` when nothing has ever been tracked against it. */
  started_at: string | null;
}

export interface CreateProjectPayload {
  project_name: string;
  description: string;
  status_id: number;
  /** Required by the backend when creating; omitted on an edit that keeps the current owner. */
  owner_id?: number;
  leader_id: number | null;
  employee_ids: number[];
  deadline: string | null;
  billing_type: string;
  fixed_hours: number | null;
  /**
   * Optional. On a create, omitted or `null` means uncategorised. On an edit,
   * omitted leaves the category alone and `null` clears it.
   */
  category?: ProjectCategory | null;
}

export type GetProjectsArgs = {
  page?: number;
  limit?: number;
  search?: string;
  status_id?: number | null;
  leader_id?: number | null;
  /** Only projects of these billing types ('fixed', 'free', 'non_billing'); empty or absent means all. */
  billing_type?: string[] | null;
  /** Only projects tagged with this category. */
  category?: ProjectCategory | null;
  /** Only projects staffed with at least one of these members. */
  employee_ids?: number[];
};

type ThunkParts = { dispatch: (action: any) => any; getState: () => any };

/** Runs `mutate` against both project caches: the paginated list and the load-everything one. */
const patchProjectLists = (
  parts: ThunkParts,
  mutate: (items: Project[], draft: any, arg: any) => void,
) => {
  const paged = patchEveryCachedQuery(parts, 'getProjects', (draft, arg) => {
    if (draft?.items) mutate(draft.items, draft, arg);
  });
  const all = patchEveryCachedQuery(parts, 'getAllProjects', (draft, arg) => {
    if (Array.isArray(draft)) mutate(draft, draft, arg);
  });
  return {
    undo: () => {
      paged.undo();
      all.undo();
    },
  };
};

/**
 * Shows a newly created task on the Task Listing screen without waiting for
 * the report behind it to be re-fetched.
 *
 * That screen does not render `getProjects` at all — it renders
 * `getProjectTaskSummary`, which joins every task to the time tracked against
 * it and takes the better part of a second to answer. Patching only the
 * project caches therefore left the author staring at an unchanged page until
 * the refetch landed: the "task takes a few seconds to appear" report.
 *
 * The tracked total is a real zero, not a placeholder: a task created a moment
 * ago has had no time booked against it, and its project's total is unchanged.
 * That is why the caller does not refetch the report afterwards. The patched
 * row is already what the report would return, and re-running the query would
 * put the delay straight back: while a query is re-fetching, `useQuery` keeps
 * serving the snapshot it held when the previous request fulfilled, so a patch
 * written mid-flight stays in the cache but never reaches the screen until the
 * new response lands.
 */
const patchTaskSummaries = (
  parts: ThunkParts,
  projectId: number,
  task: ProjectTask,
) =>
  patchEveryCachedQuery(parts, 'getProjectTaskSummary', (draft) => {
    const project = draft?.projects?.find((candidate: any) => candidate.id === projectId);
    // The project is not on the page being viewed (another page, or filtered
    // out). Nothing to show, and inventing a row for it would be worse.
    if (!project) return;
    if (!Array.isArray(project.tasks)) return;
    if (project.tasks.some((existing: any) => existing.id === task.id)) return;
    // The report orders a project's tasks oldest first, so the newest belongs
    // at the end — where the server will also put it.
    project.tasks.push({
      id: task.id,
      task_name: task.name,
      // The report returns a calendar date (`created_at.date()` server-side),
      // not a timestamp. Storing the same shape keeps the patched row and the
      // fetched one identical rather than merely equivalent.
      task_created_date: String(task.created_at).slice(0, 10),
      total_tracked_seconds: 0,
      total_tracked_hours: 0,
      total_tracked_time: '00:00:00',
      estimated_hours: task.estimated_hours ?? null,
    });
    project.total_task_count = (project.total_task_count || 0) + 1;
  });

export const projectsApi = baseApi.injectEndpoints({
  endpoints: (builder) => ({
    getProjectMetadata: builder.query<ProjectMetadata, void>({
      query: () => ENDPOINTS.PROJECTS.METADATA,
    }),

    getProjects: builder.query<ProjectListResponse, GetProjectsArgs>({
      query: (params) => {
        let url = `${ENDPOINTS.PROJECTS.GET_ALL}?page=${params.page || 1}&limit=${params.limit || 20}`;
        if (params.search) url += `&search=${encodeURIComponent(params.search)}`;
        if (params.status_id) url += `&status_id=${params.status_id}`;
        if (params.leader_id) url += `&leader_id=${params.leader_id}`;
        for (const type of params.billing_type || []) url += `&billing_type=${type}`;
        if (params.category) url += `&category=${params.category}`;
        for (const id of params.employee_ids || []) url += `&employee_ids=${id}`;
        return url;
      },
      providesTags: (result) =>
        result
          ? [
              ...result.items.map(({ id }) => ({ type: 'Project' as const, id })),
              { type: 'Project' as const, id: 'LIST' },
            ]
          : [{ type: 'Project' as const, id: 'LIST' }],
    }),

    /**
     * Every project the caller can see, across all pages.
     *
     * `includeTasks` defaults to false because most callers are filter
     * pickers and project dropdowns that render a name and nothing else. With
     * it on, the response carries every active task of every project — which
     * for a real organisation is the bulk of the payload, fetched to display
     * none of it. The two variants are cached separately, so a screen that
     * does need the tasks (MemberTasks, MemberTimeTracking) asks for them
     * explicitly and does not have to share a cache entry with the pickers.
     */
    getAllProjects: builder.query<Project[], { includeTasks?: boolean } | void>({
      async queryFn(arg, _api, _extraOptions, baseQuery) {
        const includeTasks = (arg || {}).includeTasks === true;
        const page = (pageNumber: number) =>
          `${ENDPOINTS.PROJECTS.GET_ALL}?page=${pageNumber}&limit=100&include_tasks=${includeTasks}`;

        const firstResult = await baseQuery(page(1));
        if (firstResult.error) return { error: firstResult.error };

        const firstResponse = firstResult.data as ProjectListResponse;
        const totalPages = firstResponse.pagination?.total_pages || 1;
        const remainingResults = await Promise.all(
          Array.from({ length: totalPages - 1 }, (_, index) => baseQuery(page(index + 2))),
        );
        const failedResult = remainingResults.find((result) => result.error);
        if (failedResult?.error) return { error: failedResult.error };

        const projects = [
          ...(firstResponse.items || []),
          ...remainingResults.flatMap((result) => ((result.data as ProjectListResponse).items || [])),
        ];

        return { data: projects };
      },
      providesTags: (result) =>
        result
          ? [
              ...result.map(({ id }) => ({ type: 'Project' as const, id })),
              { type: 'Project' as const, id: 'LIST' },
            ]
          : [{ type: 'Project' as const, id: 'LIST' }],
    }),

    getProjectById: builder.query<Project, number>({
      query: (id) => ENDPOINTS.PROJECTS.GET_BY_ID(id),
      providesTags: (_result, _error, id) => [{ type: 'Project', id }],
    }),

    getProjectHoursSummary: builder.query<ProjectHoursSummary[], number[]>({
      query: (projectIds) => {
        const query = new URLSearchParams();
        projectIds.forEach((id) => query.append('project_id', String(id)));
        return `${ENDPOINTS.PROJECTS.HOURS_SUMMARY}?${query.toString()}`;
      },
      transformResponse: (response: { items: ProjectHoursSummary[] }) => response.items,
      providesTags: (result) =>
        result
          ? [...result.map(({ project_id }) => ({ type: 'Project' as const, id: `hours-${project_id}` })), { type: 'Project' as const, id: 'HOURS' }]
          : [{ type: 'Project' as const, id: 'HOURS' }],
    }),

    getAssignableLeaders: builder.query<ProjectUser[], void>({
      query: () => ENDPOINTS.PROJECTS.ASSIGNABLE_LEADERS,
    }),

    /**
     * Members the backend says may own a project (`users.can_own_projects`).
     * The list is the server's, never assembled here — and the same rule
     * validates `owner_id` on save, so this is a convenience, not a gate.
     */
    getAssignableOwners: builder.query<ProjectUser[], void>({
      query: () => ENDPOINTS.PROJECTS.ASSIGNABLE_OWNERS,
    }),

    getAssignableEmployees: builder.query<ProjectUser[], void>({
      query: () => ENDPOINTS.PROJECTS.ASSIGNABLE_EMPLOYEES,
    }),

    createProject: builder.mutation<Project, CreateProjectPayload>({
      query: (body) => ({ url: ENDPOINTS.PROJECTS.CREATE, method: 'POST', body }),
      // Teams screens are derived from projects. Invalidating marks them stale
      // so they reload the next time one is opened - it does not fire a request
      // now, because none of them is mounted.
      invalidatesTags: [{ type: 'Team', id: 'LIST' }],
      async onQueryStarted(_body, { dispatch, getState, queryFulfilled }) {
        try {
          const { data } = await queryFulfilled;
          // The list is ordered newest-first server-side, so a new project
          // genuinely belongs at the top of the first page.
          patchProjectLists({ dispatch, getState }, (items, draft, arg) => {
            if (items.some((project) => project.id === data.id)) return;
            const page = (arg as GetProjectsArgs | undefined)?.page;
            if (page && page > 1) return;
            items.unshift(data);
            if (draft?.pagination) {
              draft.pagination.total = (draft.pagination.total || 0) + 1;
              if (items.length > (draft.pagination.limit || items.length)) items.pop();
            }
          });
        } catch {
          // The caller surfaces the failure; nothing was patched yet.
        }
      },
    }),

    updateProject: builder.mutation<Project, { id: number; body: Partial<CreateProjectPayload> }>({
      query: ({ id, body }) => ({ url: ENDPOINTS.PROJECTS.UPDATE(id), method: 'PATCH', body }),
      invalidatesTags: [{ type: 'Team', id: 'LIST' }],
      async onQueryStarted({ id, body }, { dispatch, getState, queryFulfilled }) {
        // The inline dropdown sends a status id; the matching name and colour
        // are already in the metadata cache, so the badge can change instantly.
        const metadata = projectsApi.endpoints.getProjectMetadata.select(undefined)(getState())?.data;
        const nextStatus =
          body.status_id !== undefined
            ? metadata?.project_statuses?.find((status) => status.id === body.status_id)
            : undefined;
        const assignableLeaders = projectsApi.endpoints.getAssignableLeaders.select(undefined)(getState())?.data;
        const nextLeader =
          body.leader_id !== undefined
            ? assignableLeaders?.find((leader) => leader.id === body.leader_id) ?? null
            : undefined;
        const assignableOwners = projectsApi.endpoints.getAssignableOwners.select(undefined)(getState())?.data;
        const nextOwner =
          body.owner_id !== undefined
            ? assignableOwners?.find((owner) => owner.id === body.owner_id)
            : undefined;

        // Paint the fields we can derive locally on the very next frame.
        const optimistic = patchProjectLists({ dispatch, getState }, (items) => {
          const project = items.find((candidate) => candidate.id === id);
          if (!project) return;
          if (body.project_name !== undefined) project.project_name = body.project_name;
          if (body.description !== undefined) project.description = body.description;
          if (body.deadline !== undefined) project.deadline = body.deadline;
          if (body.billing_type !== undefined) project.billing_type = body.billing_type;
          if (body.category !== undefined) project.category = body.category;
          if (body.fixed_hours !== undefined) {
            project.fixed_hours = body.fixed_hours === null ? null : String(body.fixed_hours);
          }
          if (nextStatus) {
            project.status = { id: nextStatus.id, name: nextStatus.project_status, color: nextStatus.color };
          }
          if (nextLeader !== undefined) project.leader = nextLeader;
          if (nextOwner) project.owner = nextOwner;
          if (body.employee_ids !== undefined) project.employee_count = body.employee_ids.length;
        });

        try {
          const { data } = await queryFulfilled;
          // Status colour, leader, employee_count and task_count are all
          // computed server-side, so swap in the authoritative record.
          patchProjectLists({ dispatch, getState }, (items) => {
            const index = items.findIndex((project) => project.id === id);
            if (index >= 0) items[index] = data;
          });
          dispatch(
            baseApi.util.updateQueryData('getProjectById' as never, id as never, (() => data) as never),
          );
        } catch {
          optimistic.undo();
        }
      },
    }),

    deleteProject: builder.mutation<{ id: number; status: string }, number>({
      query: (id) => ({ url: ENDPOINTS.PROJECTS.DELETE(id), method: 'DELETE' }),
      invalidatesTags: [{ type: 'Team', id: 'LIST' }],
      async onQueryStarted(id, { dispatch, getState, queryFulfilled }) {
        // The backend archives the project, and archived projects are excluded
        // from the list, so dropping the row locally matches the server.
        const optimistic = patchProjectLists({ dispatch, getState }, (items, draft) => {
          const index = items.findIndex((project) => project.id === id);
          if (index < 0) return;
          items.splice(index, 1);
          if (draft?.pagination) draft.pagination.total = Math.max(0, (draft.pagination.total || 1) - 1);
        });

        try {
          await queryFulfilled;
        } catch {
          optimistic.undo();
        }
      },
    }),

    createTask: builder.mutation<
      ProjectTask,
      {
        projectId: number;
        body: {
          project_id?: number;
          name: string;
          assignee_id?: number | null;
          /** Create the task already held by these members (administrators and leaders only). */
          assignee_ids?: number[];
          status_id: number;
          estimated_hours?: number | null;
        };
      }
    >({
      query: ({ projectId, body }) => ({
        url: ENDPOINTS.PROJECTS.TASKS(projectId),
        method: 'POST',
        body: { ...body, project_id: projectId },
      }),
      invalidatesTags: [{ type: 'Team', id: 'LIST' }],
      async onQueryStarted({ projectId }, { dispatch, getState, queryFulfilled }) {
        try {
          const { data } = await queryFulfilled;
          patchProjectLists({ dispatch, getState }, (items) => {
            const project = items.find((candidate) => candidate.id === projectId);
            if (!project) return;
            project.task_count = (project.task_count || 0) + 1;
            // A `null` array means this cache entry was fetched with
            // `include_tasks=false`. Starting one here would turn "you did not
            // ask for the tasks" into "this project has exactly one task".
            if (!Array.isArray(project.tasks)) return;
            if (project.tasks.some((task) => task.id === data.id)) return;
            project.tasks.push(data);
          });
          patchTaskSummaries({ dispatch, getState }, projectId, data);
        } catch {
          // Surfaced by the caller.
        }
      },
    }),

    updateTask: builder.mutation<
      ProjectTask,
      { projectId: number; taskId: number; body: { name?: string; assignee_id?: number | null; status_id?: number; estimated_hours?: number | null } }
    >({
      query: ({ projectId, taskId, body }) => ({
        url: ENDPOINTS.PROJECTS.TASK_BY_ID(projectId, taskId),
        method: 'PATCH',
        body,
      }),
      invalidatesTags: [{ type: 'Team', id: 'LIST' }],
      async onQueryStarted({ projectId, taskId, body }, { dispatch, getState, queryFulfilled }) {
        // Status is the field that changes most often (the inline dropdown) and
        // the name/colour for the chosen status id is already in the metadata cache.
        const metadata = projectsApi.endpoints.getProjectMetadata.select(undefined)(getState())?.data;
        const nextStatus =
          body.status_id !== undefined
            ? metadata?.task_statuses?.find((status) => status.id === body.status_id)
            : undefined;

        const optimistic = patchProjectLists({ dispatch, getState }, (items) => {
          const task = items
            .find((project) => project.id === projectId)
            ?.tasks?.find((candidate) => candidate.id === taskId);
          if (!task) return;
          if (body.name !== undefined) task.name = body.name;
          if (nextStatus) {
            task.status = { id: nextStatus.id, name: nextStatus.task_status, color: nextStatus.color };
          }
        });

        try {
          const { data } = await queryFulfilled;
          patchProjectLists({ dispatch, getState }, (items) => {
            const tasks = items.find((project) => project.id === projectId)?.tasks;
            const index = tasks?.findIndex((task) => task.id === taskId) ?? -1;
            if (tasks && index >= 0) tasks[index] = data;
          });
        } catch {
          optimistic.undo();
        }
      },
    }),

    /**
     * Makes `userIds` the complete set of members holding a task (and, when
     * `statusId` is given, sets its status in the same request).
     *
     * The response is the whole task, so it is written straight into every
     * cached project list — and the Assign Tasks screen, which reads one of
     * them, shows the result the moment the server confirms it. Deliberately no
     * refetch afterwards: see `AdminTaskListing.handleCreateTask` for why a
     * refetch right after a cache patch hides the patch.
     */
    setTaskAssignees: builder.mutation<
      ProjectTask,
      { projectId: number; taskId: number; body: { user_ids: number[]; status_id?: number } }
    >({
      query: ({ projectId, taskId, body }) => ({
        url: ENDPOINTS.PROJECTS.TASK_ASSIGNEES(projectId, taskId),
        method: 'PUT',
        body,
      }),
      // Teams screens are derived from projects and their tasks' assignees.
      invalidatesTags: [{ type: 'Team', id: 'LIST' }],
      async onQueryStarted({ projectId, taskId }, { dispatch, getState, queryFulfilled }) {
        try {
          const { data } = await queryFulfilled;
          patchProjectLists({ dispatch, getState }, (items) => {
            const tasks = items.find((project) => project.id === projectId)?.tasks;
            const index = tasks?.findIndex((task) => task.id === taskId) ?? -1;
            if (tasks && index >= 0) tasks[index] = data;
          });
        } catch {
          // The caller surfaces the failure; nothing was patched.
        }
      },
    }),
  }),
});

export const {
  useGetProjectMetadataQuery,
  useGetProjectsQuery,
  useLazyGetProjectsQuery,
  useGetProjectHoursSummaryQuery,
  useGetAllProjectsQuery,
  useGetProjectByIdQuery,
  useGetAssignableLeadersQuery,
  useGetAssignableOwnersQuery,
  useGetAssignableEmployeesQuery,
  useCreateProjectMutation,
  useUpdateProjectMutation,
  useDeleteProjectMutation,
  useCreateTaskMutation,
  useUpdateTaskMutation,
  useSetTaskAssigneesMutation,
} = projectsApi;
