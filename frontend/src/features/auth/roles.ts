import type { UserRead } from "../../api/auth";

/**
 * Who may read the whole organization's feedback.
 *
 * Almost every gate in this app is a *permission* check, because the backend
 * endpoint behind it is gated on a permission. Feedback is the exception: the
 * backend gates `GET /feedback` on the role name itself
 * (`FEEDBACK_VIEW_ALL_ROLES` in `app/services/feedback.py`), so mirroring that
 * role list is what keeps the sidebar from offering a page the API would
 * answer with 403. The two lists must stay in step — if a role is added to one,
 * add it to the other.
 *
 * `org_admin` / `super_admin` and `project_leader` are the alternate spellings
 * the backend's ROLE_PERMISSIONS table already defines for administrator and
 * leader authority.
 */
const FEEDBACK_VIEW_ALL_ROLES = new Set([
  "administrator",
  "org_admin",
  "super_admin",
  "hr",
  "leader",
  "project_leader",
]);

/** True for Admin, HR and Leader — the roles allowed the org-wide feedback list. */
export const canViewAllFeedback = (user: UserRead | null) =>
  FEEDBACK_VIEW_ALL_ROLES.has((user?.role_name || "").trim().toLowerCase());

/**
 * Who may look at *other people's* screenshots, and how far.
 *
 * Three audiences, and the difference between them is reach, not the screen:
 *
 * - **Admin and HR** see every member's captures.
 * - **A leader** sees their own team's: the people on the projects they lead.
 *   The backend decides who that is — `TimeEntryScreenshotService` reads
 *   through `visible_member_ids`, the same set that scopes a leader's
 *   dashboard, timesheets and logs — so this page sends no list of people and
 *   a leader cannot ask for someone off their team (403).
 * - **Everyone else** sees exactly one person's captures: their own.
 *
 * These are *role* lists rather than a permission because the backend has no
 * separate screenshot-read permission to mirror; the roles here are the ones
 * whose server-side scope reaches past themselves. A stale copy can only hide
 * the employees view, never widen what the endpoint returns.
 *
 * Seeing is not deleting. A leader sees their team's captures and may not
 * destroy one — see `canDeleteScreenshots`.
 */
const SCREENSHOT_VIEW_ALL_ROLES = new Set([
  "administrator",
  "org_admin",
  "super_admin",
  "hr",
]);

/** Mirrors `TEAM_SCOPED_ROLES` in `backend/app/services/member_scope.py`. */
const SCREENSHOT_VIEW_TEAM_ROLES = new Set(["leader", "project_leader"]);

/** True for Admin and HR — the roles shown every member's screenshots. */
export const canViewAllScreenshots = (user: UserRead | null) =>
  SCREENSHOT_VIEW_ALL_ROLES.has((user?.role_name || "").trim().toLowerCase());

/** True for a leader — shown their own team's screenshots and nobody else's. */
export const canViewTeamScreenshots = (user: UserRead | null) =>
  SCREENSHOT_VIEW_TEAM_ROLES.has((user?.role_name || "").trim().toLowerCase());

/** True for anyone shown screenshots other than their own: Admin, HR, Leader. */
export const canViewOthersScreenshots = (user: UserRead | null) =>
  canViewAllScreenshots(user) || canViewTeamScreenshots(user);

/**
 * Who may destroy a screenshot.
 *
 * Unlike the read helpers above, this one has a real backend permission to
 * mirror: `DELETE /time-entry-screenshots/{id}` is gated on `screenshots:delete`,
 * which `app/core/permissions.py` grants to `administrator`, `org_admin`, `super_admin`
 * and `hr` and to nobody else — deliberately not to a leader or a manager, who
 * may see their team's captures but may not delete them, and not to an employee
 * for their own. So the check reads the permission the user was actually issued
 * rather than a second copy of the role list, and a grant changed on the server
 * reaches this button with no frontend change.
 *
 * Hiding the control is presentation only. The endpoint refuses the request
 * regardless, which is what actually enforces this.
 */
export const canDeleteScreenshots = (user: UserRead | null) =>
  Boolean(user?.permissions?.["screenshots:delete"]);

/**
 * Who may switch the deployment-wide maintenance notice on and off.
 *
 * Mirrors `MAINTENANCE_MANAGE_ROLES` in `backend/app/services/maintenance_mode.py`:
 * the three administrator spellings and nobody else. A role list rather than a
 * permission for the same reason feedback's is: the backend gates
 * `PUT /system/maintenance-mode` on the role name itself, so that a new
 * permission key would not have to wait for every administrator to sign in
 * again before the button worked. The two lists must stay in step.
 *
 * Hiding the page and the sidebar entry is presentation. The endpoint refuses
 * any other caller with 403 regardless of what this returns.
 */
const SYSTEM_MANAGE_ROLES = new Set(["administrator", "org_admin", "super_admin"]);

/** True for administrators — the only role shown the System settings page. */
export const canManageSystem = (user: UserRead | null) =>
  SYSTEM_MANAGE_ROLES.has((user?.role_name || "").trim().toLowerCase());

/**
 * Who may invite clients and manage their project access.
 *
 * A permission, mirroring `clients:manage` in `app/core/permissions.py`
 * (granted to administrator, org_admin and super_admin).
 */
export const canManageClients = (user: UserRead | null) =>
  Boolean(user?.permissions?.["clients:manage"]);

/**
 * Whether this account is a client's — an external party reading a read-only
 * slice of the organization's projects, never an org member. Mirrors
 * `clients:view_shared`, the one permission `ROLE_PERMISSIONS["client"]`
 * grants.
 */
export const isClientAccount = (user: UserRead | null) =>
  Boolean(user?.permissions?.["clients:view_shared"]);
