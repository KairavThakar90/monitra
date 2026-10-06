import React, { useEffect, useRef, useState } from 'react';
import { V2Shell } from '../dashboard/v2/V2Shell';
import { Card, EmptyState, ErrorNote, Spinner } from '../member/MemberUi';
import { useGetAllProjectsQuery } from '../../store/api/projectsApi';
import { SEARCH_MAX_LENGTH } from '../../validation';
import {
  DEFAULT_CLIENT_PERMISSIONS,
  useCreateClientInvitationMutation,
  useDeactivateClientMutation,
  useGetClientsQuery,
  useResendClientInvitationMutation,
  useUpdateClientAccessMutation,
  type ClientListItem,
  type ClientPermissions,
  type ClientProjectRef,
} from '../../store/api/clientsApi';

const STATUS_STYLES: Record<ClientListItem['status'], string> = {
  pending: 'bg-amber-50 text-amber-700 border border-amber-200',
  active: 'bg-emerald-50 text-emerald-700 border border-emerald-200',
  rejected: 'bg-rose-50 text-rose-700 border border-rose-200',
  deactivated: 'bg-slate-100 text-slate-600 border border-slate-200',
};

const StatusBadge: React.FC<{ status: ClientListItem['status'] }> = ({ status }) => (
  <span className={`inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-bold capitalize ${STATUS_STYLES[status]}`}>
    {status}
  </span>
);

/** How many project names a client's row shows before "See all". */
const PROJECT_PREVIEW_COUNT = 3;

/**
 * One client's projects, as chips.
 *
 * A client can be given hundreds of projects, and the column used to join every
 * name into one comma-separated paragraph, which made that row taller than the
 * screen. Now a row shows the first few as chips and a "See all (N)" button;
 * pressing it shows every project -- in a scrollable box, so even then the row
 * stays a sensible height -- and the button becomes "Show less". A client with
 * only a few projects shows them all and has no button at all.
 *
 * The open/closed state is the row's own, so expanding one client leaves the
 * others as they were. Names are cut with an ellipsis when they are long and
 * carry the full name as a tooltip.
 */
const ClientProjectsCell: React.FC<{ clientId: number; projects: ClientProjectRef[] }> = ({ clientId, projects }) => {
  const [expanded, setExpanded] = useState(false);

  if (projects.length === 0) return <span className="text-[#94A3B8]">—</span>;

  const collapsible = projects.length > PROJECT_PREVIEW_COUNT;
  const shown = collapsible && !expanded ? projects.slice(0, PROJECT_PREVIEW_COUNT) : projects;
  const listId = `client-projects-${clientId}`;

  return (
    <div className="min-w-[240px] max-w-[560px]">
      <ul
        id={listId}
        aria-label="Projects"
        className={
          'flex flex-wrap gap-1.5 ' +
          (expanded ? 'max-h-56 overflow-y-auto rounded-lg border border-[#E2E8F0] bg-[#F8FAFC] p-2' : '')
        }
      >
        {shown.map((project) => (
          <li
            key={project.id}
            title={project.project_name}
            className="max-w-[240px] truncate rounded-md border border-[#E2E8F0] bg-white px-2 py-1 text-xs font-semibold text-[#334155]"
          >
            {project.project_name}
          </li>
        ))}
      </ul>
      {collapsible && (
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={() => setExpanded((open) => !open)}
          className="mt-2 text-[12px] font-bold text-[#2563EB] transition hover:text-blue-800 hover:underline"
        >
          {expanded ? 'Show less' : `See all (${projects.length})`}
        </button>
      )}
    </div>
  );
};

/**
 * The project checkbox list, shared by the Add Client and Edit Access modals.
 *
 * A search box narrows the list by project name, "Select all" ticks (or
 * unticks) every project the search currently shows, and the count beside it
 * is how many projects are selected in total -- including any the search is
 * hiding, since those are still part of what gets saved.
 */
