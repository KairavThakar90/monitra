import React, { useState, useMemo } from 'react';
import { V2Shell } from './src/features/dashboard/v2/V2Shell';
import { 
  useGetMembersQuery, 
  useCreateMemberMutation, 
  useUpdateMemberMutation, 
  useDeleteMemberMutation,
} from './src/store/api/membersApi';
import type { Member } from './src/store/api/membersApi';

const GRADIENT_CYAN_PURPLE = 'bg-gradient-to-r from-[#0ea5e9] via-[#3b82f6] to-[#8b5cf6]';

const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  if ((status || '').toLowerCase() === 'active') {
    return <span className="inline-flex items-center rounded-md bg-emerald-50 px-2.5 py-1 text-[11px] font-bold tracking-wider text-emerald-600 border border-emerald-200">Active</span>;
  }
  return <span className="inline-flex items-center rounded-md bg-slate-50 px-2.5 py-1 text-[11px] font-bold tracking-wider text-slate-500 border border-slate-200">Inactive</span>;
};

const formatDate = (dateStr: string | null) => {
  if (!dateStr) return '-';
  const parts = dateStr.split('-');
  if (parts.length !== 3) return dateStr;
  const date = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]));
  const day = String(date.getDate()).padStart(2, '0');
  const month = date.toLocaleString('en-US', { month: 'short' });
  const year = date.getFullYear();
  return `${day} ${month} ${year}`;
};

