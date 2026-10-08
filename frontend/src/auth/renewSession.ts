import { RefreshUnavailableError, SessionEndedError, refreshSessionAPI } from "../api/auth";
import { storeSessionTokens } from "./session";

/**
 * The one way this client renews its session.
 *
 * Refresh tokens are single-use: the server revokes the one it is shown and
 * issues another. That makes *who else is refreshing* the whole problem:
 *
 *  - two queries that meet a 401 together must share one refresh (in a tab:
 *    `inFlight`; across tabs of the same browser: a Web Lock, below);
 *  - a request that was sent with a token somebody has since replaced must not
 *    refresh at all -- it just retries with the token that is there now;
 *  - a refresh the server refuses *because another tab got there first* is not
 *    the end of the session, it is a session that has already been renewed.
 *
 * Before this, the loser of such a race cleared the tokens in localStorage --
 * the winner's too -- and the user was signed out, or stranded on a dashboard
 * that 401'd for ever.
 */
export type RenewOutcome = "renewed" | "adopted";

let inFlight: Promise<RenewOutcome> | null = null;

const LOCK_NAME = "monitra-session-refresh";

const withLock = async <T,>(work: () => Promise<T>): Promise<T> => {
  const locks = typeof navigator !== "undefined" ? (navigator as Navigator & { locks?: LockManager }).locks : undefined;
  if (!locks?.request) return work();
  return locks.request(LOCK_NAME, work) as Promise<T>;
};

const renew = (accessTokenUsed: string | null): Promise<RenewOutcome> =>
  withLock(async () => {
    const current = localStorage.getItem("accessToken");
    // Somebody (another tab, an earlier refresh) already replaced the token
    // this request was sent with: nothing to renew, just use theirs.
    if (accessTokenUsed && current && current !== accessTokenUsed) return "adopted";

    const refreshToken = localStorage.getItem("refreshToken");
    if (!refreshToken) throw new SessionEndedError("No refresh token");

    try {
      const session = await refreshSessionAPI(refreshToken);
      storeSessionTokens(session, true);
      return "renewed" as const;
    } catch (error) {
      if (error instanceof SessionEndedError) {
        // Refused -- but if the stored token has changed while we waited, it
        // was another tab that spent the one we presented, and it succeeded.
        const nowRefresh = localStorage.getItem("refreshToken");
        const nowAccess = localStorage.getItem("accessToken");
        if (nowRefresh && nowAccess && nowRefresh !== refreshToken) return "adopted" as const;
      }
      throw error;
    }
  });

/**
 * Renew the session, once, for everyone who asks while it is in progress.
 * `accessTokenUsed` is the token the failing request carried.
 *
 * Rejects with `SessionEndedError` / `LoginDisabledError` (the session is over)
 * or `RefreshUnavailableError` (it could not be asked; nothing is over).
 */
export const renewSession = (accessTokenUsed: string | null): Promise<RenewOutcome> => {
  inFlight ??= renew(accessTokenUsed).finally(() => {
    inFlight = null;
  });
  return inFlight;
};

export { RefreshUnavailableError, SessionEndedError };
