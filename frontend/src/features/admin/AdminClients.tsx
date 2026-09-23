import React, { useState } from 'react';
import { V2Shell } from '../dashboard/v2/V2Shell';
import { Card, EmptyState, ErrorNote, Spinner } from '../member/MemberUi';
import { useGetAllProjectsQuery } from '../../store/api/projectsApi';
import {
  DEFAULT_CLIENT_PERMISSIONS,
  useCreateClientInvitationMutation,
  useDeactivateClientMutation,
  useGetClientsQuery,
  useResendClientInvitationMutation,
  useUpdateClientAccessMutation,
  type ClientListItem,
  type ClientPermissions,
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

/** The project checkbox list, shared by the Add Client and Edit Access modals. */
const ProjectChecklist: React.FC<{
  selectedIds: Set<number>;
  onToggle: (id: number) => void;
}> = ({ selectedIds, onToggle }) => {
  const { data: projects, isLoading } = useGetAllProjectsQuery();

  if (isLoading) return <Spinner label="Loading projects…" />;

  return (
    <div className="max-h-56 overflow-y-auto rounded-lg border border-[#E2E8F0] divide-y divide-[#F1F5F9]">
      {(projects ?? []).map((project) => (
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
      {(projects ?? []).length === 0 && (
        <p className="px-3 py-4 text-sm text-[#94A3B8]">No projects available yet.</p>
      )}
    </div>
  );
};

const PERMISSION_LABELS: { key: keyof ClientPermissions; label: string; hint: string }[] = [
  { key: 'share_member_details', label: 'Member Details', hint: 'Employee/member information for the shared projects.' },
  { key: 'share_screenshots', label: 'Screenshots', hint: "Screenshots captured while working on the shared project(s)." },
  { key: 'share_tasks', label: 'Tasks', hint: 'Project task details and task activity.' },
  { key: 'share_timing', label: 'Timing', hint: 'Member/project working hours and time-tracking details.' },
];

/** The four sharing-permission toggles, shared by the Add Client and Edit
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
  return { selectedIds, toggle };
};

const AddClientModal: React.FC<{ onClose: () => void }> = ({ onClose }) => {
  const [createInvitation, { isLoading: isSending }] = useCreateClientInvitationMutation();
  const { selectedIds, toggle } = useToggleSet([]);
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
            <ProjectChecklist selectedIds={selectedIds} onToggle={toggle} />
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
  const { selectedIds, toggle } = useToggleSet(client.projects.map((p) => p.id));
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
            <ProjectChecklist selectedIds={selectedIds} onToggle={toggle} />
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
                      {client.projects.length ? client.projects.map((p) => p.project_name).join(', ') : '—'}
                    </td>
                    <td className="px-4 py-3"><StatusBadge status={client.status} /></td>
                    <td className="px-4 py-3 text-[#475569]">
                      {new Date(client.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-3">
                        <button
                          onClick={() => setEditingClient(client)}
                          className="text-[#475569] font-semibold hover:text-[#0F172A]"
                        >
                          Edit Access
                        </button>
                        {client.status === 'active' ? (
                          <button
                            disabled={isDeactivating}
                            onClick={() => deactivateClient(client.id)}
                            className="text-rose-600 font-semibold hover:text-rose-700 disabled:opacity-50"
                          >
                            Deactivate
                          </button>
                        ) : (
                          <button
                            disabled={isResending}
                            onClick={() => resendInvitation(client.id)}
                            className="text-[#2563EB] font-semibold hover:text-blue-700 disabled:opacity-50"
                          >
                            Resend Invitation
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