const ProjectChecklist: React.FC<{
  selectedIds: Set<number>;
  onToggle: (id: number) => void;
  onSetMany: (ids: number[], selected: boolean) => void;
}> = ({ selectedIds, onToggle, onSetMany }) => {
  const { data: projects, isLoading } = useGetAllProjectsQuery();
  const [search, setSearch] = useState('');
  const selectAllRef = useRef<HTMLInputElement>(null);

  const allProjects = projects ?? [];
  const term = search.trim().toLowerCase();
  const visibleProjects = term
    ? allProjects.filter((project) => project.project_name.toLowerCase().includes(term))
    : allProjects;
  const selectedCount = allProjects.filter((project) => selectedIds.has(project.id)).length;
  const visibleSelectedCount = visibleProjects.filter((project) => selectedIds.has(project.id)).length;
  const allVisibleSelected = visibleProjects.length > 0 && visibleSelectedCount === visibleProjects.length;
  const someVisibleSelected = visibleSelectedCount > 0 && !allVisibleSelected;

  // `indeterminate` is a DOM property with no React attribute.
  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = someVisibleSelected;
  }, [someVisibleSelected]);

  if (isLoading) return <Spinner label="Loading projects…" />;

  return (
    <div className="space-y-2">
      <input
        type="text"
        value={search}
        maxLength={SEARCH_MAX_LENGTH}
        onChange={(e) => setSearch(e.target.value)}
        // Enter in the search box must not submit the surrounding form.
        onKeyDown={(e) => { if (e.key === 'Enter') e.preventDefault(); }}
        placeholder="Search projects…"
        aria-label="Search projects"
        className="w-full rounded-lg border border-[#E2E8F0] px-3 py-2 text-sm text-[#0F172A] shadow-sm outline-none focus:border-[#2563EB] focus:ring-2 focus:ring-[#2563EB]/15"
      />

      <div className="rounded-lg border border-[#E2E8F0]">
        <div className="flex items-center justify-between gap-3 border-b border-[#E2E8F0] bg-[#F8FAFC] px-3 py-2">
          <label className="flex items-center gap-3 text-sm font-semibold text-[#0F172A] cursor-pointer">
            <input
              ref={selectAllRef}
              type="checkbox"
              checked={allVisibleSelected}
              disabled={visibleProjects.length === 0}
              onChange={() => onSetMany(visibleProjects.map((project) => project.id), !allVisibleSelected)}
              className="h-4 w-4 rounded border-[#CBD5E1] text-[#2563EB] focus:ring-[#2563EB]"
            />
            {term ? 'Select all results' : 'Select all'}
          </label>
          <span className="text-xs font-semibold text-[#64748B]" aria-live="polite">
            {selectedCount} of {allProjects.length} selected
          </span>
        </div>

        <div className="max-h-56 overflow-y-auto divide-y divide-[#F1F5F9]">
          {visibleProjects.map((project) => (
            <label
              key={project.id}
              className="flex items-center gap-3 px-3 py-2.5 text-sm text-[#0F172A] cursor-pointer hover:bg-[#F8FAFC]"
            >
              <input
                type="checkbox"
                checked={selectedIds.has(project.id)}
                onChange={() => onToggle(project.id)}
                className="h-4 w-4 rounded border-[#CBD5E1] text-[#2563EB] focus:ring-[#2563EB]"
              />
              {project.project_name}
            </label>
          ))}
          {allProjects.length === 0 && (
            <p className="px-3 py-4 text-sm text-[#94A3B8]">No projects available yet.</p>
          )}
          {allProjects.length > 0 && visibleProjects.length === 0 && (
            <p className="px-3 py-4 text-sm text-[#94A3B8]">No projects match your search.</p>
          )}
        </div>
      </div>
    </div>
  );
};

const PERMISSION_LABELS: { key: keyof ClientPermissions; label: string; hint: string }[] = [
  { key: 'share_member_details', label: 'Member Details', hint: 'Employee/member information for the shared projects.' },
  { key: 'share_screenshots', label: 'Screenshots', hint: "Screenshots captured while working on the shared project(s)." },
  { key: 'share_tasks', label: 'Tasks', hint: 'Project task details and task activity.' },
  { key: 'share_timing', label: 'Timing', hint: 'Member/project working hours and time-tracking details.' },
  { key: 'share_billing', label: 'Billing Details', hint: 'Budgeted, used and remaining hours for billable projects, broken down by task.' },
];

/** The sharing-permission toggles, shared by the Add Client and Edit
 * Access modals. */
const PermissionsChecklist: React.FC<{
  permissions: ClientPermissions;
  onChange: (permissions: ClientPermissions) => void;
}> = ({ permissions, onChange }) => (
  <div className="rounded-lg border border-[#E2E8F0] divide-y divide-[#F1F5F9]">
    {PERMISSION_LABELS.map(({ key, label, hint }) => (
      <label
        key={key}
        className="flex items-start gap-3 px-3 py-2.5 text-sm text-[#0F172A] cursor-pointer hover:bg-[#F8FAFC]"
      >
        <input
          type="checkbox"
          checked={permissions[key]}
          onChange={() => onChange({ ...permissions, [key]: !permissions[key] })}
          className="mt-0.5 h-4 w-4 rounded border-[#CBD5E1] text-[#2563EB] focus:ring-[#2563EB]"
        />
        <span>
          <span className="block font-semibold">{label}</span>
          <span className="block text-xs text-[#94A3B8]">{hint}</span>
        </span>
      </label>
    ))}
  </div>
);

