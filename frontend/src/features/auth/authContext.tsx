import React, { createContext, useContext, useState, useEffect } from "react";
import type { ReactNode } from "react";
import { loginAPI, getMeAPI, logoutAPI, refreshSessionAPI, ssoLoginAPI } from "../../api/auth";
import { clearSessionStorage, ensureSessionExpiry, getSessionExpiresAt, storeSessionTokens } from "../../auth/session";
import { store } from "../../store";
import { baseApi } from "../../store/api/baseApi";
import { clearPersistedApiCache } from "../../store/persist";
import type { UserRead } from "../../api/auth";

interface AuthContextType {
  accessToken: string | null;
  refreshToken: string | null;
  isAuthenticated: boolean;
  currentUser: UserRead | null;
  isLoading: boolean;
  /** Set when a `?token=...` handoff was present but could not be exchanged. */
  ssoError: string | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

const PROFILE_KEY = "monitra.session.user.v1";

/**
 * The profile from the last verified session.
 *
 * Keeping it lets the app render its real screens on the first frame after a
 * refresh instead of holding everything behind a round trip to /auth/me. It is
 * a rendering convenience only - it grants nothing. Every request still carries
 * the token and the backend still authorises it, and the check below revokes
 * the session the moment the server disagrees.
 */
/**
 * Take the single sign-on token out of the address bar.
 *
 * The performance portal links users here as `/?token=<portal jwt>`. The token
 * is a live credential, so it is read once and immediately removed from the URL
 * - a URL gets bookmarked, pasted and kept in history, and the token should not
 * travel with it. Removing it also stops a reload from replaying the exchange.
 */
const consumeSsoToken = (): string | null => {
  try {
    const params = new URLSearchParams(window.location.search);
    const token = params.get("token");
    if (!token) return null;

    params.delete("token");
    const query = params.toString();
    window.history.replaceState(
      {},
      document.title,
      `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`
    );
    return token;
  } catch {
    return null;
  }
};

const readCachedProfile = (): UserRead | null => {
  try {
    const raw = localStorage.getItem(PROFILE_KEY);
    if (!raw) return null;
    const cached = JSON.parse(raw) as { token?: string; user?: UserRead };
    // Bound to the token it was fetched with, so a different session never
    // renders as the previous user even for a frame.
    if (!cached?.user || cached.token !== localStorage.getItem("accessToken")) return null;
    return cached.user;
  } catch {
    return null;
  }
};

const writeCachedProfile = (user: UserRead | null) => {
  try {
    if (user) {
      localStorage.setItem(PROFILE_KEY, JSON.stringify({ token: localStorage.getItem("accessToken"), user }));
    } else {
      localStorage.removeItem(PROFILE_KEY);
    }
  } catch {
    /* storage unavailable - the app just starts cold next time */
  }
};

export const AuthProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  // Seed straight from storage so the first render already knows who is signed
  // in. `getMeAPI` below still runs and still has the last word.
  const restored = localStorage.getItem("accessToken") && localStorage.getItem("refreshToken")
    ? readCachedProfile()
    : null;

  const [accessToken, setAccessToken] = useState<string | null>(restored ? localStorage.getItem("accessToken") : null);
  const [refreshToken, setRefreshToken] = useState<string | null>(restored ? localStorage.getItem("refreshToken") : null);
  const [currentUser, setCurrentUser] = useState<UserRead | null>(restored);
  const [isLoading, setIsLoading] = useState<boolean>(!restored);
  const [ssoError, setSsoError] = useState<string | null>(null);

  // Read before the first effect runs so a reload cannot replay the exchange.
  const [ssoToken] = useState<string | null>(consumeSsoToken);

  const applySession = (response: { access_token: string; refresh_token: string; user: UserRead }) => {
    storeSessionTokens(response);
    // A previous account's cache must never leak into this session.
    store.dispatch(baseApi.util.resetApiState());
    clearPersistedApiCache();

    setAccessToken(response.access_token);
    setRefreshToken(response.refresh_token);
    setCurrentUser(response.user);
    writeCachedProfile(response.user);
  };

  // Restore authentication from localStorage on application mount
  useEffect(() => {
    const handleSessionExpired = () => {
      setAccessToken(null);
      setRefreshToken(null);
      setCurrentUser(null);
      setIsLoading(false);
      writeCachedProfile(null);
      clearPersistedApiCache();
      store.dispatch(baseApi.util.resetApiState());
    };

    window.addEventListener("auth:session-expired", handleSessionExpired);

    const restoreAuth = async () => {
      // A portal handoff wins over whatever session is already stored: the link
      // says who is arriving, and it may not be the account cached in this
      // browser. Once it succeeds the app is authenticated and the router sends
      // it straight to the dashboard.
      if (ssoToken) {
        try {
          applySession(await ssoLoginAPI(ssoToken));
          setIsLoading(false);
          return;
        } catch (err) {
          console.error("Single sign-on handoff failed:", err);
          setSsoError(err instanceof Error ? err.message : "This sign-in link is no longer valid.");
          // Fall through: any existing local session is still valid, and if
          // there is none the user lands on the login screen with the reason.
        }
      }

      const storedAccess = localStorage.getItem("accessToken");
      const storedRefresh = localStorage.getItem("refreshToken");

      if (storedAccess && storedRefresh) {
        ensureSessionExpiry();
        try {
          const sessionExpiresAt = getSessionExpiresAt();
          if (sessionExpiresAt !== null && sessionExpiresAt <= Date.now()) {
            throw new Error("Session expired");
          }

          // Verify the token and pick up any profile change. When we already
          // restored from cache this runs behind the rendered UI; otherwise the
          // app waits on it exactly as before.
          try {
            const user = await getMeAPI(storedAccess);
            setAccessToken(storedAccess);
            setRefreshToken(storedRefresh);
            setCurrentUser(user);
            writeCachedProfile(user);
          } catch {
            const refreshed = await refreshSessionAPI(storedRefresh);
            storeSessionTokens(refreshed, true);
            setAccessToken(refreshed.access_token);
            setRefreshToken(refreshed.refresh_token);
            setCurrentUser(refreshed.user);
            writeCachedProfile(refreshed.user);
          }
        } catch (err) {
          console.error("Failed to restore session, clearing invalid tokens:", err);
          clearSessionStorage();
          writeCachedProfile(null);
          clearPersistedApiCache();
          store.dispatch(baseApi.util.resetApiState());
          setAccessToken(null);
          setRefreshToken(null);
          setCurrentUser(null);
        }
      } else {
        writeCachedProfile(null);
      }
      setIsLoading(false);
    };

    restoreAuth();
    return () => window.removeEventListener("auth:session-expired", handleSessionExpired);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const login = async (email: string, password: string) => {
    const response = await loginAPI({ email, password });
    applySession(response);
  };

  const logout = () => {
    const storedRefresh = localStorage.getItem("refreshToken");
    void logoutAPI(storedRefresh).catch(() => undefined);
    clearSessionStorage();
    // Cached responses belong to the account that fetched them - drop both the
    // in-memory cache and the copy on disk so the next sign-in starts clean.
    store.dispatch(baseApi.util.resetApiState());
    clearPersistedApiCache();
    writeCachedProfile(null);
    setAccessToken(null);
    setRefreshToken(null);
    setCurrentUser(null);
    setSsoError(null);
  };

  const isAuthenticated = !!accessToken && !!currentUser;

  return (
    <AuthContext.Provider
      value={{
        accessToken,
        refreshToken,
        isAuthenticated,
        currentUser,
        isLoading,
        ssoError,
        login,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = (): AuthContextType => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
};
