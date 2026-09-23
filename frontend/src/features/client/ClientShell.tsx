import React, { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/authContext';
import { openPathInNewTab, opensInNewTab } from '../../utils/navigation';
import { BrandLockup } from '../dashboard/v2/V2Shell';

/**
 * The client portal's own chrome.
 *
 * A sibling of `V2Shell`/`MemberShell` rather than a mode of either — same
 * dark sidebar, same brand lockup, same active-item gradient, so a client
 * signing in sees the same Monitra design language everyone else does. It is
 * still its own component because its nav is a fixed, short list (Projects,
 * Timing, Members, Tasks) with nothing conditionally hidden: a client account
 * holds exactly one permission (`clients:view_shared`) and none of the staff
 * screens are ever reachable from here.
 */

const getInitials = (name: string) => {
  if (!name) return 'C';
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
};

const brandGradient = 'linear-gradient(135deg, #0ea5e9 0%, #3b82f6 50%, #8b5cf6 100%)';

type NavItem = { path: string; label: string; icon: React.ReactNode };

const icon = (d: string) => <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d={d} />;

const NAV: NavItem[] = [
  {
    path: '/client/dashboard',
    label: 'Projects',
    icon: icon(
      'M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10'
    ),
  },
  {
    path: '/client/timing',
    label: 'Timing',
    icon: icon('M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z'),
  },
  {
    path: '/client/members',
    label: 'Members',
    icon: icon(
      'M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z'
    ),
  },
  {
    path: '/client/tasks',
    label: 'Tasks',
    icon: icon(
      'M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2'
    ),
  },
];

export const ClientShell: React.FC<{
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}> = ({ title, subtitle, actions, children }) => {
  const { currentUser, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  const isActive = (path: string) =>
    location.pathname === path || location.pathname.startsWith(`${path}/`);

  const go = (event: React.MouseEvent, path: string) => {
    if (opensInNewTab(event)) {
      openPathInNewTab(path);
      return;
    }
    navigate(path);
    setMobileMenuOpen(false);
  };

  const navButton = (item: NavItem) => {
    const active = isActive(item.path);
    return (
      <button
        key={item.path}
        onClick={(event) => go(event, item.path)}
        className={
          'flex w-full cursor-pointer items-center gap-3 rounded-lg px-3.5 py-3 text-left text-sm font-medium leading-snug transition duration-150 ' +
          (active ? 'text-white shadow-sm' : 'text-[#94A3B8] hover:bg-slate-800/40 hover:text-white')
        }
        style={active ? { background: brandGradient } : undefined}
      >
        <svg
          className={'h-5 w-5 ' + (active ? 'text-white' : 'text-[#22D3EE]')}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          {item.icon}
        </svg>
        <span className="flex-1">{item.label}</span>
      </button>
    );
  };

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[#F8FAFC] font-sans text-slate-800">
      <div
        className={`fixed inset-0 z-40 bg-slate-900/50 backdrop-blur-sm transition-opacity lg:hidden ${
          mobileMenuOpen ? 'opacity-100 pointer-events-auto' : 'opacity-0 pointer-events-none'
        }`}
        onClick={() => setMobileMenuOpen(false)}
      />

      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-[280px] shrink-0 flex-col justify-between border-r border-slate-800 bg-[#0B1220] p-6 text-slate-400 transition-transform duration-300 lg:static lg:translate-x-0 ${
          mobileMenuOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="flex min-h-0 flex-grow flex-col">
          <div className="mb-8 flex shrink-0 items-start gap-3">
            <BrandLockup caption="Client Portal" />
            <button
              className="-mr-1 shrink-0 text-slate-400 hover:text-white lg:hidden"
              onClick={() => setMobileMenuOpen(false)}
            >
              <svg className="h-6 w-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          <div className="custom-scrollbar min-h-0 flex-grow space-y-1 overflow-y-auto">
            {NAV.map(navButton)}
          </div>
        </div>

        <div className="shrink-0 space-y-4 border-t border-slate-800 pt-4">
          {currentUser && (
            <div className="flex items-center gap-3 rounded-xl border border-slate-700/40 bg-slate-800/60 p-3">
              <div
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-sm font-bold text-white"
                style={{ background: brandGradient }}
              >
                {getInitials(currentUser.name)}
              </div>
              <div className="min-w-0 flex-grow">
                <div className="truncate text-xs font-semibold leading-normal text-white" title={currentUser.name}>
                  {currentUser.name}
                </div>
                <div className="truncate text-[10px] leading-normal text-[#64748B]" title={currentUser.email}>
                  {currentUser.email}
                </div>
                <div className="truncate text-[10px] leading-normal text-[#94A3B8]">Client</div>
              </div>
            </div>
          )}
          <button
            onClick={handleLogout}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-slate-800 px-4 py-2.5 text-sm font-semibold text-[#94A3B8] transition hover:bg-slate-700 hover:text-white"
          >
            Sign Out
          </button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-grow flex-col">
        <header className="flex min-h-16 shrink-0 items-center gap-4 border-b border-[#E2E8F0] bg-white px-4 py-3 lg:px-8">
          <button
            className="shrink-0 rounded-md p-1.5 text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-700 lg:hidden"
            onClick={() => setMobileMenuOpen(true)}
          >
            <svg className="h-6 w-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-lg font-bold tracking-tight text-[#0F172A]">{title}</h1>
            {subtitle && <p className="mt-0.5 truncate text-xs text-[#64748B]">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>

        <main className="flex-grow overflow-y-auto p-4 lg:p-8">{children}</main>
      </div>
    </div>
  );
};