export const AdminMembers: React.FC = () => {
  const [search, setSearch] = useState('');
  const [filterRole, setFilterRole] = useState('All');
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 20; // 20 based on user's API instructions

  const { data, isLoading, isError } = useGetMembersQuery({ 
    page, 
    limit: PAGE_SIZE, 
    role: filterRole, 
    status: 'All' 
  });
  
  const [createMember] = useCreateMemberMutation();
  const [updateMember] = useUpdateMemberMutation();
  const [deleteMember] = useDeleteMemberMutation();

  // Drawer state
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [drawerMode, setDrawerMode] = useState<'create' | 'edit'>('create');
  const [editingId, setEditingId] = useState<number | null>(null);

  // Form state
  const [formName, setFormName] = useState('');
  const [formEmail, setFormEmail] = useState('');
  const [formRole, setFormRole] = useState('employee');
  const [formStatus, setFormStatus] = useState('active');
  const [formDOJ, setFormDOJ] = useState('');
  const [formDOB, setFormDOB] = useState('');
  const [formDesignation, setFormDesignation] = useState('');

  const openCreateDrawer = () => {
    setDrawerMode('create');
    setEditingId(null);
    setFormName('');
    setFormEmail('');
    setFormRole('employee');
    setFormStatus('active');
    setFormDOJ('');
    setFormDOB('');
    setFormDesignation('');
    setIsDrawerOpen(true);
  };

  const openEditDrawer = (member: Member) => {
    setDrawerMode('edit');
    setEditingId(member.id);
    setFormName(member.name);
    setFormEmail(member.email);
    setFormRole(member.role);
    setFormStatus(member.status);
    setFormDOJ(member.date_of_joining || '');
    setFormDOB(member.date_of_birth || '');
    setFormDesignation(member.designation || '');
    setIsDrawerOpen(true);
  };

  const handleSaveMember = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formName || !formEmail) return;

    const payload = {
      name: formName,
      email: formEmail,
      role: formRole,
      status: formStatus,
      date_of_joining: formDOJ || null,
      date_of_birth: formDOB || null,
      designation: formDesignation,
    };

    try {
      if (drawerMode === 'create') {
        await createMember(payload).unwrap();
      } else if (drawerMode === 'edit' && editingId) {
        await updateMember({ id: editingId, body: payload }).unwrap();
      }
      setIsDrawerOpen(false);
    } catch (err) {
      console.error('Failed to save member', err);
    }
  };

  const handleDeleteMember = async (id: number) => {
    if (confirm("Are you sure you want to delete this member?")) {
      try {
        await deleteMember(id).unwrap();
      } catch (err) {
        console.error('Failed to delete member', err);
      }
    }
  };

  const filteredItems = useMemo(() => {
    if (!data?.items) return [];
    if (!search) return data.items;
    return data.items.filter(m => 
      (m.name || '').toLowerCase().includes(search.toLowerCase()) || 
      (m.email || '').toLowerCase().includes(search.toLowerCase())
    );
  }, [data?.items, search]);

  const totalPages = data?.pages || 1;

  return (
    <V2Shell
      title="Members Directory"
      subtitle="Manage employees, their roles, and company details."
      actions={
        <div className="flex gap-2">
          <button
            onClick={openCreateDrawer}
            className={`rounded-lg px-4 py-2 text-sm font-bold text-white shadow-md transition hover:opacity-90 ${GRADIENT_CYAN_PURPLE}`}
          >
            + Add Member
          </button>
        </div>
      }
    >
      <div className="mx-auto max-w-7xl space-y-6 pb-20">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex flex-1 items-center gap-2 px-2">
            <svg className="h-5 w-5 text-slate-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input
              type="text"
              placeholder="Search members by name or email..."
              value={search}
              onChange={(e) => { setSearch(e.target.value); }}
              className="flex-1 bg-transparent text-sm outline-none placeholder:text-slate-400 text-slate-700"
            />
          </div>
          
          <div className="h-8 w-px bg-slate-200 hidden lg:block"></div>

          <div className="flex items-center gap-3 pr-2">
            <span className="text-[11px] font-bold text-slate-500 uppercase tracking-wider">ROLE:</span>
            <select
              value={filterRole}
              onChange={(e) => { setFilterRole(e.target.value); setPage(1); }}
              className="rounded border border-slate-200 bg-white px-3 py-1.5 text-sm font-semibold text-slate-700 outline-none focus:border-[#38bdf8] hover:bg-slate-50 shadow-sm"
            >
              <option value="All">All Roles</option>
              <option value="admin">Admin</option>
              <option value="leader">Leader</option>
              <option value="employee">Employee</option>
            </select>
          </div>
        </div>

        <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="overflow-x-auto pb-4">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-50 text-slate-500 border-b border-slate-200">
                <tr>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Employee</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Role</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Status</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Designation</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Date of Joining</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px]">Date of Birth</th>
                  <th className="px-6 py-4 font-bold uppercase tracking-wider text-[11px] text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {isLoading ? (
                  <tr>
                    <td colSpan={7} className="px-6 py-12 text-center text-slate-500">
                      Loading members...
                    </td>
                  </tr>
                ) : isError ? (
                  <tr>
                    <td colSpan={7} className="px-6 py-12 text-center text-red-500">
                      Failed to fetch members. Please try again.
                    </td>
                  </tr>
                ) : filteredItems.length > 0 ? (
                  filteredItems.map(member => (
                    <tr key={member.id} className="transition hover:bg-slate-50/50">
                      <td className="px-6 py-4">
                        <div className="flex items-center gap-3">
                          <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-xs font-bold text-white shadow-sm ${GRADIENT_CYAN_PURPLE}`}>
                            {(member.name || 'U').substring(0, 2).toUpperCase()}
                          </div>
                          <div className="min-w-0">
                            <div className="font-bold text-slate-800 truncate">{member.name || '-'}</div>
                            <div className="text-xs text-slate-500 truncate mt-0.5">{member.email || '-'}</div>
                          </div>
                        </div>
                      </td>
                      <td className="px-6 py-4">
                        <span className={`inline-flex items-center rounded bg-slate-100 px-2 py-0.5 text-xs font-semibold ${
                          member.role === 'admin' ? 'text-purple-600' :
                          member.role === 'leader' ? 'text-blue-600' :
                          'text-slate-600'
                        }`}>
                          {(member.role || '').charAt(0).toUpperCase() + (member.role || '').slice(1)}
                        </span>
                      </td>
                      <td className="px-6 py-4">
                        <StatusBadge status={member.status} />
                      </td>
                      <td className="px-6 py-4 font-medium text-slate-600">{member.designation || '-'}</td>
                      <td className="px-6 py-4 font-medium text-slate-600">{formatDate(member.date_of_joining)}</td>
                      <td className="px-6 py-4 font-medium text-slate-600">{formatDate(member.date_of_birth)}</td>
                      <td className="px-6 py-4 text-right">
                        <div className="flex items-center justify-end gap-2">
                          <button
                            onClick={() => openEditDrawer(member)}
                            className="rounded px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-[#14B8A6] border border-[#14B8A6]/30 transition hover:bg-[#14B8A6]/10"
                          >
                            Edit
                          </button>
                          <button
                            onClick={() => handleDeleteMember(member.id)}
                            className="rounded px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-rose-500 border border-rose-200 transition hover:bg-rose-50"
                          >
                            Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={7} className="px-6 py-12 text-center text-slate-500">
                      No members found matching your criteria.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          
          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between border-t border-slate-100 bg-white px-6 py-3">
              <span className="text-xs text-slate-500">
                Showing {((page - 1) * PAGE_SIZE) + 1} to {Math.min(page * PAGE_SIZE, data?.total || 0)} of {data?.total || 0} Members
              </span>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page === 1}
                  className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-semibold text-slate-600 transition disabled:opacity-40 hover:bg-slate-50"
                >
                  Prev
                </button>
                <span className="px-3 text-xs font-bold text-slate-800">{page} / {totalPages}</span>
                <button
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page === totalPages}
                  className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-semibold text-slate-600 transition disabled:opacity-40 hover:bg-slate-50"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Right Slide-over Drawer for Create / Edit */}
      <div className={`fixed inset-0 z-50 overflow-hidden ${isDrawerOpen ? 'pointer-events-auto' : 'pointer-events-none'}`}>
        <div 
          className={`absolute inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity duration-300 ${isDrawerOpen ? 'opacity-100' : 'opacity-0'}`} 
          onClick={() => setIsDrawerOpen(false)} 
        />
        <div className={`absolute inset-y-0 right-0 w-full max-w-md bg-white shadow-2xl transition-transform duration-300 ease-in-out ${isDrawerOpen ? 'translate-x-0' : 'translate-x-full'}`}>
          <div className="flex h-full flex-col">
            <div className="flex items-center justify-between border-b border-slate-100 px-6 py-5">
              <h3 className="text-lg font-bold text-slate-800">
                {drawerMode === 'create' ? 'Add New Member' : 'Edit Member'}
              </h3>
              <button type="button" onClick={() => setIsDrawerOpen(false)} className="text-slate-400 hover:text-slate-600">
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            
            <div className="flex-1 overflow-y-auto">
              <form id="member-form" onSubmit={handleSaveMember} className="p-6 space-y-6">
                <div>
                  <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Employee Name</label>
                  <input
                    required
                    type="text"
                    value={formName}
                    onChange={e => setFormName(e.target.value)}
                    className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium"
                    placeholder="E.g. John Doe"
                  />
                </div>

                <div>
                  <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Email Address</label>
                  <input
                    required
                    type="email"
                    value={formEmail}
                    onChange={e => setFormEmail(e.target.value)}
                    className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium"
                    placeholder="john.doe@company.com"
                  />
                </div>
                
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Role</label>
                    <select
                      required
                      value={formRole}
                      onChange={e => setFormRole(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium bg-white"
                    >
                      <option value="employee">Employee</option>
                      <option value="leader">Leader</option>
                      <option value="admin">Admin</option>
                    </select>
                  </div>
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Status</label>
                    <select
                      required
                      value={formStatus}
                      onChange={e => setFormStatus(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium bg-white"
                    >
                      <option value="active">Active</option>
                      <option value="inactive">Inactive</option>
                    </select>
                  </div>
                </div>

                <div className="grid grid-cols-1 gap-4 pt-2 border-t border-slate-100">
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Designation</label>
                    <input
                      type="text"
                      value={formDesignation}
                      onChange={e => setFormDesignation(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium text-slate-700"
                      placeholder="e.g. Full Stack Developer"
                    />
                  </div>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-2 border-t border-slate-100">
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Date of Joining</label>
                    <input
                      type="date"
                      value={formDOJ}
                      onChange={e => setFormDOJ(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium text-slate-700"
                    />
                  </div>
                  <div>
                    <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">Date of Birth</label>
                    <input
                      type="date"
                      value={formDOB}
                      onChange={e => setFormDOB(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-4 py-2.5 outline-none focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6] text-sm font-medium text-slate-700"
                    />
                  </div>
                </div>
              </form>
            </div>
            
            <div className="border-t border-slate-100 p-6 bg-slate-50">
              <button
                type="submit"
                form="member-form"
                className={`w-full rounded-lg px-6 py-3 text-sm font-bold text-white shadow-md hover:opacity-90 transition-opacity ${GRADIENT_CYAN_PURPLE}`}
              >
                {drawerMode === 'create' ? 'Save Member' : 'Save Changes'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </V2Shell>
  );
};
