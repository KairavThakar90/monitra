import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { MemberShell } from "./MemberShell";
import { Avatar, Card, EmptyState, ErrorNote, ProgressBar, Spinner, StatusPill } from "./MemberUi";
import { useGetProjectMetadataQuery, useGetProjectsQuery } from "../../store/api/projectsApi";
import type { Project } from "../../store/api/projectsApi";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { InlineRefreshIndicator } from "../../components/InlineRefreshIndicator";
import { PaginationArrow } from "../../components/PaginationArrow";
import { series } from "../dashboard/v2/theme";
import { formatISTDate } from "../../utils/duration";
import { billingTypeLabel } from "../../utils/billing";
import { FieldError, SEARCH_MAX_LENGTH, validateSearchTerm } from "../../validation";

/**
 * The member's project management view.
 *
 * `/projects` already answers with only the projects the caller is a member
 * of, so this page sends no user id and shows nothing it had to filter out
 * client-side. It is read-only by design: a member holds `projects:view` and
 * nothing more, so offering an edit control here would only produce a 403.
 * Changing what they *can* change — the status of their own tasks — lives on
 * My Tasks.
 */

const PAGE_SIZE = 9;

const deadlineNote = (deadline: string | null) => {
  if (!deadline) return { label: "No deadline", tone: "text-[#94A3B8]" };
  const due = new Date(`${deadline.slice(0, 10)}T00:00:00`);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((due.getTime() - today.getTime()) / 86_400_000);
  if (days < 0) return { label: `Overdue by ${Math.abs(days)}d`, tone: "text-rose-600" };
  if (days === 0) return { label: "Due today", tone: "text-amber-600" };
  if (days <= 7) return { label: `Due in ${days}d`, tone: "text-amber-600" };
  return { label: `Due ${formatISTDate(deadline)}`, tone: "text-[#64748B]" };
};

const ProjectCard: React.FC<{ project: Project; onOpenTeam: () => void }> = ({ project, onOpenTeam }) => {
  const tasks = project.tasks || [];
  const done = tasks.filter((task) => /done|complete/i.test(task.status?.name || "")).length;
  const percentage = tasks.length ? Math.round((done / tasks.length) * 100) : 0;
  const due = deadlineNote(project.deadline);

  return (
    <div className="flex flex-col rounded-xl border border-[#E2E8F0] bg-white p-5 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md">
      <div className="flex items-start justify-between gap-3">
        <h3 className="min-w-0 flex-1 truncate text-[15px] font-bold text-[#0F172A]" title={project.project_name}>
          {project.project_name}
        </h3>
        <StatusPill status={project.status} />
      </div>

      <p className="mt-2 line-clamp-2 min-h-[2.5rem] text-[13px] leading-5 text-[#64748B]">
        {project.description || "No description."}
      </p>

      <div className="mt-4">
        <div className="mb-1.5 flex items-center justify-between text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">
          <span>Task progress</span>
          <span className="text-[#0F172A]">
            {done}/{tasks.length}
          </span>
        </div>
        <ProgressBar value={percentage} color={series[0]} />
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-3 text-[13px]">
        <div>
          <dt className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">Leader</dt>
          <dd className="mt-1 truncate font-semibold text-[#0F172A]">{project.leader?.name || "Unassigned"}</dd>
        </div>
        <div>
          <dt className="text-[11px] font-bold uppercase tracking-wider text-[#94A3B8]">Billing</dt>
          <dd className="mt-1 truncate font-semibold capitalize text-[#0F172A]">
            {billingTypeLabel(project.billing_type)}
            {project.fixed_hours ? ` · ${project.fixed_hours}h` : ""}
          </dd>
        </div>
      </dl>

      <div className="mt-4 flex items-center justify-between gap-3 border-t border-[#F1F5F9] pt-4">
        <div className="flex items-center">
          {(project.employees || []).slice(0, 4).map((employee, index) => (
            <div key={employee.id} className={index === 0 ? "" : "-ml-2"}>
              <Avatar name={employee.name} size={28} color={series[index % series.length]} ring />
            </div>
          ))}
          {(project.employees || []).length > 4 && (
            <span className="-ml-2 flex h-7 w-7 items-center justify-center rounded-full bg-[#F1F5F9] text-[10px] font-bold text-[#64748B] ring-2 ring-white">
              +{project.employees.length - 4}
            </span>
          )}
          {(project.employees || []).length === 0 && (
            <span className="text-xs text-[#94A3B8]">No members listed</span>
          )}
        </div>
        <span className={`shrink-0 text-[11px] font-bold ${due.tone}`}>{due.label}</span>
      </div>

      <button
        onClick={onOpenTeam}
        className="mt-4 w-full rounded-lg border border-[#E2E8F0] px-3 py-2 text-[12px] font-bold text-[#2563EB] transition hover:bg-[#F8FAFC]"
      >
        View team
      </button>
    </div>
  );
};

