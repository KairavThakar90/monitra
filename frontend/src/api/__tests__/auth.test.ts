/**
 * Tests for sign-in.
 *
 * Sign-in is a two-hop flow and each hop has a property that is easy to break
 * silently:
 *
 * 1. **The credentials go to the portal, and the portal token goes to our
 *    backend — never the other way round.** Sending a password to our own
 *    backend, or treating the portal's JWT as a Monitra session, would both
 *    still "work" on the happy path while changing who is trusted to say who
 *    the user is. These tests pin the order and the destinations.
 * 2. **A failure must not be misreported as a bad password.** An outage or a
 *    contract break that surfaces as "invalid email or password" sends people
 *    to reset a credential that was never wrong.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { loginAPI } from '../auth';
import { AUTH_PROVIDER_LOGIN_URL, ENDPOINTS } from '../endpoints';

/** A Monitra token pair, as `/auth/sso/token` returns it. */
const SESSION = {
  access_token: 'monitra-access',
  refresh_token: 'monitra-refresh',
  token_type: 'Bearer',
  // Realistic shape: the backend's UserRead always carries role_name, and
  // loginAPI normalises the profile on the way through.
  user: { id: 172, email: 'someone@example.com', role_name: 'employee' },
};

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('loginAPI', () => {
  it('sends the credentials to the portal and the portal token to our backend', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, { status: 'success', access_token: 'portal-jwt' }))
      .mockResolvedValueOnce(jsonResponse(200, SESSION));
    vi.stubGlobal('fetch', fetchMock);

    const result = await loginAPI({ email: 'someone@example.com', password: 'pw' });

    expect(result).toEqual(SESSION);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // Hop 1: the password goes to the portal and nowhere else.
    const [providerUrl, providerInit] = fetchMock.mock.calls[0];
    expect(providerUrl).toBe(AUTH_PROVIDER_LOGIN_URL);
    expect(JSON.parse(providerInit.body)).toEqual({
      username: 'someone@example.com',
      password: 'pw',
    });

    // Hop 2: only the portal's token reaches our backend -- never the password.
    const [exchangeUrl, exchangeInit] = fetchMock.mock.calls[1];
    expect(exchangeUrl).toBe(ENDPOINTS.AUTH.SSO_TOKEN);
    expect(JSON.parse(exchangeInit.body)).toEqual({ token: 'portal-jwt' });
    expect(exchangeInit.body).not.toContain('pw');
  });

  it('reports a rejected password without ever calling the exchange', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(
      jsonResponse(401, {
        status: 'failed',
        message: 'The username or password you entered is incorrect.',
      })
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(loginAPI({ email: 'someone@example.com', password: 'wrong' })).rejects.toThrow(
      'The username or password you entered is incorrect.'
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not blame the password when the portal is unavailable', async () => {
    // A WAF or proxy answers with markup, not JSON. The user must not be told
    // their credentials are wrong, and the markup must never be shown to them.
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 502,
      json: async () => {
        throw new Error('not json');
      },
    } as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);

    await expect(loginAPI({ email: 'someone@example.com', password: 'pw' })).rejects.toThrow(
      /unavailable/i
    );
  });

  it('does not blame the password when the portal returns no token', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(200, { status: 'success' }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(loginAPI({ email: 'someone@example.com', password: 'pw' })).rejects.toThrow(
      /unexpected response/i
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('surfaces a connection failure as one, not as a credential problem', async () => {
    const fetchMock = vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch'));
    vi.stubGlobal('fetch', fetchMock);

    await expect(loginAPI({ email: 'someone@example.com', password: 'pw' })).rejects.toThrow(
      /Could not reach the sign-in service/i
    );
  });
});
