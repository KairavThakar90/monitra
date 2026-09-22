import React from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './features/auth/authContext'
import { LoginScreen } from './features/auth/LoginScreen'
import { DownloadPage } from './features/download/DownloadPage'
import { DashboardV2 } from './features/dashboard/v2/DashboardV2'
import { ReportPage } from './features/dashboard/v2/ReportPage'
import { AdminProjectManagement } from './features/admin/AdminProjectManagement';
import { AdminTaskListing } from './features/admin/AdminTaskListing';
import { AdminMembers } from './features/admin/AdminMembers';
import { AdminTimeTracking } from './features/admin/AdminTimeTracking';
import { AdminScreenshots } from './features/admin/AdminScreenshots';
import { AdminTeams } from './features/admin/AdminTeams';
import { AdminFeedback } from './features/admin/AdminFeedback';
import { AdminSettings } from './features/admin/AdminSettings';
import { AdminUserManagement } from './features/admin/AdminUserManagement';
import { AdminScreenshotPrivacy } from './features/admin/AdminScreenshotPrivacy';
import { MaintenanceToast } from './components/MaintenanceToast'
import { MemberFeedback } from './features/member/MemberFeedback'
import { canManageSystem, canViewAllFeedback } from './features/auth/roles'
import { MemberDashboard } from './features/member/MemberDashboard'
import { MemberReports } from './features/member/MemberReports'
import { MemberProjects } from './features/member/MemberProjects'
import { MemberTasks } from './features/member/MemberTasks'
import { MemberTimeTracking } from './features/member/MemberTimeTracking'
import { MemberScreenshots } from './features/member/MemberScreenshots'
import { MemberTeam } from './features/member/MemberTeam'
import type { UserRead } from './api/auth'

/**
 * Whether this user may open the project and task management screens.
 *
 * A permission, not a role list: a leader's job is to run projects — they hold
 * `projects:create`, `project_members:manage` and `tasks:create`, and the
 * backend accepts their writes — but the old hardcoded
 * hardcoded administrator-role list bounced them off the very
 * screens those permissions are for.
 */
const canManageProjects = (user: UserRead | null) => !!user?.permissions?.["projects:create"];

/**
 * Whether this user may read the organization's member directory.
 *
 * This is a *permission*, not a role list, because the backend gates the
 * directory endpoints on exactly this permission (`app/api/members.py`,
 * `app/react_apis/member_usage.py`). HR holds `view_employees` without
 * `manage_employees`: it must reach the roster, time-tracking, screenshot and
 * team screens to see every member's details, while the create/edit/deactivate
 * actions inside them stay disabled (see `canManageEmployees`).
 */
const canViewEmployees = (user: UserRead | null) => !!user?.permissions?.["view_employees"];

/**
 * Whether this user's reports and dashboard cover the whole organization.
 *
 * This is the permission the backend actually checks, not a role list. It
 * matters that the two agree: without `time_entries:view_all` the Dashboard
 * and Reports endpoints pin `member_id` to the caller, so a user who lacks it
 * belongs on the member screens, and a manager who has it — an admin role by
 * capability but not by name — still belongs on the org-wide ones.
 */
const canViewAllTime = (user: UserRead | null) => !!user?.permissions?.["time_entries:view_all"];

/**
 * Where a signed-in user belongs.
 *
 * The org-wide screens read the whole organization and the member screens read
 * one person's own work, so "home" is not the same route for everyone. Every
 * redirect in this file goes through here rather than hard-coding
 * `/dashboard`, which used to drop a member onto a page whose every request
 * they are forbidden to make.
 */
const homeFor = (user: UserRead | null) => (canViewAllTime(user) ? "/dashboard" : "/member/dashboard");

const LoadingScreen: React.FC<{ label: string }> = ({ label }) => (
  <div className="min-h-screen flex items-center justify-center bg-[#F8FAFC]">
    <div className="text-sm font-semibold text-[#64748B]">{label}</div>
  </div>
);

