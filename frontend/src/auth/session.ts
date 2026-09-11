import type { TokenPair } from "../api/auth";

export const SESSION_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
export const SESSION_EXPIRES_AT_KEY = "monitra.session.expiresAt";

export const getSessionExpiresAt = (): number | null => {
  const value = Number(localStorage.getItem(SESSION_EXPIRES_AT_KEY));
  return Number.isFinite(value) && value > 0 ? value : null;
};

export const establishSessionExpiry = (response: Pick<TokenPair, "session_created_at" | "session_expires_at">) => {
  const startedAt = response.session_created_at ? Date.parse(response.session_created_at) : Date.now();
  const clientExpiry = startedAt + SESSION_MAX_AGE_MS;
  const serverExpiry = response.session_expires_at ? Date.parse(response.session_expires_at) : NaN;
  const expiresAt = Number.isFinite(serverExpiry) ? Math.min(clientExpiry, serverExpiry) : clientExpiry;
  localStorage.setItem(SESSION_EXPIRES_AT_KEY, String(expiresAt));
};

export const ensureSessionExpiry = (): number | null => {
  const existing = getSessionExpiresAt();
  if (existing !== null) return existing;

  if (localStorage.getItem("accessToken") && localStorage.getItem("refreshToken")) {
    const expiresAt = Date.now() + SESSION_MAX_AGE_MS;
    localStorage.setItem(SESSION_EXPIRES_AT_KEY, String(expiresAt));
    return expiresAt;
  }

  return null;
};

export const clearSessionStorage = () => {
  localStorage.removeItem("accessToken");
  localStorage.removeItem("refreshToken");
  localStorage.removeItem(SESSION_EXPIRES_AT_KEY);
};

export const storeSessionTokens = (
  response: Pick<TokenPair, "access_token" | "refresh_token" | "session_created_at" | "session_expires_at">,
  preserveExpiry = false
) => {
  localStorage.setItem("accessToken", response.access_token);
  localStorage.setItem("refreshToken", response.refresh_token);
  if (preserveExpiry) {
    ensureSessionExpiry();
  } else {
    establishSessionExpiry(response);
  }
};