const useToggleSet = (initial: number[]) => {
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set(initial));
  const toggle = (id: number) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  /** Selects (or deselects) every id in `ids`, leaving the rest of the set alone. */
  const setMany = (ids: number[], selected: boolean) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      for (const id of ids) {
        if (selected) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  };
  return { selectedIds, toggle, setMany };
};

const AddClientModal: React.FC<{ onClose: () => void }> = ({ onClose }) => {
  const [createInvitation, { isLoading: isSending }] = useCreateClientInvitationMutation();
  const { selectedIds, toggle, setMany } = useToggleSet([]);
  const [permissions, setPermissions] = useState<ClientPermissions>(DEFAULT_CLIENT_PERMISSIONS);

  const [email, setEmail] = useState('');
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!email.trim()) {
      setError('Enter the client’s email address.');
      return;
    }
    if (selectedIds.size === 0) {
      setError('Select at least one project to share.');
      return;
    }
    try {
      await createInvitation({ email: email.trim(), project_ids: Array.from(selectedIds), permissions }).unwrap();
      onClose();
    } catch (err: any) {
      setError(err?.data?.detail || 'Could not send the invitation. Please try again.');
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-[#0f172a]/50 backdrop-blur-sm">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md overflow-hidden border border-[#E2E8F0] max-h-[90vh] flex flex-col">
        <div className="px-6 py-4 border-b border-[#F1F5F9] flex justify-between items-center shrink-0">
          <h3 className="text-[15px] font-bold text-[#0F172A]">Add Client</h3>
          <button type="button" onClick={onClose} className="text-[#94A3B8] hover:text-[#64748B] focus:outline-none">
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-5 overflow-y-auto">
          {error && (
            <div className="p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-600">{error}</div>
          )}

          <div>
            <label className="block text-xs font-semibold text-[#94A3B8] tracking-wider uppercase mb-1">
              Client Email Address
            </label>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="client@example.com"
              className="w-full rounded-lg border border-[#E2E8F0] px-3 py-2 text-sm text-[#0F172A] shadow-sm outline-none focus:border-[#2563EB] focus:ring-2 focus:ring-[#2563EB]/15"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-[#94A3B8] tracking-wider uppercase mb-2">
              Select Projects
            </label>
            <ProjectChecklist selectedIds={selectedIds} onToggle={toggle} onSetMany={setMany} />
          </div>

          <div>
            <label className="block text-xs font-semibold text-[#94A3B8] tracking-wider uppercase mb-2">
              Permissions
            </label>
            <PermissionsChecklist permissions={permissions} onChange={setPermissions} />
          </div>

          <div className="pt-2 flex justify-end gap-3">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg px-4 py-2 text-sm font-medium text-[#64748B] hover:bg-[#F1F5F9]"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSending}
              className="rounded-lg bg-[#2563EB] px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {isSending ? 'Sending…' : 'Send Invitation'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};

const EditAccessModal: React.FC<{ client: ClientListItem; onClose: () => void }> = ({ client, onClose }) => {
  const [updateAccess, { isLoading: isSaving }] = useUpdateClientAccessMutation();
  const { selectedIds, toggle, setMany } = useToggleSet(client.projects.map((p) => p.id));
  const [permissions, setPermissions] = useState<ClientPermissions>(client.permissions);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (selectedIds.size === 0) {
      setError('Select at least one project to share.');
      return;
    }
    try {
      await updateAccess({ id: client.id, project_ids: Array.from(selectedIds), permissions }).unwrap();
      onClose();
    } catch (err: any) {
      setError(err?.data?.detail || 'Could not update access. Please try again.');
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-[#0f172a]/50 backdrop-blur-sm">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md overflow-hidden border border-[#E2E8F0] max-h-[90vh] flex flex-col">
        <div className="px-6 py-4 border-b border-[#F1F5F9] flex justify-between items-center shrink-0">
          <h3 className="text-[15px] font-bold text-[#0F172A]">Edit Access — {client.name}</h3>
          <button type="button" onClick={onClose} className="text-[#94A3B8] hover:text-[#64748B] focus:outline-none">
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-5 overflow-y-auto">
          {error && (
            <div className="p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-600">{error}</div>
          )}

          <div>
            <label className="block text-xs font-semibold text-[#94A3B8] tracking-wider uppercase mb-2">
              Shared Projects
            </label>
            <ProjectChecklist selectedIds={selectedIds} onToggle={toggle} onSetMany={setMany} />
          </div>

          <div>
            <label className="block text-xs font-semibold text-[#94A3B8] tracking-wider uppercase mb-2">
              Permissions
            </label>
            <PermissionsChecklist permissions={permissions} onChange={setPermissions} />
          </div>

          <div className="pt-2 flex justify-end gap-3">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg px-4 py-2 text-sm font-medium text-[#64748B] hover:bg-[#F1F5F9]"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSaving}
              className="rounded-lg bg-[#2563EB] px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {isSaving ? 'Saving…' : 'Save Changes'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};

export const AdminClients: React.FC = () => {
  const [page, setPage] = useState(1);
  const { data, isLoading, isFetching, isError } = useGetClientsQuery({ page, limit: 20 });
  const [resendInvitation, { isLoading: isResending }] = useResendClientInvitationMutation();
  const [deactivateClient, { isLoading: isDeactivating }] = useDeactivateClientMutation();
  const [modalOpen, setModalOpen] = useState(false);
  const [editingClient, setEditingClient] = useState<ClientListItem | null>(null);

  const items = data?.items ?? [];
  const pagination = data?.pagination;

  return (
    <V2Shell
      title="Clients"
      subtitle="Invite external clients and control which projects — and what — they can view."
      actions={
        <button
          onClick={() => setModalOpen(true)}
          className="bg-[#2563EB] text-white px-4 py-2 rounded-lg shadow-sm text-sm font-medium transition hover:bg-blue-700"
        >
          + Add Client
        </button>
      }
    >
      <div className="w-full space-y-4 pb-20">
        {isError && <ErrorNote message="Clients could not be loaded. Please try again." />}

        {isLoading ? (
          <Spinner label="Loading clients…" />
        ) : items.length === 0 ? (
          <Card>
            <EmptyState
              message="No clients invited yet."
              hint="Click “Add Client” to invite one and choose which projects they can see."
            />
          </Card>
        ) : (
          <div className={`overflow-hidden rounded-xl border border-[#E2E8F0] bg-white shadow-sm transition-opacity ${isFetching ? 'opacity-60' : ''}`}>
            <table className="w-full text-left text-sm">
              <thead className="bg-[#F8FAFC] text-[11px] font-bold uppercase tracking-wider text-[#64748B]">
                <tr>
                  <th className="px-4 py-3">Client Name</th>
                  <th className="px-4 py-3">Email</th>
                  <th className="px-4 py-3">Projects</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Created Date</th>
                  <th className="px-4 py-3">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#F1F5F9]">
                {items.map((client) => (
                  <tr key={client.id}>
                    <td className="px-4 py-3 font-medium text-[#0F172A]">{client.name}</td>
                    <td className="px-4 py-3 text-[#475569]">{client.email}</td>
                    <td className="px-4 py-3 text-[#475569]">
                      <ClientProjectsCell clientId={client.id} projects={client.projects} />
                    </td>
                    <td className="px-4 py-3"><StatusBadge status={client.status} /></td>
                    <td className="px-4 py-3 text-[#475569]">
                      {new Date(client.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-3">
                      {/* The same bordered-pill actions the Members table uses. */}
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => setEditingClient(client)}
                          className="rounded px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-[#14B8A6] border border-[#14B8A6]/30 transition hover:bg-[#14B8A6]/10"
                        >
                          Edit 
                        </button>
                        {client.status === 'active' ? (
                          <button
                            disabled={isDeactivating}
                            onClick={() => deactivateClient(client.id)}
                            className="rounded px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-rose-500 border border-rose-200 transition hover:bg-rose-50 disabled:opacity-50"
                          >
                            Deactivate
                          </button>
                        ) : (
                          <button
                            disabled={isResending}
                            onClick={() => resendInvitation(client.id)}
                            className="rounded px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-[#2563EB] border border-[#2563EB]/30 transition hover:bg-[#2563EB]/10 disabled:opacity-50"
                          >
                            Resend 
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {pagination && pagination.total_pages > 1 && (
          <div className="flex items-center justify-between rounded-xl border border-[#E2E8F0] bg-white px-4 py-3 shadow-sm">
            <span className="text-[12px] font-semibold text-[#64748B]">
              Page {pagination.page} of {pagination.total_pages}
            </span>
            <div className="flex items-center gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage((current) => Math.max(1, current - 1))}
                className="rounded-md border border-[#E2E8F0] px-3 py-1.5 text-xs font-semibold text-[#475569] disabled:opacity-40"
              >
                Prev
              </button>
              <button
                disabled={page >= pagination.total_pages}
                onClick={() => setPage((current) => Math.min(pagination.total_pages, current + 1))}
                className="rounded-md border border-[#E2E8F0] px-3 py-1.5 text-xs font-semibold text-[#475569] disabled:opacity-40"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>

      {modalOpen && <AddClientModal onClose={() => setModalOpen(false)} />}
      {editingClient && (
        <EditAccessModal client={editingClient} onClose={() => setEditingClient(null)} />
      )}
    </V2Shell>
  );
};