/** The `/admin` screens that create or edit projects and tasks. */
const AdminRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, currentUser, isLoading } = useAuth();

  if (isLoading) return <LoadingScreen label="Loading session..." />;
  if (!isAuthenticated) return <Navigate to="/login" replace />;

  return canManageProjects(currentUser) ? <>{children}</> : <Navigate to={homeFor(currentUser)} replace />;
};

/**
 * The `/admin` screens that only *read* the organization's people: the member
 * roster, time tracking, screenshots and teams.
 *
 * Gated on `view_employees` rather than a role name so HR reaches them. The
 * write affordances these pages carry are gated separately on
 * `manage_employees`, so a viewer sees every detail and no create/edit button.
 */
const DirectoryRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, currentUser, isLoading } = useAuth();

  if (isLoading) return <LoadingScreen label="Loading session..." />;
  if (!isAuthenticated) return <Navigate to="/login" replace />;

  return canViewEmployees(currentUser) ? <>{children}</> : <Navigate to={homeFor(currentUser)} replace />;
};

/** The org-wide Dashboard and Reports, which need `time_entries:view_all`. */
const OrgWideRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, currentUser, isLoading } = useAuth();

  if (isLoading) return <LoadingScreen label="Loading session..." />;
  if (!isAuthenticated) return <Navigate to="/login" replace />;

  return canViewAllTime(currentUser) ? <>{children}</> : <Navigate to="/member/dashboard" replace />;
};

/**
 * The member screens.
 *
 * A user who can see everyone's time is sent to the org-wide dashboard rather
 * than shown a member's: these pages are answered by the same endpoints, and
 * for them `member_id` is *not* pinned — so "My Dashboard" would quietly show
 * the whole organization's numbers under a personal heading.
 */
const MemberRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, currentUser, isLoading } = useAuth();

  if (isLoading) return <LoadingScreen label="Loading session..." />;
  if (!isAuthenticated) return <Navigate to="/login" replace />;

  return canViewAllTime(currentUser) ? <Navigate to="/dashboard" replace /> : <>{children}</>;
};

/**
 * The organization-wide feedback list.
 *
 * The odd one out: it is gated on the *role*, not a permission, because that is
 * what the backend checks for `GET /feedback` (see `features/auth/roles.ts`).
 * A user without one of those roles is sent home rather than to a page whose
 * only request would come back 403 — their own feedback lives at
 * `/member/feedback`.
 */
const FeedbackAdminRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, currentUser, isLoading } = useAuth();

  if (isLoading) return <LoadingScreen label="Loading session..." />;
  if (!isAuthenticated) return <Navigate to="/login" replace />;

  return canViewAllFeedback(currentUser) ? <>{children}</> : <Navigate to={homeFor(currentUser)} replace />;
};

/**
 * Admin Settings → System: the maintenance notice switch.
 *
 * Gated on the administrator role, because that is what the backend checks
 * for `PUT /system/maintenance-mode` (see `features/auth/roles.ts`). Anyone
 * else is sent home rather than to a page whose only action would be refused.
 */
const SystemAdminRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, currentUser, isLoading } = useAuth();

  if (isLoading) return <LoadingScreen label="Loading session..." />;
  if (!isAuthenticated) return <Navigate to="/login" replace />;

  return canManageSystem(currentUser) ? <>{children}</> : <Navigate to={homeFor(currentUser)} replace />;
};

