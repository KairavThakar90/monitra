/**
 * A client's password, on the wire.
 *
 * Three properties matter, and each is easy to break while every happy path
 * keeps working:
 *
 * 1. **A password goes only where it belongs.** A client's goes to our backend
 *    and never to the staff provider; a staff member's goes to the provider and
 *    never to our backend. The sign-in form decides with a question that carries
 *    the email address alone.
 * 2. **A slow or broken "which way?" question cannot lock staff out.** It reads
 *    as "no" and sign-in carries on down the path it has always taken.
 * 3. **The password is sent exactly as typed.** Never trimmed or altered
 *    (docs/VALIDATION.md, "Passwords are special").
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ClientInvitationError,
  clientPasswordLoginAPI,
  clientSignInMethodAPI,
  getClientInvitationAPI,
  setClientPasswordAPI,
  signInAPI,
} from '../auth';
import { AUTH_PROVIDER_LOGIN_URL, ENDPOINTS } from '../endpoints';
import { LoginDisabledError } from '../../auth/loginAccess';

const SESSION = {
  access_token: 'monitra-access',
  refresh_token: 'monitra-refresh',
  token_type: 'Bearer',
  user: { id: 9, email: 'pat@example.com', role_name: 'client' },
};

function jsonResponse(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

const bodyOf = (call: unknown[]) => JSON.parse((call[1] as RequestInit).body as string);
const urlsOf = (fetchMock: ReturnType<typeof vi.fn>) => fetchMock.mock.calls.map((call) => call[0]);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('clientSignInMethodAPI', () => {
  it('asks with the email alone and reads the answer', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, { password_required: true }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(clientSignInMethodAPI('pat@example.com')).resolves.toBe(true);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(ENDPOINTS.AUTH.CLIENT_SIGN_IN_METHOD);
    expect(bodyOf(fetchMock.mock.calls[0])).toEqual({ email: 'pat@example.com' });
  });

  it('reads "no" as no', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, { password_required: false })));
    await expect(clientSignInMethodAPI('staff@example.com')).resolves.toBe(false);
  });

  it('never throws: an error status, a bad body or a dead network all read as no', async () => {
    for (const reply of [
      () => Promise.resolve(jsonResponse(500, {})),
      () => Promise.resolve(jsonResponse(200, { password_required: 'yes' })),
      () => Promise.resolve(jsonResponse(200, null)),
      () => Promise.reject(new TypeError('network down')),
    ]) {
      vi.stubGlobal('fetch', vi.fn(reply));
      await expect(clientSignInMethodAPI('x@example.com')).resolves.toBe(false);
    }
  });

  it('gives up on a question that never answers, so staff are not held up', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    })));

    const pending = clientSignInMethodAPI('x@example.com');
    await vi.advanceTimersByTimeAsync(4001);

    await expect(pending).resolves.toBe(false);
  });
});

describe('clientPasswordLoginAPI', () => {
  it('sends the password exactly as typed, to our backend, and returns the session', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, SESSION));
    vi.stubGlobal('fetch', fetchMock);
    const awkward = '  <b>p@ss</b> wörd {x}  ';

    const result = await clientPasswordLoginAPI('pat@example.com', awkward);

    expect(result).toEqual(SESSION);
    expect(fetchMock.mock.calls[0][0]).toBe(ENDPOINTS.AUTH.CLIENT_LOGIN);
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('POST');
    expect(bodyOf(fetchMock.mock.calls[0])).toEqual({ email: 'pat@example.com', password: awkward });
  });

  it("surfaces the backend's wording on a refusal", async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(401, { detail: 'Invalid email or password' })));
    await expect(clientPasswordLoginAPI('pat@example.com', 'nope')).rejects.toThrow('Invalid email or password');
  });

  it('falls back to the same wording when the refusal has no readable body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => { throw new Error('x'); } }));
    await expect(clientPasswordLoginAPI('pat@example.com', 'nope')).rejects.toThrow('Invalid email or password');
  });

  it('tells an excluded account apart from a wrong password', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse(403, { detail: { code: 'login_disabled', message: 'm', note: 'n' } }),
    ));
    await expect(clientPasswordLoginAPI('pat@example.com', 'pw')).rejects.toBeInstanceOf(LoginDisabledError);
  });

  it('says what the user can act on when the network is down, not "wrong password"', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('network down')));
    await expect(clientPasswordLoginAPI('pat@example.com', 'pw')).rejects.toThrow(/could not reach/i);
  });
});

describe('signInAPI', () => {
  it("sends a client's password to our backend and never to the staff provider", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(200, { password_required: true }))
      .mockResolvedValueOnce(jsonResponse(200, SESSION));
    vi.stubGlobal('fetch', fetchMock);

    const result = await signInAPI({ email: 'pat@example.com', password: 'secret-pw' });

    expect(result).toEqual(SESSION);
    expect(urlsOf(fetchMock)).toEqual([ENDPOINTS.AUTH.CLIENT_SIGN_IN_METHOD, ENDPOINTS.AUTH.CLIENT_LOGIN]);
    expect(urlsOf(fetchMock)).not.toContain(AUTH_PROVIDER_LOGIN_URL);
    // The question carried the address and nothing secret.
    expect(bodyOf(fetchMock.mock.calls[0])).toEqual({ email: 'pat@example.com' });
  });

  it("sends a staff member's password to the provider and never to our backend", async () => {
    const STAFF = { ...SESSION, user: { ...SESSION.user, role_name: 'employee' } };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(200, { password_required: false }))
      .mockResolvedValueOnce(jsonResponse(200, { status: 'success', access_token: 'portal-jwt' }))
      .mockResolvedValueOnce(jsonResponse(200, STAFF));
    vi.stubGlobal('fetch', fetchMock);

    const result = await signInAPI({ email: 'staff@example.com', password: 'staff-pw' });

    expect(result).toEqual(STAFF);
    expect(urlsOf(fetchMock)).toEqual([
      ENDPOINTS.AUTH.CLIENT_SIGN_IN_METHOD, AUTH_PROVIDER_LOGIN_URL, ENDPOINTS.AUTH.SSO_TOKEN,
    ]);
    // Neither of the two calls to our backend before the provider's token
    // exists contains the staff password.
    for (const call of [fetchMock.mock.calls[0], fetchMock.mock.calls[2]]) {
      expect((call[1] as RequestInit).body as string).not.toContain('staff-pw');
    }
    expect(bodyOf(fetchMock.mock.calls[1])).toEqual({ username: 'staff@example.com', password: 'staff-pw' });
  });

  it('carries on to the staff provider when the question cannot be answered', async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('network down'))
      .mockResolvedValueOnce(jsonResponse(200, { status: 'success', access_token: 'portal-jwt' }))
      .mockResolvedValueOnce(jsonResponse(200, SESSION));
    vi.stubGlobal('fetch', fetchMock);

    await signInAPI({ email: 'staff@example.com', password: 'pw' });

    expect(urlsOf(fetchMock)[1]).toBe(AUTH_PROVIDER_LOGIN_URL);
  });

  it("reports a client's wrong password as one, not as a provider failure", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(200, { password_required: true }))
      .mockResolvedValueOnce(jsonResponse(401, { detail: 'Invalid email or password' }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(signInAPI({ email: 'pat@example.com', password: 'wrong' })).rejects.toThrow('Invalid email or password');
    expect(urlsOf(fetchMock)).not.toContain(AUTH_PROVIDER_LOGIN_URL);
  });
});

describe('the invitation link calls', () => {
  it('reads which account a link is for, with no credentials and no body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, { email: 'pat@example.com' }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(getClientInvitationAPI('tok-123')).resolves.toEqual({ email: 'pat@example.com' });

    expect(fetchMock.mock.calls[0][0]).toBe(ENDPOINTS.CLIENTS.INVITATION('tok-123'));
    expect(fetchMock.mock.calls[0][1]).toBeUndefined();
  });

  it('puts the token in the path, encoded', () => {
    expect(ENDPOINTS.CLIENTS.INVITATION('a/b?c')).toContain('a%2Fb%3Fc');
    expect(ENDPOINTS.CLIENTS.INVITATION_PASSWORD('tok')).toMatch(/\/clients\/invitations\/tok\/password$/);
  });

  it('chooses the password with the text exactly as typed', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, { email: 'pat@example.com' }));
    vi.stubGlobal('fetch', fetchMock);
    const typed = ' a passphrase with spaces ';

    await expect(setClientPasswordAPI('tok-123', typed)).resolves.toEqual({ email: 'pat@example.com' });

    expect(fetchMock.mock.calls[0][0]).toBe(ENDPOINTS.CLIENTS.INVITATION_PASSWORD('tok-123'));
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('POST');
    expect(bodyOf(fetchMock.mock.calls[0])).toEqual({ password: typed });
  });

  it('calls a dead link invalid, on both calls', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(401, { detail: 'x' })));

    for (const call of [() => getClientInvitationAPI('t'), () => setClientPasswordAPI('t', 'long enough pw')]) {
      const error = await call().catch((err: unknown) => err);
      expect(error).toBeInstanceOf(ClientInvitationError);
      expect((error as ClientInvitationError).kind).toBe('invalid');
      expect((error as Error).message).toMatch(/invalid or has expired/i);
    }
  });

  it('calls a refused password rejected, so the link is not reported as dead', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(422, { detail: [] })));

    const error = await setClientPasswordAPI('t', 'short').catch((err: unknown) => err);

    expect((error as ClientInvitationError).kind).toBe('rejected');
  });

  it('calls a network failure or a server error unavailable -- worth trying again', async () => {
    for (const reply of [
      () => Promise.reject(new TypeError('network down')),
      () => Promise.resolve(jsonResponse(500, {})),
      () => Promise.resolve(jsonResponse(200, { nope: true })),
    ]) {
      vi.stubGlobal('fetch', vi.fn(reply));
      const error = await getClientInvitationAPI('t').catch((err: unknown) => err);
      expect((error as ClientInvitationError).kind).toBe('unavailable');
    }
  });
});
