/**
 * What a failed query means, in one place.
 *
 * Every screen used to say "could not be loaded, please try again" whatever had
 * happened, so a session that had ended, a server that was restarting and a
 * request that timed out were indistinguishable -- to the user and in any
 * report. The user-facing sentence stays short; the *kind* is what decides
 * whether to offer a sign-in, a retry, or nothing.
 */
export type QueryErrorKind =
  | "auth"       // 401/403: the session is over or the account may not do this
  | "network"    // the request never got an answer (offline, DNS, reset, CORS-less 5xx)
  | "timeout"    // it got no answer in time
  | "server"     // 5xx / 429: the server answered "not now"
  | "client";    // any other 4xx, or a malformed answer

export interface QueryErrorInfo {
  kind: QueryErrorKind;
  /** Worth offering Retry; may recover by itself. */
  transient: boolean;
  /** HTTP status when there was one, else the RTK Query error code. */
  code: string;
  /** One calm sentence. */
  message: string;
}

const statusOf = (error: unknown): number | string | undefined => {
  if (error && typeof error === "object" && "status" in error) {
    return (error as { status: number | string }).status;
  }
  return undefined;
};

export const describeQueryError = (error: unknown): QueryErrorInfo => {
  const status = statusOf(error);
  if (status === 401 || status === 403) {
    return {
      kind: "auth", transient: false, code: String(status),
      message: "Your session has ended. Please sign in again.",
    };
  }
  if (status === "TIMEOUT_ERROR") {
    return {
      kind: "timeout", transient: true, code: "TIMEOUT_ERROR",
      message: "The server is taking too long to answer.",
    };
  }
  if (status === "FETCH_ERROR" || status === undefined) {
    return {
      kind: "network", transient: true, code: String(status ?? "UNKNOWN"),
      message: "Connection temporarily unavailable.",
    };
  }
  if (typeof status === "number" && (status >= 500 || status === 429 || status === 408)) {
    return {
      kind: "server", transient: true, code: String(status),
      message: "The service is temporarily unavailable.",
    };
  }
  return {
    kind: "client", transient: false, code: String(status),
    message: "This could not be loaded.",
  };
};
