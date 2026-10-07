import React, { useMemo, useState } from 'react';
import type { ScreenshotApplication, ScreenshotUrl } from '../../api/screenshotPrivacy';
import { PillTabs } from '../feedback/feedbackFilters';
import { formatISTDate } from '../../utils/duration';

/**
 * Every privacy rule that has been added, as Applications and Websites tabs.
 *
 * Shown on the Screenshot Privacy page until a member is picked, in place of an
 * empty "choose a member" card: the shared catalogue is the thing an
 * administrator has just been adding to, and seeing it is how they know a rule
 * saved. Picking a member swaps it for that member's Captured / Excluded
 * switches. Read-only on purpose -- it lists the catalogue; it does not change it.
 *
 * Newest first, so a rule added a moment ago is on top.
 */

type Tab = 'applications' | 'websites';

/** One row, whichever kind of rule it is. */
interface Row {
  id: number;
  name: string;
  /** Process name, or domain. */
  detail: string;
  /** A website's URL pattern. */
  pattern?: string;
  category: string;
  active: boolean;
  added: string;
}

const timeOf = (value: string) => {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
};

const newestFirst = (a: Row, b: Row) => timeOf(b.added) - timeOf(a.added) || b.id - a.id;

const toApplicationRow = (rule: ScreenshotApplication): Row => ({
  id: rule.id,
  name: rule.name,
  detail: rule.process_name,
  category: rule.category,
  active: rule.is_active,
  added: rule.created_at,
});

const toWebsiteRow = (rule: ScreenshotUrl): Row => ({
  id: rule.id,
  name: rule.name,
  detail: rule.domain,
  pattern: rule.url_pattern,
  category: rule.category,
  active: rule.is_active,
  added: rule.created_at,
});

const matches = (row: Row, term: string) =>
  [row.name, row.detail, row.pattern ?? '', row.category].some((value) => value.toLowerCase().includes(term));

const AppIcon = (
  <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
    />
  </svg>
);

const WebIcon = (
  <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9"
    />
  </svg>
);

export const PrivacyRuleCatalogue: React.FC<{
  applications: ScreenshotApplication[];
  websites: ScreenshotUrl[];
}> = ({ applications, websites }) => {
  // Null until the reader chooses, so the tab that opens is the one with
  // something in it rather than an empty Applications tab beside a full Websites one.
  const [chosen, setChosen] = useState<Tab | null>(null);
  const [search, setSearch] = useState('');

  const appRows = useMemo(() => applications.map(toApplicationRow).sort(newestFirst), [applications]);
  const webRows = useMemo(() => websites.map(toWebsiteRow).sort(newestFirst), [websites]);

  if (appRows.length === 0 && webRows.length === 0) {
    return (
      <div className="rounded-xl border border-[#E2E8F0] bg-white p-12 text-center shadow-sm">
        <svg className="mx-auto h-12 w-12 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.5"
            d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z M15 13a3 3 0 11-6 0 3 3 0 016 0z"
          />
        </svg>
        <h3 className="mt-4 text-sm font-bold text-slate-800">No privacy rules yet</h3>
        <p className="mt-1 text-xs font-medium text-slate-500">
          Add an application or website with “+ Add Privacy Rule”. No member selected yet — once you have rules, choose
          a member above to switch them on or off for that member.
        </p>
      </div>
    );
  }

  const tab: Tab = chosen ?? (appRows.length === 0 ? 'websites' : 'applications');
  const rows = tab === 'applications' ? appRows : webRows;
  const term = search.trim().toLowerCase();
  const visible = term ? rows.filter((row) => matches(row, term)) : rows;
  const noun = tab === 'applications' ? 'application' : 'website';

  return (
    <section className="flex flex-col overflow-hidden rounded-xl border border-[#E2E8F0] bg-white shadow-sm">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[#F1F5F9] px-5 py-4">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-[#EFF6FF] text-[#2563EB]">
            {tab === 'applications' ? AppIcon : WebIcon}
          </div>
          <div>
            <h3 className="text-[15px] font-bold text-[#0F172A]">Privacy rules</h3>
            <p className="text-[11px] text-[#94A3B8]">
              No member selected — choose one above to switch these on or off for that member.
            </p>
          </div>
        </div>
        <PillTabs<Tab>
          options={[
            { id: 'applications', label: 'Applications' },
            { id: 'websites', label: 'Websites' },
          ]}
          value={tab}
          onChange={(next) => {
            setChosen(next);
            setSearch('');
          }}
          counts={{ applications: appRows.length, websites: webRows.length }}
          label="Rule type"
        />
      </header>

      <div className="border-b border-[#F1F5F9] px-5 py-3">
        <input
          type="text"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder={`Search ${noun}s…`}
          aria-label={`Search ${noun}s`}
          className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-700 outline-none transition focus:border-[#3B82F6]"
        />
      </div>

      {rows.length === 0 ? (
        <p className="px-5 py-10 text-center text-sm text-[#94A3B8]">
          No {noun} rules yet — add one with “+ Add Privacy Rule”.
        </p>
      ) : visible.length === 0 ? (
        <p className="px-5 py-10 text-center text-sm text-[#94A3B8]">Nothing matches this search.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-left text-sm">
            <thead className="bg-[#F8FAFC] text-[11px] font-bold uppercase tracking-wider text-[#64748B]">
              <tr>
                <th className="px-5 py-3">{tab === 'applications' ? 'Application' : 'Website'}</th>
                <th className="px-5 py-3">{tab === 'applications' ? 'Process' : 'Domain'}</th>
                {tab === 'websites' && <th className="px-5 py-3">URL pattern</th>}
                <th className="px-5 py-3">Category</th>
                <th className="px-5 py-3">Status</th>
                <th className="px-5 py-3 text-right">Added</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#F1F5F9]">
              {visible.map((row) => (
                <tr key={row.id} className="transition hover:bg-[#F8FAFC]/60">
                  <td className="px-5 py-3">
                    <div className="flex items-center gap-3">
                      <span
                        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[#EFF6FF] text-[12px] font-bold text-[#2563EB]"
                        aria-hidden="true"
                      >
                        {(row.name.trim()[0] ?? '?').toUpperCase()}
                      </span>
                      <span className="min-w-0 truncate font-semibold text-[#0F172A]" title={row.name}>
                        {row.name}
                      </span>
                    </div>
                  </td>
                  <td className="px-5 py-3 text-[#475569]">
                    <span className="font-mono text-[12px]">{row.detail || '—'}</span>
                  </td>
                  {tab === 'websites' && (
                    <td className="px-5 py-3 text-[#64748B]">
                      <span className="font-mono text-[12px]">{row.pattern || '—'}</span>
                    </td>
                  )}
                  <td className="px-5 py-3">
                    <span className="inline-flex items-center rounded-md bg-slate-100 px-2.5 py-1 text-[11px] font-bold text-slate-600">
                      {row.category || '—'}
                    </span>
                  </td>
                  <td className="px-5 py-3">
                    <span
                      className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-bold ${
                        row.active
                          ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                          : 'border-slate-200 bg-slate-50 text-slate-500'
                      }`}
                    >
                      {row.active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td className="whitespace-nowrap px-5 py-3 text-right text-[12px] font-semibold text-[#64748B]">
                    {formatISTDate(row.added) || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
};
