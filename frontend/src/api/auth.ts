import { AUTH_PROVIDER_LOGIN_URL, ENDPOINTS } from "./endpoints";

export interface DevLoginPayload {
  email: string;
  password: string;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  user: UserRead;
  session_created_at?: string;
  session_expires_at?: string;
}

/** What the portal returns when credentials are accepted. */
interface ProviderLoginResponse {
  status?: string;
  message?: string;
  access_token?: string;
}

/**
 * Sign in.
 *
 * The credentials go straight to the performance portal, which is the system
 * that holds them -- this is the web client's only call to that host, and it is
 * only ever this one. The portal answers with its own JWT.
 *
 * That JWT is deliberately not treated as a session. It is handed to our
 * backend, which re-verifies it with the portal and reads the identity behind
 * it before issuing the Monitra token every other screen requires. So a token
 * minted by anything other than the portal is refused server-side, and the
 * browser never decides for itself who signed in.
 */
export async function loginAPI(payload: DevLoginPayload): Promise<TokenPair> {
  let response: Response;
  try {
    response = await fetch(AUTH_PROVIDER_LOGIN_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        username: payload.email,
        password: payload.password,
      }),
    });
  } catch {
    // A network-level failure here is indistinguishable from the portal being
    // down, so say what the user can act on rather than guessing which it was.
    throw new Error(
      "Could not reach the sign-in service. Check your connection and try again."
    );
  }

  let data: ProviderLoginResponse | null = null;
  try {
    data = (await response.json()) as ProviderLoginResponse;
  } catch {
    // A non-JSON body means something other than the portal answered -- a WAF
    // or a proxy error page. Never surface that markup to the user.
    data = null;
  }

  if (!response.ok || data?.status === "failed") {
    if (response.status === 401 || response.status === 403) {
      throw new Error(data?.message || "Invalid email or password");
    }
    throw new Error(
      data?.message || "The sign-in service is unavailable. Please try again shortly."
    );
  }

  if (!data?.access_token) {
    // A 200 with no token is a contract break, not a credential problem. Saying
    // "invalid password" here would send the user to reset a working one.
    throw new Error("The sign-in service returned an unexpected response.");
  }

  return ssoLoginAPI(
    data.access_token,
    "Signed in, but this account could not be opened. Please try again."
  );
}

/**
 * Exchange the performance portal's own JWT for a Monitra session. The portal
 * token is only ever sent to our backend, which verifies it with the portal
 * before issuing anything.
 *
 * Two callers reach this: a `?token=...` handoff arriving from the portal, and
 * `loginAPI` straight after the sign-in form got a token from the portal. They
 * need different wording when it fails -- telling someone who just typed their
 * password that "the link expired" describes a link they never used -- so the
 * fallback is the caller's to supply.
 */
export async function ssoLoginAPI(
  token: string,
  fallbackMessage = "This sign-in link is no longer valid. Please sign in below."
): Promise<TokenPair> {
  const response = await fetch(ENDPOINTS.AUTH.SSO_TOKEN, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ token }),
  });

  if (!response.ok) {
    let errorDetail = fallbackMessage;
    try {
      const errorData = await response.json();
      if (errorData.detail && typeof errorData.detail === "object" && errorData.detail.message) {
        errorDetail = errorData.detail.message;
      } else if (errorData.detail) {
        errorDetail = errorData.detail;
      }
    } catch {
      // Ignore - the default message already explains what the user should do.
    }
    throw new Error(errorDetail);
  }

  const data = (await response.json()) as TokenPair;
  return { ...data, user: normalizeUserProfile(data.user) };
}

export async function refreshSessionAPI(refreshToken: string): Promise<TokenPair> {
  const response = await fetch(ENDPOINTS.AUTH.REFRESH, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });

  if (!response.ok) {
    throw new Error("Session expired");
  }

  const data = (await response.json()) as TokenPair;
  return { ...data, user: normalizeUserProfile(data.user) };
}

/**
 * Sign a client in immediately from their email address alone -- no link to
 * click, no second factor. Deliberately the weakest credential in this
 * system, scoped to `client`-role accounts only; see `AuthService.client_direct_login`
 * on the backend for the reasoning.
 *
 * Throws with the backend's own explanation when the address does not match
 * an active client account ("you are not registered as a client...").
 */
export async function clientDirectLoginAPI(email: string): Promise<TokenPair> {
  const response = await fetch(ENDPOINTS.AUTH.CLIENT_LOGIN, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });

  if (!response.ok) {
    let message = "Sorry, you are not registered as a client. Please contact your admin.";
    try {
      const data = await response.json();
      if (typeof data?.detail === "string") message = data.detail;
    } catch {
      // The default message already explains what to do.
    }
    throw new Error(message);
  }

  const data = (await response.json()) as TokenPair;
  return { ...data, user: normalizeUserProfile(data.user) };
}

/**
 * Ask the backend to email a fresh passwordless sign-in link to a client
 * account. A client has no password, so this is the whole of a returning
 * client's sign-in: enter the email, nothing else.
 *
 * Throws when the address does not match an active client account, with the
 * backend's own explanation ("you are not registered as a client...") as the
 * message -- a deliberate product choice to tell the visitor plainly rather
 * than pretending a link was sent either way.
 */
export async function requestClientLoginLinkAPI(email: string): Promise<void> {
  const response = await fetch(ENDPOINTS.AUTH.CLIENT_LOGIN_LINK, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });

  if (!response.ok) {
    let message = "Sorry, you are not registered as a client. Please contact your admin.";
    try {
      const data = await response.json();
      if (typeof data?.detail === "string") message = data.detail;
    } catch {
      // The default message already explains what to do.
    }
    throw new Error(message);
  }
}

export async function logoutAPI(refreshToken: string | null): Promise<void> {
  if (!refreshToken) return;
  await fetch(ENDPOINTS.AUTH.LOGOUT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
}

export interface UserRead {
  id: number;
  organization_id: number;
  username: string;
  email: string;
  name: string;
  designation?: string | null;
  role_name: string;
  permissions: Record<string, boolean>;
  is_active: boolean;
}

/** Keep profiles from older deployments compatible with the canonical role name. */
const normalizeUserProfile = (user: UserRead): UserRead => ({
  ...user,
  role_name: user.role_name.trim().toLowerCase() === "admin" ? "administrator" : user.role_name,
});

export async function getMeAPI(token: string): Promise<UserRead> {
  const response = await fetch(ENDPOINTS.AUTH.ME, {
    method: "GET",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
  });

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Unauthorized");
    }
    throw new Error("Failed to fetch user profile");
  }

  return normalizeUserProfile((await response.json()) as UserRead);
}
