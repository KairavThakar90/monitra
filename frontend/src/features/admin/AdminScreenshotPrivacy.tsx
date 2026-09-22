import React, { useState } from 'react';
import {
  useGetScreenshotApplicationsQuery,
  useGetScreenshotUrlsQuery,
  useGetUserExclusionsQuery,
  useCreateUserExclusionMutation,
  useUpdateUserExclusionMutation,
  useDeleteUserExclusionMutation,
  useCreateScreenshotApplicationMutation,
  useCreateScreenshotUrlMutation,
} from '../../api/screenshotPrivacy';
import { useGetMembersQuery } from '../../store/api/membersApi';
import { V2Shell } from './../dashboard/v2/V2Shell';

export const AdminScreenshotPrivacy: React.FC = () => {
  const [selectedUserId, setSelectedUserId] = useState<number | null>(null);

  const { data: membersData, isLoading: membersLoading } = useGetMembersQuery({ 
    limit: 100, 
    status: 'active' 
  });
  const members = membersData?.items;
  
  const { data: apps, isLoading: appsLoading } = useGetScreenshotApplicationsQuery();
  const { data: urls, isLoading: urlsLoading } = useGetScreenshotUrlsQuery();

  const { data: exclusions = [], isLoading: exclusionsLoading } = useGetUserExclusionsQuery(
    selectedUserId ?? 0,
    { skip: !selectedUserId }
  );

  const [createExclusion] = useCreateUserExclusionMutation();
  const [updateExclusion] = useUpdateUserExclusionMutation();
  const [deleteExclusion] = useDeleteUserExclusionMutation();

  const isLoading = membersLoading || appsLoading || urlsLoading || exclusionsLoading;

  const handleToggleApp = async (appId: number, isExcludedCurrently: boolean, exclusionId?: number) => {
    if (!selectedUserId) return;
    if (isExcludedCurrently && exclusionId) {
      await deleteExclusion({ id: exclusionId, user_id: selectedUserId });
    } else if (!isExcludedCurrently) {
      await createExclusion({
        user_id: selectedUserId,
        application_id: appId,
        exclusion_type: 'application',
        is_excluded: true,
      });
    }
  };

  const handleToggleUrl = async (urlId: number, isExcludedCurrently: boolean, exclusionId?: number) => {
    if (!selectedUserId) return;
    if (isExcludedCurrently && exclusionId) {
      await deleteExclusion({ id: exclusionId, user_id: selectedUserId });
    } else if (!isExcludedCurrently) {
      await createExclusion({
        user_id: selectedUserId,
        url_id: urlId,
        exclusion_type: 'url',
        is_excluded: true,
      });
    }
  };

  const [createApplication] = useCreateScreenshotApplicationMutation();
  const [createUrl] = useCreateScreenshotUrlMutation();

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerType, setDrawerType] = useState<'application'|'url'>('application');
  const [form, setForm] = useState({ name: '', process_name: '', domain: '', url_pattern: '', category: '' });

  const closeDrawer = () => {
    setDrawerOpen(false);
    setForm({ name: '', process_name: '', domain: '', url_pattern: '', category: '' });
  };

  const handleCreateRule = async () => {
    if (drawerType === 'application') {
      if (!form.name || !form.process_name || !form.category) return;
      await createApplication({ name: form.name, process_name: form.process_name, category: form.category, is_active: true });
    } else {
      if (!form.name || !form.domain || !form.url_pattern || !form.category) return;
      await createUrl({ name: form.name, domain: form.domain, url_pattern: form.url_pattern, category: form.category, is_active: true });
    }
    closeDrawer();
  };

  const headerActions = (
    <button 
      onClick={() => setDrawerOpen(true)} 
      className="bg-blue-600 text-white px-4 py-2 rounded-lg shadow-sm text-sm font-medium transition hover:bg-blue-700"
    >
      + Add New Rule
    </button>
  );

  return (
    <V2Shell title="Settings" subtitle="Screenshot Privacy Controls" actions={headerActions}>
      <div className="w-full space-y-6 pb-20">
        
        {/* User Selection Card */}
        <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <div>
            <h2 className="text-lg font-semibold text-slate-800">Target User</h2>
            <p className="text-sm text-slate-500">Select an employee to manage their allowed and excluded applications.</p>
          </div>
          <div className="w-full md:w-80">
            <select 
              className="w-full rounded-lg border-slate-300 border p-2.5 text-sm text-slate-700 shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
              value={selectedUserId || ''} 
              onChange={(e) => setSelectedUserId(Number(e.target.value))}
            >
              <option value="" disabled>Select User ▼</option>
              {members?.map(member => (
                <option key={member.id} value={member.id}>{member.name} ({member.email})</option>
              ))}
            </select>
          </div>
        </div>

      {isLoading && (
        <div className="flex min-h-28 items-center justify-center">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-500 border-t-transparent" />
        </div>
      )}

      {selectedUserId && !isLoading && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Applications */}
          <div className="flex flex-col gap-4">
            <h3 className="text-lg font-semibold text-slate-800 flex items-center gap-2">
              <svg className="w-5 h-5 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"></path></svg>
              Desktop Applications
            </h3>
            <div className="space-y-3">
              {apps?.map(app => {
                const exclusion = exclusions.find(e => e.exclusion_type === 'application' && e.application_id === app.id);
                const isExcluded = !!exclusion && exclusion.is_excluded;
                
                return (
                  <div key={app.id} className="flex items-center justify-between p-4 border border-slate-200 bg-white rounded-xl shadow-sm transition hover:shadow-md">
                    <div>
                      <div className="font-medium text-slate-800">{app.name}</div>
                      <div className="text-xs text-slate-500 mt-0.5">{app.category}</div>
                    </div>
                    <div className="flex items-center gap-4">
                      <span className={`text-xs font-semibold px-2.5 py-1 rounded-full ${!isExcluded ? 'bg-green-100 text-green-700' : 'bg-rose-100 text-rose-700'}`}>
                        {!isExcluded ? 'Allowed' : 'Excluded'}
                      </span>
                      <button
                        type="button"
                        onClick={() => handleToggleApp(app.id, isExcluded, exclusion?.id)}
                        className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${!isExcluded ? 'bg-blue-600' : 'bg-slate-300'}`}
                      >
                        <span className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${!isExcluded ? 'translate-x-4' : 'translate-x-0'}`} />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* URLs */}
          <div className="flex flex-col gap-4">
            <h3 className="text-lg font-semibold text-slate-800 flex items-center gap-2">
              <svg className="w-5 h-5 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9"></path></svg>
              Website URLs
            </h3>
            <div className="space-y-3">
              {urls?.map(url => {
                const exclusion = exclusions.find(e => e.exclusion_type === 'url' && e.url_id === url.id);
                const isExcluded = !!exclusion && exclusion.is_excluded;
                
                return (
                  <div key={url.id} className="flex items-center justify-between p-4 border border-slate-200 bg-white rounded-xl shadow-sm transition hover:shadow-md">
                    <div>
                      <div className="font-medium text-slate-800">{url.name}</div>
                      <div className="text-xs text-slate-500 mt-0.5">{url.domain}</div>
                    </div>
                    <div className="flex items-center gap-4">
                      <span className={`text-xs font-semibold px-2.5 py-1 rounded-full ${!isExcluded ? 'bg-green-100 text-green-700' : 'bg-rose-100 text-rose-700'}`}>
                        {!isExcluded ? 'Allowed' : 'Excluded'}
                      </span>
                      <button
                        type="button"
                        onClick={() => handleToggleUrl(url.id, isExcluded, exclusion?.id)}
                        className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${!isExcluded ? 'bg-blue-600' : 'bg-slate-300'}`}
                      >
                        <span className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${!isExcluded ? 'translate-x-4' : 'translate-x-0'}`} />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}
      </div>

      {/* Drawer */}
      <div className={`fixed inset-0 z-50 overflow-hidden ${drawerOpen ? "pointer-events-auto" : "pointer-events-none"}`}>
        <div
          className={`absolute inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity duration-300 ${drawerOpen ? "opacity-100" : "opacity-0"}`}
          onClick={closeDrawer}
        />
        <div className={`absolute inset-y-0 right-0 w-full max-w-md bg-white shadow-2xl transition-transform duration-300 ease-in-out flex flex-col ${drawerOpen ? "translate-x-0" : "translate-x-full"}`}>
          
          <div className="flex items-center justify-between border-b border-slate-100 px-6 py-4">
            <h2 className="text-lg font-semibold text-slate-800">Add New Rule</h2>
            <button onClick={closeDrawer} className="rounded-full p-2 text-slate-400 transition hover:bg-slate-50 hover:text-slate-600">
              <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          <div className="flex-1 overflow-y-auto px-6 py-6">
            <div className="mb-6 flex gap-4">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="radio" name="ruleType" checked={drawerType === 'application'} onChange={() => setDrawerType('application')} className="text-blue-600" />
                Application
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="radio" name="ruleType" checked={drawerType === 'url'} onChange={() => setDrawerType('url')} className="text-blue-600" />
                Website URL
              </label>
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-1">Display Name</label>
                <input type="text" value={form.name} onChange={e => setForm({...form, name: e.target.value})} className="w-full rounded-lg border-slate-300 border p-2" placeholder="e.g., Slack" />
              </div>
              
              {drawerType === 'application' && (
                <div>
                  <label className="block text-sm font-medium text-slate-700 mb-1">Process Name</label>
                  <input type="text" value={form.process_name} onChange={e => setForm({...form, process_name: e.target.value})} className="w-full rounded-lg border-slate-300 border p-2" placeholder="e.g., slack.exe" />
                </div>
              )}

              {drawerType === 'url' && (
                <>
                  <div>
                    <label className="block text-sm font-medium text-slate-700 mb-1">Domain</label>
                    <input type="text" value={form.domain} onChange={e => setForm({...form, domain: e.target.value})} className="w-full rounded-lg border-slate-300 border p-2" placeholder="e.g., slack.com" />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-slate-700 mb-1">URL Pattern</label>
                    <input type="text" value={form.url_pattern} onChange={e => setForm({...form, url_pattern: e.target.value})} className="w-full rounded-lg border-slate-300 border p-2" placeholder="e.g., https://app.slack.com/*" />
                  </div>
                </>
              )}

              <div>
                <label className="block text-sm font-medium text-slate-700 mb-1">Category</label>
                <input type="text" value={form.category} onChange={e => setForm({...form, category: e.target.value})} className="w-full rounded-lg border-slate-300 border p-2" placeholder="e.g., Communication" />
              </div>
            </div>
          </div>

          <div className="border-t border-slate-100 bg-slate-50 px-6 py-4 flex justify-end gap-3">
            <button onClick={closeDrawer} className="rounded-lg px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-200">Cancel</button>
            <button onClick={handleCreateRule} className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700">Save Rule</button>
          </div>
        </div>
      </div>

    </V2Shell>
  );
};