export const MemberProjects: React.FC = () => {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [searchError, setSearchError] = useState<string | null>(null);
  const [statusId, setStatusId] = useState<number | null>(null);
  const debouncedSearch = useDebouncedValue(search);

  // A rejected term is held back from the query rather than sent: the box says
  // why, and the last good result set stays on screen.
  const searchCheck = validateSearchTerm(debouncedSearch, { fieldLabel: "Search" });
  const searchTerm = searchCheck.ok ? searchCheck.value : "";

  const { data: metadata } = useGetProjectMetadataQuery();
  const { data, isLoading, isFetching, isError } = useGetProjectsQuery({
    page,
    limit: PAGE_SIZE,
    search: searchTerm || undefined,
    status_id: statusId,
  });

  const projects = data?.items ?? [];
  const pagination = data?.pagination;
  const totalPages = pagination?.total_pages ?? 0;

  const applyStatus = (id: number | null) => {
    setStatusId(id);
    setPage(1);
  };

  return (
    <MemberShell
      title="My Projects"
      subtitle="Every project you are assigned to, with its team and task progress."
      actions={<InlineRefreshIndicator active={isFetching && !isLoading} />}
    >
      <div className="w-full space-y-6 pb-20">
        <Card>
          <div className="flex flex-wrap items-center gap-3">
            <div className="relative min-w-[220px] flex-1">
              <svg
                className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[#94A3B8]"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
              <input
                value={search}
                onChange={(event) => {
                  const next = event.target.value;
                  setSearch(next);
                  const result = validateSearchTerm(next, { fieldLabel: "Search" });
                  setSearchError(result.ok ? null : result.error);
                  setPage(1);
                }}
                placeholder="Search my projects…"
                maxLength={SEARCH_MAX_LENGTH}
                aria-invalid={searchError ? true : undefined}
                aria-describedby={searchError ? "project-search-error" : undefined}
                className="w-full rounded-lg border border-[#E2E8F0] py-2.5 pl-9 pr-3 text-[13px] font-medium text-[#0F172A] outline-none transition focus:border-[#2563EB]"
              />
              <FieldError id="project-search-error" message={searchError} />
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <button
                onClick={() => applyStatus(null)}
                className={
                  "rounded-lg px-3 py-2 text-[12px] font-bold transition " +
                  (statusId === null ? "bg-[#2563EB] text-white" : "text-[#64748B] hover:bg-[#F8FAFC]")
                }
              >
                All
              </button>
              {(metadata?.project_statuses ?? []).map((status) => (
                <button
                  key={status.id}
                  onClick={() => applyStatus(status.id)}
                  className={
                    "rounded-lg px-3 py-2 text-[12px] font-bold transition " +
                    (statusId === status.id ? "bg-[#2563EB] text-white" : "text-[#64748B] hover:bg-[#F8FAFC]")
                  }
                >
                  {status.project_status}
                </button>
              ))}
            </div>
          </div>
        </Card>

        {isError && <ErrorNote message="Your projects could not be loaded. Please try again." />}

        {isLoading ? (
          <Spinner label="Loading your projects…" />
        ) : projects.length === 0 ? (
          <Card>
            <EmptyState
              message={
                debouncedSearch || statusId
                  ? "No projects match these filters."
                  : "You are not assigned to any projects yet."
              }
              hint="A project appears here once a leader adds you to it."
            />
          </Card>
        ) : (
          <div
            className={`grid grid-cols-1 gap-5 transition-opacity md:grid-cols-2 xl:grid-cols-3 ${
              isFetching ? "opacity-60" : ""
            }`}
          >
            {projects.map((project) => (
              <ProjectCard
                key={project.id}
                project={project}
                onOpenTeam={() => navigate(`/member/team/${project.id}`)}
              />
            ))}
          </div>
        )}

        {totalPages > 1 && (
          <div className="flex items-center justify-between rounded-xl border border-[#E2E8F0] bg-white px-4 py-3 shadow-sm">
            <span className="text-[12px] font-semibold text-[#64748B]">
              Page {pagination?.page} of {totalPages} · {pagination?.total} projects
            </span>
            <div className="flex items-center gap-2">
              <PaginationArrow
                direction="prev"
                disabled={page <= 1}
                onClick={() => setPage((current) => Math.max(1, current - 1))}
              />
              <PaginationArrow
                direction="next"
                disabled={page >= totalPages}
                onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
              />
            </div>
          </div>
        )}
      </div>
    </MemberShell>
  );
};
