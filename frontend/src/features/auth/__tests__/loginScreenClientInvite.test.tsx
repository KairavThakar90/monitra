// @vitest-environment jsdom
/**
 * The sign-in screen, for a client who has just chosen a password.
 *
 * The set-password page sends the client here with `?client_invite=password_set`
 * and their address in the router's state. The screen then has to say what
 * happened, have the address ready, and leave the password as the only thing to
 * type -- and a client's password has to reach `login` exactly as typed.
 *
 * `useAuth` is replaced so the screen can be driven without a session; the
 * wiring from `login` to the right endpoint is tested in `clientAuth.test.ts`.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const auth = vi.hoisted(() => ({
  login: vi.fn(async () => {}),
  loginAsClient: vi.fn(async () => {}),
}));

vi.mock('../authContext', () => ({
  useAuth: () => ({ login: auth.login, loginAsClient: auth.loginAsClient, ssoError: null }),
}));

import { LoginScreen } from '../LoginScreen';

describe('the sign-in screen after a client chooses a password', () => {
  let container: HTMLDivElement;
  let root: Root;

  const settle = async () => {
    for (let i = 0; i < 4; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  const render = async (opts: { query?: string; state?: unknown } = {}) => {
    window.history.replaceState({}, '', `/login${opts.query ?? ''}`);
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={[{ pathname: '/login', state: opts.state }]}>
          <Routes>
            <Route path="/login" element={<LoginScreen />} />
            <Route path="/dashboard" element={<div data-testid="dashboard" />} />
          </Routes>
        </MemoryRouter>,
      );
    });
    await settle();
  };

  const field = (id: string) => container.querySelector(`#${id}`) as HTMLInputElement;
  const type = async (id: string, value: string) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    await act(async () => {
      setter.call(field(id), value);
      field(id).dispatchEvent(new Event('input', { bubbles: true }));
    });
  };
  const signIn = async () => {
    await act(async () => { (container.querySelector('button[type="submit"]') as HTMLButtonElement).click(); });
    await settle();
  };
  const text = () => container.textContent ?? '';

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    auth.login.mockReset().mockResolvedValue(undefined);
    auth.loginAsClient.mockReset().mockResolvedValue(undefined);
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    window.history.replaceState({}, '', '/');
  });

  it('says the password was set, fills in the address, and puts the cursor in the password field', async () => {
    await render({ query: '?client_invite=password_set', state: { email: 'pat@example.com' } });

    expect(container.querySelector('[role="status"]')?.textContent).toMatch(/your password has been set/i);
    expect(field('email').value).toBe('pat@example.com');
    expect(document.activeElement).toBe(field('password'));
  });

  it('takes the note out of the address bar, so a reload does not repeat it', async () => {
    await render({ query: '?client_invite=password_set&keep=1', state: { email: 'pat@example.com' } });

    expect(window.location.search).toBe('?keep=1');
  });

  it('still shows the declined-invitation note for a rejected link', async () => {
    await render({ query: '?client_invite=rejected' });

    expect(text()).toMatch(/you have declined this client invitation/i);
    expect(container.querySelector('[role="status"]')).toBeNull();
  });

  it('shows no note, and no address, on an ordinary visit', async () => {
    await render();

    expect(container.querySelector('[role="status"]')).toBeNull();
    expect(text()).not.toMatch(/declined|password has been set/i);
    expect(field('email').value).toBe('');
    expect(document.activeElement).not.toBe(field('password'));
  });

  it('ignores a note it does not know and an address that is not text', async () => {
    await render({ query: '?client_invite=something-else', state: { email: 42 } });

    expect(container.querySelector('[role="status"]')).toBeNull();
    expect(field('email').value).toBe('');
  });

  it("signs the client in with the password exactly as typed, then leaves the sign-in screen", async () => {
    await render({ query: '?client_invite=password_set', state: { email: 'pat@example.com' } });
    const typed = '  correct horse <battery> staple  ';
    await type('password', typed);
    await signIn();

    expect(auth.login).toHaveBeenCalledTimes(1);
    expect(auth.login).toHaveBeenCalledWith('pat@example.com', typed);
    expect(auth.loginAsClient).not.toHaveBeenCalled();
    expect(container.querySelector('[data-testid="dashboard"]')).not.toBeNull();
  });

  it('shows the reason when the password is wrong, and stays on the screen', async () => {
    auth.login.mockRejectedValue(new Error('Invalid email or password'));
    await render({ query: '?client_invite=password_set', state: { email: 'pat@example.com' } });
    await type('password', 'not the password');
    await signIn();

    expect(text()).toContain('Invalid email or password');
    expect(container.querySelector('[data-testid="dashboard"]')).toBeNull();
  });

  it('tells a client who has a password, and left it blank, to enter it', async () => {
    auth.loginAsClient.mockRejectedValue(new Error('Enter your email address and password to sign in.'));
    await render({ query: '?client_invite=password_set', state: { email: 'pat@example.com' } });
    await signIn();

    expect(auth.loginAsClient).toHaveBeenCalledWith('pat@example.com');
    expect(auth.login).not.toHaveBeenCalled();
    expect(text()).toContain('Enter your email address and password to sign in.');
  });

  it('leaves an ordinary staff sign-in exactly as it was', async () => {
    await render();
    await type('email', 'staff@example.com');
    await type('password', 'staff-password');
    await signIn();

    expect(auth.login).toHaveBeenCalledWith('staff@example.com', 'staff-password');
    expect(auth.loginAsClient).not.toHaveBeenCalled();
  });
});
