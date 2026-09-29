import React, { useMemo, useState } from 'react';
import {
  useGetScreenshotApplicationsQuery,
  useGetScreenshotUrlsQuery,
  useGetUserExclusionsQuery,
  useCreateUserExclusionMutation,
  useDeleteUserExclusionMutation,
  useCreateScreenshotApplicationMutation,
  useCreateScreenshotUrlMutation,
} from '../../api/screenshotPrivacy';
import { useGetMembersQuery } from '../../store/api/membersApi';
import { V2Shell } from './../dashboard/v2/V2Shell';

/**
 * Screenshot Privacy: which applications and websites are blurred/skipped in
 * a member's captures. The admin picks a member, then flips each rule
 * between Captured and Excluded for that member; "+ Add Privacy Rule" adds a
 * new application or URL rule to the shared catalogue.
 *
 * Redesigned (2026-09-29) to the same design language as the rest of the
 * admin area — V2Shell tables with uppercase slate headers, per-section
 * search, honest empty states — with the data flow untouched: the same
 * queries, the same per-member exclusion toggle semantics, the same
 * create-rule drawer.
 */

const inputClass =
  'w-full rounded-lg border border-slate-300 bg-white px-4 py-2.5 text-sm font-medium text-slate-700 shadow-sm outline-none transition focus:border-[#3B82F6] focus:ring-1 focus:ring-[#3B82F6]';