const AppRoutes: React.FC = () => {
  const { isAuthenticated, currentUser, isLoading } = useAuth();

  if (isLoading) return <LoadingScreen label="Loading authentication..." />;

  const home = homeFor(currentUser);

  return (
    <Routes>
      <Route
        path="/login"
        element={isAuthenticated ? <Navigate to={home} replace /> : <LoginScreen />}
      />

      {/* Public, and deliberately not redirected when signed in: someone
          installing Monitra for the first time has no account yet, and asking
          them to sign in before they can download the thing they sign in with
          is a loop. */}
      <Route path="/download" element={<DownloadPage />} />

      {/* ------------------------------------------------------ admin */}
      <Route
        path="/admin/project-management"
        element={
          <AdminRoute>
            <AdminProjectManagement />
          </AdminRoute>
        }
      />
      <Route
        path="/admin/task-listing"
        element={
          <AdminRoute>
            <AdminTaskListing />
          </AdminRoute>
        }
      />
      <Route
        path="/admin/members"
        element={
          <DirectoryRoute>
            <AdminMembers />
          </DirectoryRoute>
        }
      />
      <Route
        path="/admin/time-tracking"
        element={
          <DirectoryRoute>
            <AdminTimeTracking />
          </DirectoryRoute>
        }
      />
      <Route
        path="/admin/screenshots"
        element={
          <DirectoryRoute>
            <AdminScreenshots />
          </DirectoryRoute>
        }
      />
      <Route
        path="/admin/teams"
        element={
          <DirectoryRoute>
            <AdminTeams />
          </DirectoryRoute>
        }
      />
      <Route
        path="/admin/teams/:leaderId"
        element={
          <DirectoryRoute>
            <AdminTeams />
          </DirectoryRoute>
        }
      />
      <Route
        path="/admin/teams/:leaderId/:projectId"
        element={
          <DirectoryRoute>
            <AdminTeams />
          </DirectoryRoute>
        }
      />
      <Route
        path="/admin/feedback"
        element={
          <FeedbackAdminRoute>
            <AdminFeedback />
          </FeedbackAdminRoute>
        }
      />
      <Route
        path="/admin/settings"
        element={<Navigate to="/admin/settings/maintenance" replace />}
      />
      <Route
        path="/admin/settings/maintenance"
        element={
          <SystemAdminRoute>
            <AdminSettings />
          </SystemAdminRoute>
        }
      />
      <Route
        path="/admin/settings/user-management"
        element={
          <SystemAdminRoute>
            <AdminUserManagement />
          </SystemAdminRoute>
        }
      />
      <Route
        path="/admin/settings/screenshot-privacy"
        element={
          <SystemAdminRoute>
            <AdminScreenshotPrivacy />
          </SystemAdminRoute>
        }
      />
      <Route
        path="/dashboard"
        element={
          <OrgWideRoute>
            <DashboardV2 />
          </OrgWideRoute>
        }
      />
      <Route
        path="/dashboard/reports/:reportId"
        element={
          <OrgWideRoute>
            <ReportPage />
          </OrgWideRoute>
        }
      />

      {/* ----------------------------------------------------- member */}
      <Route
        path="/member/dashboard"
        element={
          <MemberRoute>
            <MemberDashboard />
          </MemberRoute>
        }
      />
      <Route
        path="/member/reports"
        element={<Navigate to="/member/reports/projects" replace />}
      />
      <Route
        path="/member/reports/:reportId"
        element={
          <MemberRoute>
            <MemberReports />
          </MemberRoute>
        }
      />
      <Route
        path="/member/projects"
        element={
          <MemberRoute>
            <MemberProjects />
          </MemberRoute>
        }
      />
      <Route
        path="/member/tasks"
        element={
          <MemberRoute>
            <MemberTasks />
          </MemberRoute>
        }
      />
      <Route
        path="/member/time-tracking"
        element={
          <MemberRoute>
            <MemberTimeTracking />
          </MemberRoute>
        }
      />
      <Route
        path="/member/screenshots"
        element={
          <MemberRoute>
            <MemberScreenshots />
          </MemberRoute>
        }
      />
      <Route
        path="/member/team"
        element={
          <MemberRoute>
            <MemberTeam />
          </MemberRoute>
        }
      />
      <Route
        path="/member/feedback"
        element={
          <MemberRoute>
            <MemberFeedback />
          </MemberRoute>
        }
      />
      <Route
        path="/member/team/:projectId"
        element={
          <MemberRoute>
            <MemberTeam />
          </MemberRoute>
        }
      />

      <Route path="*" element={<Navigate to={isAuthenticated ? home : "/login"} replace />} />
    </Routes>
  );
};

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppRoutes />
        {/* The maintenance notice: one card for the signed-in session, over
            every route, and never in the way of any of them. */}
        <MaintenanceToast />
      </BrowserRouter>
    </AuthProvider>
  )
}

export default App
