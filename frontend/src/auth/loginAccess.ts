/**
 * The Members directory's Allow / Exclude switch for signing in.
 *
 * When an administrator excludes a member, the backend stops their running
 * timer, revokes their sessions, and answers every request with 401 and a
 * structured `{ detail: { code: "login_disabled", message, note } }` (see
 * backend `app/core/login_access.py`); a fresh sign-in gets 403 with the same
 * detail. This module is how the web client tells that apart from an ordinary
 * expiry, so the sign-in screen can say *why* instead of "session expired".
 */

export const LOGIN_DISABLED_CODE = "login_disabled";

export const LOGIN_DISABLED_MESSAGE =
  "You are not allowed to log in yet. Once an administrator allows you, you can log in again.";

export const LOGIN_DISABLED_NOTE =
  "If you are continuously unable to log in, please contact your administrator.";

/** Thrown by the auth API functions when the backend refuses this account. */
export class LoginDisabledError extends Error {
  readonly note = LOGIN_DISABLED_NOTE;

  constructor() {
    super(LOGIN_DISABLED_MESSAGE);
    this.name = "LoginDisabledError";
  }
}

export const isLoginDisabledError = (err: unknown): err is LoginDisabledError =>
  err instanceof LoginDisabledError || (err as { name?: string } | null)?.name === "LoginDisabledError";

/** Whether a parsed error body (or RTK Query `error.data`) is the refusal. */
export const isLoginDisabledBody = (body: unknown): boolean => {
  const detail = (body as { detail?: unknown } | null | undefined)?.detail;
  return !!detail && typeof detail === "object" && (detail as { code?: unknown }).code === LOGIN_DISABLED_CODE;
};

const NOTICE_KEY = "monitra.auth.notice";

/**
 * Remember, across the redirect to /login, that the session ended because the
 * account was excluded. sessionStorage: it belongs to this tab's sign-out.
 */
export const markLoginDisabled = () => {
  try {
    sessionStorage.setItem(NOTICE_KEY, LOGIN_DISABLED_CODE);
  } catch {
    /* storage unavailable - the sign-in attempt will still say why */
  }
};

/** Whether the last sign-out was an exclusion. Read-only, so it is safe in a
 * state initializer that React may run twice. */
export const peekLoginDisabledNotice = (): boolean => {
  try {
    return sessionStorage.getItem(NOTICE_KEY) === LOGIN_DISABLED_CODE;
  } catch {
    return false;
  }
};

/** Forget the notice once the sign-in screen has shown it. */
export const clearLoginDisabledNotice = () => {
  try {
    sessionStorage.removeItem(NOTICE_KEY);
  } catch {
    /* nothing to clear */
  }
};