/** The Allowed/Excluded pill + switch pair, shared by both rule tables. */
const ExclusionToggle: React.FC<{
  isExcluded: boolean;
  onToggle: () => void;
}> = ({ isExcluded, onToggle }) => (
  <div className="flex items-center justify-end gap-3">
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-bold ${
        !isExcluded
          ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
          : 'border-rose-200 bg-rose-50 text-rose-600'
      }`}
    >
      {!isExcluded ? 'Captured' : 'Excluded'}
    </span>
    <button
      type="button"
      onClick={onToggle}
      role="switch"
      aria-checked={!isExcluded}
      title={!isExcluded ? 'Exclude from this member’s screenshots' : 'Capture again'}
      className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
        !isExcluded ? 'bg-[#2563EB]' : 'bg-slate-300'
      }`}
    >
      <span
        className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow transition duration-200 ease-in-out ${
          !isExcluded ? 'translate-x-4' : 'translate-x-0'
        }`}
      />
    </button>
  </div>
);

/** One rule section: icon + title header, search, admin-style table. */
const RuleSection: React.FC<{
  title: string;
  hint: string;
  icon: React.ReactNode;
  columns: [string, string];
  rows: { id: number; primary: string; secondary: string; isExcluded: boolean; onToggle: () => void }[];
  excludedCount: number;
  emptyMessage: string;
}> = ({ title, hint, icon, columns, rows, excludedCount, emptyMessage }) => {
  const [search, setSearch] = useState('');
  const visible = search.trim()
    ? rows.filter(
        (row) =>
          row.primary.toLowerCase().includes(search.trim().toLowerCase()) ||
          row.secondary.toLowerCase().includes(search.trim().toLowerCase()),
      )
    : rows;

  return (
    <section className="flex flex-col overflow-hidden rounded-xl border border-[#E2E8F0] bg-white shadow-sm">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[#F1F5F9] px-5 py-4">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-[#EFF6FF] text-[#2563EB]">
            {icon}
          </div>
          <div>
            <h3 className="text-[15px] font-bold text-[#0F172A]">{title}</h3>
            <p className="text-[11px] text-[#94A3B8]">{hint}</p>
          </div>
        </div>
        <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-bold text-[#64748B]">
          {excludedCount} of {rows.length} excluded
        </span>
      </header>

      <div className="border-b border-[#F1F5F9] px-5 py-3">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={`Search ${title.toLowerCase()}…`}
          className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-700 outline-none transition focus:border-[#3B82F6]"
        />
      </div>

      {visible.length === 0 ? (
        <p className="px-5 py-10 text-center text-sm text-[#94A3B8]">
          {rows.length === 0 ? emptyMessage : 'Nothing matches this search.'}
        </p>
      ) : (
        <table className="w-full text-left text-sm">
          <thead className="bg-[#F8FAFC] text-[11px] font-bold uppercase tracking-wider text-[#64748B]">
            <tr>
              <th className="px-5 py-3">{columns[0]}</th>
              <th className="px-5 py-3">{columns[1]}</th>
              <th className="px-5 py-3 text-right">Screenshots</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[#F1F5F9]">
            {visible.map((row) => (
              <tr key={row.id} className="transition hover:bg-[#F8FAFC]/60">
                <td className="px-5 py-3 font-medium text-[#0F172A]">{row.primary}</td>
                <td className="px-5 py-3 text-[#64748B]">{row.secondary || '—'}</td>
                <td className="px-5 py-3">
                  <ExclusionToggle isExcluded={row.isExcluded} onToggle={row.onToggle} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
};

export const AdminScreenshotPrivacy: React.FC = () => {
  const [selectedUserId, setSelectedUserId] = useState<number | null>(null);

  const { data: membersData, isLoading: membersLoading } = useGetMembersQuery({
    limit: 100,
    status: 'active',
  });
  const members = membersData?.items;
  const selectedMember = members?.find((member) => member.id === selectedUserId);

  const { data: apps, isLoading: appsLoading } = useGetScreenshotApplicationsQuery();
  const { data: urls, isLoading: urlsLoading } = useGetScreenshotUrlsQuery();

  const { data: exclusions = [], isLoading: exclusionsLoading } = useGetUserExclusionsQuery(
    selectedUserId ?? 0,
    { skip: !selectedUserId },
  );

  const [createExclusion] = useCreateUserExclusionMutation();
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

  const appRows = useMemo(
    () =>
      (apps ?? []).map((app) => {
        const exclusion = exclusions.find(
          (e) => e.exclusion_type === 'application' && e.application_id === app.id,
        );
        const isExcluded = !!exclusion && exclusion.is_excluded;
        return {
          id: app.id,
          primary: app.name,
          secondary: app.category ?? '',
          isExcluded,
          onToggle: () => void handleToggleApp(app.id, isExcluded, exclusion?.id),
        };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [apps, exclusions, selectedUserId],
  );

  const urlRows = useMemo(
    () =>
      (urls ?? []).map((url) => {
        const exclusion = exclusions.find((e) => e.exclusion_type === 'url' && e.url_id === url.id);
        const isExcluded = !!exclusion && exclusion.is_excluded;
        return {
          id: url.id,
          primary: url.name,
          secondary: url.domain ?? '',
          isExcluded,
          onToggle: () => void handleToggleUrl(url.id, isExcluded, exclusion?.id),
        };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [urls, exclusions, selectedUserId],
  );

  const [createApplication] = useCreateScreenshotApplicationMutation();
  const [createUrl] = useCreateScreenshotUrlMutation();

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerType, setDrawerType] = useState<'application' | 'url'>('application');
  const [form, setForm] = useState({ name: '', process_name: '', domain: '', url_pattern: '', category: '' });

  const closeDrawer = () => {
    setDrawerOpen(false);
    setForm({ name: '', process_name: '', domain: '', url_pattern: '', category: '' });
  };

  const formValid =
    drawerType === 'application'
      ? Boolean(form.name && form.process_name && form.category)
      : Boolean(form.name && form.domain && form.url_pattern && form.category);

  const handleCreateRule = async () => {
    if (!formValid) return;
    if (drawerType === 'application') {
      await createApplication({
        name: form.name,
        process_name: form.process_name,
        category: form.category,
        is_active: true,
      });
    } else {
      await createUrl({
        name: form.name,
        domain: form.domain,
        url_pattern: form.url_pattern,
        category: form.category,
        is_active: true,
      });
    }
    closeDrawer();
  };

  const field = (label: string, node: React.ReactNode) => (
    <div>
      <label className="mb-2 block text-xs font-bold uppercase tracking-wider text-slate-500">
        {label} <span className="text-rose-500">*</span>
      </label>
      {node}
    </div>
  );

  return (
    <V2Shell
      title="Screenshot Privacy"
      subtitle="Choose which applications and websites are excluded from each member's screenshots."
      actions={
        <button
          onClick={() => setDrawerOpen(true)}
          className="cursor-pointer rounded-lg bg-gradient-to-r from-[#3B82F6] to-[#8B5CF6] px-4 py-2 text-sm font-bold text-white shadow-md transition hover:opacity-90"
        >
          + Add Privacy Rule
        </button>
      }
    >
      <div className="w-full space-y-6 pb-20">
        {/* Member picker */}
        <div className="flex flex-col items-start justify-between gap-4 rounded-xl border border-[#E2E8F0] bg-white p-6 shadow-sm md:flex-row md:items-center">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-gradient-to-r from-[#3B82F6] to-[#8B5CF6] text-white shadow-sm">
              <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="2"
                  d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
                />
              </svg>
            </div>
            <div>
              <h2 className="text-[15px] font-bold text-[#0F172A]">Member</h2>
              <p className="text-[12px] text-[#94A3B8]">
                Privacy rules are per member — pick who these settings apply to.
              </p>
            </div>
          </div>
          <div className="w-full md:w-80">
            <select
              className={inputClass}
              value={selectedUserId || ''}
              onChange={(e) => setSelectedUserId(Number(e.target.value))}
            >
              <option value="" disabled>
                Select a member…
              </option>
              {members?.map((member) => (
                <option key={member.id} value={member.id}>
                  {member.name} ({member.email})
                </option>
              ))}
            </select>
          </div>
        </div>

        {isLoading && (
          <div className="flex min-h-28 items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-4 border-blue-500 border-t-transparent" />
          </div>
        )}

        {!selectedUserId && !isLoading && (
          <div className="rounded-xl border border-[#E2E8F0] bg-white p-12 text-center shadow-sm">
            <svg className="mx-auto h-12 w-12 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="1.5"
                d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z M15 13a3 3 0 11-6 0 3 3 0 016 0z"
              />
            </svg>
            <h3 className="mt-4 text-sm font-bold text-slate-800">No member selected</h3>
            <p className="mt-1 text-xs font-medium text-slate-500">
              Choose a member above to see which applications and websites are excluded from their screenshots.
            </p>
          </div>
        )}

        {selectedUserId && !isLoading && (
          <>
            {selectedMember && (
              <p className="text-[12px] font-semibold text-[#64748B]">
                Managing screenshot privacy for{' '}
                <span className="text-[#0F172A]">{selectedMember.name}</span>. Excluded items are
                blanked in their captures.
              </p>
            )}
            <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
              <RuleSection
                title="Desktop Applications"
                hint="Captures taken while one of these is the active window."
                columns={['Application', 'Category']}
                excludedCount={appRows.filter((row) => row.isExcluded).length}
                rows={appRows}
                emptyMessage="No application rules yet — add one with “+ Add Privacy Rule”."
                icon={
                  <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth="2"
                      d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
                    />
                  </svg>
                }
              />
              <RuleSection
                title="Website URLs"
                hint="Captures taken while one of these sites is open."
                columns={['Website', 'Domain']}
                excludedCount={urlRows.filter((row) => row.isExcluded).length}
                rows={urlRows}
                emptyMessage="No website rules yet — add one with “+ Add Privacy Rule”."
                icon={
                  <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth="2"
                      d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9"
                    />
                  </svg>
                }
              />
            </div>
          </>
        )}
      </div>

      {/* Add-rule drawer */}
      <div className={`fixed inset-0 z-50 overflow-hidden ${drawerOpen ? 'pointer-events-auto' : 'pointer-events-none'}`}>
        <div
          className={`absolute inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity duration-300 ${drawerOpen ? 'opacity-100' : 'opacity-0'}`}
          onClick={closeDrawer}
        />
        <div
          className={`absolute inset-y-0 right-0 flex w-full max-w-md flex-col bg-white shadow-2xl transition-transform duration-300 ease-in-out ${drawerOpen ? 'translate-x-0' : 'translate-x-full'}`}
        >
          <div className="flex items-center justify-between border-b border-slate-100 px-6 py-4">
            <div>
              <h2 className="text-lg font-bold text-slate-800">Add Privacy Rule</h2>
              <p className="text-[12px] text-slate-400">
                Adds to the shared catalogue; exclude it per member afterwards.
              </p>
            </div>
            <button
              onClick={closeDrawer}
              className="cursor-pointer rounded-full p-2 text-slate-400 transition hover:bg-slate-50 hover:text-slate-600"
            >
              <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          <div className="flex-1 overflow-y-auto px-6 py-6">
            {/* Segmented rule-type choice, like the project drawer's billing pair */}
            <div className="mb-6 flex gap-3">
              {(
                [
                  { key: 'application', label: 'Application' },
                  { key: 'url', label: 'Website URL' },
                ] as { key: 'application' | 'url'; label: string }[]
              ).map((option) => (
                <label
                  key={option.key}
                  className={`flex flex-1 cursor-pointer items-center justify-center gap-2 rounded-lg border-2 p-3 transition ${
                    drawerType === option.key
                      ? 'border-[#3B82F6] bg-blue-50 text-[#3B82F6]'
                      : 'border-slate-200 bg-white text-slate-500 hover:bg-slate-50'
                  }`}
                >
                  <input
                    type="radio"
                    name="ruleType"
                    checked={drawerType === option.key}
                    onChange={() => setDrawerType(option.key)}
                    className="sr-only"
                  />
                  <span className="text-sm font-bold">{option.label}</span>
                </label>
              ))}
            </div>

            <div className="space-y-5">
              {field(
                'Display Name',
                <input
                  type="text"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  className={inputClass}
                  placeholder="e.g. Slack"
                />,
              )}

              {drawerType === 'application' &&
                field(
                  'Process Name',
                  <input
                    type="text"
                    value={form.process_name}
                    onChange={(e) => setForm({ ...form, process_name: e.target.value })}
                    className={inputClass}
                    placeholder="e.g. slack.exe"
                  />,
                )}

              {drawerType === 'url' && (
                <>
                  {field(
                    'Domain',
                    <input
                      type="text"
                      value={form.domain}
                      onChange={(e) => setForm({ ...form, domain: e.target.value })}
                      className={inputClass}
                      placeholder="e.g. slack.com"
                    />,
                  )}
                  {field(
                    'URL Pattern',
                    <input
                      type="text"
                      value={form.url_pattern}
                      onChange={(e) => setForm({ ...form, url_pattern: e.target.value })}
                      className={inputClass}
                      placeholder="e.g. https://app.slack.com/*"
                    />,
                  )}
                </>
              )}

              {field(
                'Category',
                <input
                  type="text"
                  value={form.category}
                  onChange={(e) => setForm({ ...form, category: e.target.value })}
                  className={inputClass}
                  placeholder="e.g. Communication"
                />,
              )}
            </div>
          </div>

          <div className="flex justify-end gap-3 border-t border-slate-100 bg-slate-50 px-6 py-4">
            <button
              onClick={closeDrawer}
              className="cursor-pointer rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm font-bold text-slate-600 transition hover:bg-slate-50"
            >
              Cancel
            </button>
            <button
              onClick={handleCreateRule}
              disabled={!formValid}
              className="cursor-pointer rounded-lg bg-gradient-to-r from-[#3B82F6] to-[#8B5CF6] px-4 py-2 text-sm font-bold text-white shadow-md transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Save Rule
            </button>
          </div>
        </div>
      </div>
    </V2Shell>
  );
};
