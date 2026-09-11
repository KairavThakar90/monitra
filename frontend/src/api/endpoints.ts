import { API_BASE_URL } from './utils';

export { API_BASE_URL };

/**
 * The performance portal's own login endpoint.
 *
 * This is the only request the web client sends to that host, and it is only
 * ever a sign-in: the portal holds the credentials, so it is the one system
 * that can check them. Everything else in this file goes to our own backend.
 *
 * What the portal returns is *its* JWT, which is not a Monitra credential. It
 * is exchanged at `AUTH.SSO_TOKEN`, where the backend re-verifies it with the
 * portal before issuing the Monitra session the rest of the app needs. Never
 * send a portal token to any other endpoint, and never treat one as a session.
 */
export const AUTH_PROVIDER_LOGIN_URL =
  (import.meta.env.VITE_AUTH_PROVIDER_LOGIN_URL as string | undefined)?.trim() ||
  "https://nothing.peakworkos.com/wp-json/st-performance/v1/auth/hubstaff/login";

/**
 * Every backend URL the web client talks to lives here.
 * Nothing else in the app builds an API URL by hand — if you need a new call,
 * add it to this map first and reference it from the RTK Query slice.
 */
export const ENDPOINTS = {
  AUTH: {
    LOGIN: `${API_BASE_URL}/auth/login`,
    SSO_TOKEN: `${API_BASE_URL}/auth/sso/token`,
    REFRESH: `${API_BASE_URL}/auth/refresh`,
    LOGOUT: `${API_BASE_URL}/auth/logout`,
    ME: `${API_BASE_URL}/auth/me`,
  },
  MEMBERS: {
    GET_ALL: `${API_BASE_URL}/members`,
    GET_BY_ID: (id: string | number) => `${API_BASE_URL}/members/${id}`,
    CREATE: `${API_BASE_URL}/members`,
    UPDATE: (id: string | number) => `${API_BASE_URL}/members/${id}`,
    DELETE: (id: string | number) => `${API_BASE_URL}/members/${id}`,
  },
  PROJECTS: {
    BASE: `${API_BASE_URL}/projects`,
    METADATA: `${API_BASE_URL}/project-management/metadata`,
    GET_ALL: `${API_BASE_URL}/projects`,
    GET_BY_ID: (id: string | number) => `${API_BASE_URL}/projects/${id}`,
    CREATE: `${API_BASE_URL}/projects`,
    UPDATE: (id: string | number) => `${API_BASE_URL}/projects/${id}`,
    DELETE: (id: string | number) => `${API_BASE_URL}/projects/${id}`,
    ASSIGNABLE_LEADERS: `${API_BASE_URL}/projects/assignable-leaders`,
    ASSIGNABLE_EMPLOYEES: `${API_BASE_URL}/projects/assignable-employees`,
    TASKS: (projectId: string | number) => `${API_BASE_URL}/projects/${projectId}/tasks`,
    TASK_BY_ID: (projectId: string | number, taskId: string | number) =>
      `${API_BASE_URL}/projects/${projectId}/tasks/${taskId}`,
  },
  TEAMS: {
    SUMMARY: `${API_BASE_URL}/teams/summary`,
    LEADERS: `${API_BASE_URL}/teams/leaders`,
    LEADER_BY_ID: (id: string | number) => `${API_BASE_URL}/teams/leaders/${id}`,
    LEADER_PROJECTS: (id: string | number) => `${API_BASE_URL}/teams/leaders/${id}/projects`,
    PROJECT_BY_ID: (id: string | number) => `${API_BASE_URL}/teams/projects/${id}`,
    PROJECT_MEMBER: (projectId: string | number, memberId: string | number) => `${API_BASE_URL}/teams/projects/${projectId}/members/${memberId}`,
  },
  TIME_ENTRY_SCREENSHOTS: {
    BASE: `${API_BASE_URL}/time-entry-screenshots`,
    // One IST day grouped into the fixed windows the desktop captures against.
    TIMELINE: `${API_BASE_URL}/time-entry-screenshots/timeline`,
    // The same day for every member the caller may see, in one request.
    DAY: `${API_BASE_URL}/time-entry-screenshots/day`,
    // Streams the image bytes behind the same permission check as the listing.
    // The backend returns `view_url` as a root-relative path with no version
    // prefix, so the client rebuilds it from the id rather than using it raw.
    VIEW: (id: string | number) => `${API_BASE_URL}/time-entry-screenshots/${id}/view`,
    // Permanently destroys the image and its row. The backend admits only
    // callers holding `screenshots:delete` (Administrator and HR); every other role is
    // refused with 403 whatever the UI chose to show them.
    DELETE: (id: string | number) => `${API_BASE_URL}/time-entry-screenshots/${id}`,
  },
  TIME_TRACKING: {
    GET_ALL: `${API_BASE_URL}/time-tracking`,
    GET_BY_EMPLOYEE: (id: string | number) => `${API_BASE_URL}/time-tracking/${id}`,
  },
  MANUAL_TIME_ENTRIES: {
    BASE: `${API_BASE_URL}/manual-time-entries`,
  },
  MANUAL_TIME_ENTRY_REQUESTS: {
    BASE: `${API_BASE_URL}/manual-time-entry-requests`,
    APPROVE: (id: string | number) => `${API_BASE_URL}/manual-time-entry-requests/${id}/approve`,
    REJECT: (id: string | number) => `${API_BASE_URL}/manual-time-entry-requests/${id}/reject`,
    DELETE: (id: string | number) => `${API_BASE_URL}/manual-time-entry-requests/${id}`,
    EDIT: (id: string | number) => `${API_BASE_URL}/manual-time-entry-requests/${id}`,
  },
  REPORTS: {
    BASE: `${API_BASE_URL}/reports`,
    DETAILED_LOGS: `${API_BASE_URL}/reports/detailed-logs`,
  },
  // Feedback & Help. Submission happens in the desktop client; the dashboard
  // only reads. `MY` is scoped to the caller by the token, and `BASE` is the
  // organization-wide list the backend restricts to Admin, HR and Leader.
  FEEDBACK: {
    BASE: `${API_BASE_URL}/feedback`,
    MY: `${API_BASE_URL}/feedback/my`,
    MY_BY_ID: (id: string | number) => `${API_BASE_URL}/feedback/my/${id}`,
    GET_BY_ID: (id: string | number) => `${API_BASE_URL}/feedback/${id}`,
  },
  // Desktop downloads. Unauthenticated: someone installing Monitra for the
  // first time has no account yet. These are asked for "the latest", never for
  // a versioned filename, which is what keeps the download button current
  // without the site being redeployed for every release.
  DESKTOP: {
    DOWNLOADS: `${API_BASE_URL}/desktop/releases/downloads`,
    LATEST: (platform: string, arch?: string) =>
      `${API_BASE_URL}/desktop/releases/latest?platform=${encodeURIComponent(platform)}` +
      (arch ? `&arch=${encodeURIComponent(arch)}` : ''),
    // A plain link a download button can point at with no JavaScript. The
    // backend redirects to whatever the current artifact is.
    DOWNLOAD: (platform: string, arch?: string) =>
      `${API_BASE_URL}/desktop/releases/download?platform=${encodeURIComponent(platform)}` +
      (arch ? `&arch=${encodeURIComponent(arch)}` : ''),
  },
  REACT_DASHBOARD: {
    BASE: `${API_BASE_URL}/react/dashboard`,
    PROJECTS: `${API_BASE_URL}/react/dashboard/projects`,
    MEMBERS: `${API_BASE_URL}/react/dashboard/members`,
    APPS: `${API_BASE_URL}/react/dashboard/apps`,
  },
};
