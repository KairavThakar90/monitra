import React from 'react';
import { Link } from 'react-router-dom';
import { useAuth } from '../auth/authContext';

/**
 * The client portal's own minimal frame.
 *
 * Deliberately not `V2Shell`: that shell is built around staff navigation
 * (project management, the member directory, reports, settings), and every
 * one of those links would either be hidden by a permission check or, worse,
 * bounce a client back here anyway. A client's account holds none of those
 * permissions, so the honest layout is a page of its own rather than a staff
 * shell with everything conditionally hidden.
 */
export const ClientShell: React.FC<{
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}> = ({ title, subtitle, actions, children }) => {
  const { currentUser, logout } = useAuth();

  return (
    <div className="min-h-screen bg-[#F8FAFC]">
      <header className="sticky top-0 z-10 border-b border-[#E2E8F0] bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
          <Link to="/client/dashboard" className="flex items-center gap-2">
            <img src="/logo.png" alt="Monitra" className="h-8 w-auto object-contain" />
          </Link>
          <div className="flex items-center gap-4">
            <div className="text-right">
              <div className="text-sm font-semibold text-[#0F172A]">{currentUser?.name}</div>
              <div className="text-xs text-[#94A3B8]">{currentUser?.email}</div>
            </div>
            <button
              onClick={logout}
              className="rounded-md border border-[#E2E8F0] px-3 py-1.5 text-xs font-semibold text-[#475569] hover:bg-[#F1F5F9]"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-6 py-8">
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold tracking-tight text-[#0F172A]">{title}</h1>
            {subtitle && <p className="mt-1 text-sm text-[#64748B]">{subtitle}</p>}
          </div>
          {actions}
        </div>
        {children}
      </main>
    </div>
  );
};
