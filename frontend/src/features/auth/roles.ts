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
 * Who may look at *other people's* screenshots.
 *
 * Screenshots are the most invasive thing this product records, so the audience
 * for someone else's is deliberately narrower than for their time: Admin and HR
 * only. A leader holds `view_employees` and `time_entries:view_all` — enough for
 * the roster, the dashboard and their team's timesheets — but not this. A leader
 * and an employee both see exactly one person's captures: their own.
 *
 * This is a *role* list rather than a permission because the backend has no
 * separate screenshot permission to mirror: `TimeEntryScreenshotService`
 * authorises reads through the general member scope, which would let a leader
 * through. The gate therefore lives here, and a leader's own screenshots come
 * from the endpoint's caller-pinned default rather than from a `user_id`.
 */
const SCREENSHOT_VIEW_ALL_ROLES = new Set([
  "administrator",
  "org_admin",
  "super_admin",
  "hr",
]);

/** True for Admin and HR — the only roles shown every member's screenshots. */
export const canViewAllScreenshots = (user: UserRead | null) =>
  SCREENSHOT_VIEW_ALL_ROLES.has((user?.role_name || "").trim().toLowerCase());

/**
 * Who may destroy a screenshot.
 *
 * Unlike `canViewAllScreenshots`, this one has a real backend permission to
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
