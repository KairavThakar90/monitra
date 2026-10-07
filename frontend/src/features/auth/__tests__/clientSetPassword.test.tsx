// @vitest-environment jsdom
/**
 * The page an invitation email's button opens: an invited client chooses their
 * password, then is sent back to sign in.
 *
 * The real page and the real API functions are used; only `fetch` is stubbed,
 * so what is asserted is what would actually go over the wire. What matters:
 *
 *  - a dead link is reported *before* a password is typed, and offers a way out;
 *  - a network failure is not reported as a dead link (the link is not used up);
 *  - a password that fails the length rule, or does not match its confirmation,
 *    is refused on the page and sends nothing;
 *  - a good one is sent exactly as typed, and the client lands on the sign-in
 *    screen with a note and their address -- signed in as nobody;
 *  - a second press while the first is in flight sends one request.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ClientSetPassword } from '../ClientSetPassword';
import { ENDPOINTS } from '../../../api/endpoints';

const TOKEN = 'tok_123';
const EMAIL = 'pat@example.com';
const GOOD = 'correct horse battery';

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const urlOf = (input: RequestInfo | URL) =>
  typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;

const LoginStub = () => {
  const location = useLocation();
  return (
    <div data-testid="login">
      {location.pathname}{location.search}|{(location.state as { email?: string } | null)?.email ?? ''}
    </div>
  );
};

describe('the set-password page', () => {
  let container: HTMLDivElement;
  let root: Root;
  let calls: { url: string; method: string; body: unknown }[];
  let reply: { read: () => Promise<Response> | Response; write: () => Promise<Response> | Response };

  const settle = async () => {
    for (let i = 0; i < 8; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  const render = async (token = TOKEN) => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={[`/client/set-password/${token}`]}>
          <Routes>
            <Route path="/client/set-password/:token" element={<ClientSetPassword />} />
            <Route path="/login" element={<LoginStub />} />
          </Routes>
        </MemoryRouter>,
      );
    });
    await settle();
  };

  const input = (id: string) => container.querySelector(`#${id}`) as HTMLInputElement;
  const type = async (id: string, value: string) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    await act(async () => {
      setter.call(input(id), value);
      input(id).dispatchEvent(new Event('input', { bubbles: true }));
    });
  };
  const submitButton = () => container.querySelector('button[type="submit"]') as HTMLButtonElement;
  const submit = async () => { await act(async () => { submitButton().click(); }); await settle(); };
  const fill = async (password = GOOD, confirm = password) => {
    await type('client-password', password);
    await type('client-confirm-password', confirm);
  };
  const writes = () => calls.filter((call) => call.method === 'POST');
  const text = () => container.textContent ?? '';

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    calls = [];
    reply = {
      read: () => json(200, { email: EMAIL }),
      write: () => json(200, { email: EMAIL }),
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      const method = init?.method ?? 'GET';
      calls.push({ url, method, body: init?.body ? JSON.parse(init.body as string) : undefined });
      return method === 'POST' ? reply.write() : reply.read();
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  describe('opening the link', () => {
    it('shows the account the link is for, read-only, and the password fields', async () => {
      await render();

      expect(calls[0]).toMatchObject({ url: ENDPOINTS.CLIENTS.INVITATION(TOKEN), method: 'GET' });
      expect(text()).toContain('Set your password');
      const email = input('client-email');
      expect(email.value).toBe(EMAIL);
      expect(email.readOnly).toBe(true);
      expect(email.getAttribute('autocomplete')).toBe('username');
      expect(input('client-password').getAttribute('autocomplete')).toBe('new-password');
      expect(input('client-confirm-password').getAttribute('autocomplete')).toBe('new-password');
      expect(writes()).toHaveLength(0);
    });

    it('says a dead link is dead before anyone types, and offers the sign-in screen', async () => {
      reply.read = () => json(401, { detail: 'This invitation link is invalid or has expired.' });
      await render();

      expect(text()).toContain("This link can't be used");
      expect(text()).toMatch(/ask the person who invited you/i);
      expect(container.querySelector('#client-password')).toBeNull();
      expect(container.querySelector('a[href="/login"]')).not.toBeNull();
    });

    it('does not call a network failure a dead link, and can try again', async () => {
      reply.read = () => { throw new TypeError('network down'); };
      await render();

      expect(text()).toContain("Couldn't open your invitation");
      expect(text()).toMatch(/has\s+not been used up/i);
      expect(text()).not.toContain("This link can't be used");

      reply.read = () => json(200, { email: EMAIL });
      const retry = Array.from(container.querySelectorAll('button')).find((b) => b.textContent === 'Try again') as HTMLButtonElement;
      await act(async () => { retry.click(); });
      await settle();

      expect(input('client-email').value).toBe(EMAIL);
    });

    it('reads the token from the address, however odd it looks', async () => {
      await render('a-b_c.d');

      expect(calls[0].url).toBe(ENDPOINTS.CLIENTS.INVITATION('a-b_c.d'));
    });
  });

  describe('choosing a password', () => {
    it('refuses one that is too short, on the page, and sends nothing', async () => {
      await render();
      await fill('short');
      await submit();

      expect(writes()).toHaveLength(0);
      expect(text()).toMatch(/at least 8 characters/i);
    });

    it('refuses an empty password and an empty confirmation', async () => {
      await render();
      await submit();
      expect(writes()).toHaveLength(0);

      await type('client-password', GOOD);
      await submit();
      expect(writes()).toHaveLength(0);
      expect(text()).toContain('Please confirm your password.');
    });

    it('refuses two passwords that differ, and says which field to retype', async () => {
      await render();
      await fill(GOOD, GOOD + 'x');
      await submit();

      expect(writes()).toHaveLength(0);
      expect(container.querySelector('#client-confirm-password-error')?.textContent).toBe('The two passwords do not match.');
    });

    it('sends a good password exactly as typed, then goes to sign-in with a note and the address', async () => {
      await render();
      const typed = '  a passphrase, with <spaces> & more  ';
      await fill(typed);
      await submit();

      expect(writes()).toHaveLength(1);
      expect(writes()[0]).toMatchObject({
        url: ENDPOINTS.CLIENTS.INVITATION_PASSWORD(TOKEN),
        body: { password: typed },
      });
      expect(container.querySelector('[data-testid="login"]')?.textContent).toBe(
        `/login?client_invite=password_set|${EMAIL}`,
      );
    });

    it('puts the password nowhere in the address', async () => {
      await render();
      await fill();
      await submit();

      expect(container.querySelector('[data-testid="login"]')?.textContent).not.toContain(GOOD);
    });

    it('moves from a link that died while the form was open to the dead-link screen', async () => {
      await render();
      reply.write = () => json(401, { detail: 'x' });
      await fill();
      await submit();

      expect(text()).toContain("This link can't be used");
      expect(container.querySelector('[data-testid="login"]')).toBeNull();
    });

    it('keeps the form, and what was typed, when the request fails, so it can be retried', async () => {
      await render();
      reply.write = () => { throw new TypeError('network down'); };
      await fill();
      await submit();

      expect(container.querySelector('[role="alert"]')?.textContent).toMatch(/could not reach monitra/i);
      expect(input('client-password').value).toBe(GOOD);
      expect(submitButton().disabled).toBe(false);

      reply.write = () => json(200, { email: EMAIL });
      await submit();
      expect(container.querySelector('[data-testid="login"]')).not.toBeNull();
    });

    it('does not call a server-refused password a dead link', async () => {
      await render();
      reply.write = () => json(422, { detail: [] });
      await fill();
      await submit();

      expect(text()).toMatch(/was not accepted/i);
      expect(text()).not.toContain("This link can't be used");
      expect(input('client-password')).not.toBeNull();
    });

    it('sends one request however many times the button is pressed while it is in flight', async () => {
      await render();
      let release: (response: Response) => void = () => {};
      reply.write = () => new Promise<Response>((resolve) => { release = resolve; });
      await fill();

      await act(async () => { submitButton().click(); });
      expect(submitButton().disabled).toBe(true);
      expect(submitButton().textContent).toBe('Saving…');
      await act(async () => { submitButton().click(); });
      await act(async () => { release(json(200, { email: EMAIL })); });
      await settle();

      expect(writes()).toHaveLength(1);
    });

    it('can show and hide what is being typed', async () => {
      await render();
      expect(input('client-password').type).toBe('password');

      const toggle = container.querySelector('button[aria-label="Show password"]') as HTMLButtonElement;
      await act(async () => { toggle.click(); });

      expect(input('client-password').type).toBe('text');
      expect(input('client-confirm-password').type).toBe('text');
      expect(container.querySelector('button[aria-label="Hide password"]')).not.toBeNull();
    });
  });
});
