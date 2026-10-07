// @vitest-environment jsdom
/**
 * Screenshot Privacy: the member filter, and choosing who a new rule applies to.
 *
 * Both member controls on this page are the Reports page's own filter
 * (`MemberMultiSelect`): the picker for whose settings are shown (one member at
 * a time) and the "Applies to" filter in the Add Privacy Rule drawer ("All
 * members" until someone is picked). The real page, the real filter and a real
 * RTK Query store are used; only `fetch` and the shell are replaced, so what is
 * asserted is what would go over the wire. What matters:
 *
 *  - the member picker is the report-style filter, offers only people who
 *    capture screenshots, and a pick closes it and shows that member's rules;
 *  - the drawer defaults to All members, says exactly what saving will do, and
 *    sends `all` or the chosen ids with the rule, for applications and websites;
 *  - a refusal keeps the drawer open with what was typed, and a second press
 *    while a save is in flight sends one request;
 *  - saving refreshes the exclusions of the member being viewed.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

const showToast = vi.fn();
vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ children, actions }: { children: React.ReactNode; actions?: React.ReactNode }) => (
    <>{actions}{children}</>
  ),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction: async () => true }),
}));

import { AdminScreenshotPrivacy } from '../AdminScreenshotPrivacy';

const member = (id: number, name: string, role = 'employee', status = 'active') => ({
  id, name, email: `${name.split(' ')[0].toLowerCase()}@example.com`, role, status,
  date_of_joining: null, date_of_birth: null, designation: '',
});
const ALICE = member(1, 'Alice Active');
const BOB = member(2, 'Bob Builder', 'manager');
const CARL = member(3, 'Carl Casual');
const MEMBERS = [
  ALICE, BOB, CARL,
  member(4, 'Dora Dormant', 'employee', 'inactive'),
  member(5, 'Client Co', 'client'),
  member(6, 'Release Bot', 'release_bot'),
];
const ACTIVE_CAPTURING = 3;

const RULE = { id: 50, name: 'Slack', process_name: 'slack.exe', category: 'Communication', is_active: true,
  created_at: '2026-10-07T00:00:00Z', updated_at: '2026-10-07T00:00:00Z' };

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const urlOf = (input: RequestInfo | URL) =>
  typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;

type Call = { path: string; method: string; body: any };

describe('Screenshot Privacy member filters', () => {
  let container: HTMLDivElement;
  let root: Root;
  let calls: Call[];
  let postReply: () => Response | Promise<Response>;
  let exclusionReply: (userId: number) => Response | Promise<Response>;
  let applications: unknown[];

  const settle = async () => {
    for (let i = 0; i < 10; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };

  const render = async () => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => { root.render(<Provider store={store}><AdminScreenshotPrivacy /></Provider>); });
    await settle();
  };

  const text = () => container.textContent ?? '';
  const posts = () => calls.filter((c) => c.method === 'POST');
  const exclusionReads = (userId: number) =>
    calls.filter((c) => c.method === 'GET' && c.path.endsWith(`/users/${userId}/screenshot-exclusions`));

  /** The card around the member picker, and the drawer. */
  const picker = () =>
    Array.from(container.querySelectorAll('h2')).find((h) => h.textContent === 'Member')!.closest('.rounded-xl') as HTMLElement;
  const drawer = () =>
    Array.from(container.querySelectorAll('h2')).find((h) => h.textContent === 'Add Privacy Rule')!
      .closest('.max-w-md') as HTMLElement;

  const trigger = (scope: HTMLElement) => scope.querySelector('button[aria-haspopup="listbox"]') as HTMLButtonElement;
  const click = async (el: Element) => { await act(async () => { (el as HTMLElement).click(); }); await settle(); };
  const optionButtons = (scope: HTMLElement) =>
    Array.from(scope.querySelectorAll('.custom-scrollbar button')) as HTMLButtonElement[];
  const optionNamed = (scope: HTMLElement, name: string) =>
    optionButtons(scope).find((b) => b.textContent?.includes(name))!;

  const type = async (el: HTMLInputElement, value: string) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    await act(async () => { setter.call(el, value); el.dispatchEvent(new Event('input', { bubbles: true })); });
  };
  const openDrawer = async () => {
    await click(Array.from(container.querySelectorAll('button')).find((b) => b.textContent === '+ Add Privacy Rule')!);
  };
  const fillApplication = async () => {
    const inputs = drawer().querySelectorAll('input[type="text"]');
    // Display name, process name, category (the member filter's own search box is not among these).
    await type(drawer().querySelector('input[placeholder="e.g. Slack"]') as HTMLInputElement, 'Slack');
    await type(drawer().querySelector('input[placeholder="e.g. slack.exe"]') as HTMLInputElement, 'slack.exe');
    await type(drawer().querySelector('input[placeholder="e.g. Communication"]') as HTMLInputElement, 'Communication');
    expect(inputs.length).toBeGreaterThan(2);
  };
  const saveButton = () =>
    Array.from(drawer().querySelectorAll('button')).find((b) => /^(Save Rule|Saving…)$/.test(b.textContent ?? '')) as HTMLButtonElement;
  const note = () => drawer().querySelector('[data-testid="rule-scope-note"]')!.textContent ?? '';

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.setItem('accessToken', 'tok');
    showToast.mockClear();
    calls = [];
    postReply = () => json(200, { ...RULE, applied_to_count: ACTIVE_CAPTURING });
    exclusionReply = () => json(200, []);
    applications = [RULE];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(urlOf(input), 'http://localhost').pathname;
      const request = input instanceof Request ? input : null;
      const method = request?.method ?? init?.method ?? 'GET';
      let body: any;
      try { body = request ? await request.clone().json() : init?.body ? JSON.parse(init.body as string) : undefined; } catch { body = undefined; }
      calls.push({ path, method, body });
      if (method === 'POST') return postReply();
      if (path.endsWith('/members')) return json(200, { items: MEMBERS, page: 1, limit: 100, total: MEMBERS.length, pages: 1 });
      if (path.endsWith('/screenshot/applications')) return json(200, applications);
      if (path.endsWith('/screenshot/urls')) return json(200, []);
      const owner = /\/users\/(\d+)\/screenshot-exclusions$/.exec(path);
      if (owner) return exclusionReply(Number(owner[1]));
      return new Response('', { status: 404 });
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    localStorage.removeItem('accessToken');
  });

  describe('the member picker', () => {
    it('is the report-style filter, not a native select', async () => {
      await render();

      expect(picker().querySelector('select')).toBeNull();
      expect(trigger(picker())).not.toBeNull();
      expect(trigger(picker()).textContent).toContain('Select a member');
      expect(trigger(picker()).className).toContain('rounded-lg');
    });

    it('offers only active people who capture screenshots, with search, and no select-all row', async () => {
      await render();
      await click(trigger(picker()));

      const names = optionButtons(picker()).map((b) => b.textContent ?? '');
      expect(names).toHaveLength(ACTIVE_CAPTURING);
      expect(names.join('|')).toMatch(/Alice Active/);
      expect(names.join('|')).not.toMatch(/Dora Dormant|Client Co|Release Bot/);
      expect(picker().querySelector('input[placeholder="Search members..."]')).not.toBeNull();
      expect(picker().textContent).not.toMatch(/Select all|Clear/);
    });

    it('closes on a pick, names the member, and shows that member\'s rules', async () => {
      await render();
      expect(text()).toContain('No member selected');
      await click(trigger(picker()));
      await click(optionNamed(picker(), 'Bob Builder'));

      expect(optionButtons(picker())).toHaveLength(0);
      expect(trigger(picker()).textContent).toContain('Bob Builder');
      expect(text()).toContain('Managing screenshot privacy for');
      expect(text()).not.toContain('No member selected');
      expect(exclusionReads(2)).toHaveLength(1);
    });

    it('replaces the choice rather than adding to it', async () => {
      await render();
      await click(trigger(picker()));
      await click(optionNamed(picker(), 'Alice Active'));
      await click(trigger(picker()));
      await click(optionNamed(picker(), 'Carl Casual'));

      expect(trigger(picker()).textContent).toContain('Carl Casual');
      expect(trigger(picker()).textContent).not.toContain('Alice');
      expect(exclusionReads(3)).toHaveLength(1);
    });

    it("never shows the previous member's states under the next member's name while theirs load", async () => {
      const ALICE_EXCLUDES_SLACK = [{
        id: 900, user_id: 1, application_id: 50, url_id: null, exclusion_type: 'application', is_excluded: true,
        created_at: '2026-10-07T00:00:00Z', updated_at: '2026-10-07T00:00:00Z',
      }];
      let releaseBob: (response: Response) => void = () => {};
      exclusionReply = (userId) =>
        userId === 1
          ? json(200, ALICE_EXCLUDES_SLACK)
          : new Promise<Response>((resolve) => { releaseBob = resolve; });
      await render();

      await click(trigger(picker()));
      await click(optionNamed(picker(), 'Alice Active'));
      const rows = () => Array.from(container.querySelectorAll('tbody tr')).map((row) => row.textContent ?? '').join('|');
      expect(rows()).toContain('Excluded');

      await click(trigger(picker()));
      await click(optionNamed(picker(), 'Bob Builder'));
      // Bob's rules have not arrived: no table, and above all not Alice's "Excluded".
      expect(container.querySelector('tbody')).toBeNull();
      expect(rows()).not.toContain('Excluded');

      await act(async () => { releaseBob(json(200, [])); });
      await settle();
      expect(container.querySelector('tbody')).not.toBeNull();
      expect(rows()).toContain('Captured');
      expect(rows()).not.toContain('Excluded');
    });

    it('narrows the list as you type', async () => {
      await render();
      await click(trigger(picker()));
      await type(picker().querySelector('input[placeholder="Search members..."]') as HTMLInputElement, 'carl');

      expect(optionButtons(picker()).map((b) => b.textContent ?? '').join('|')).toContain('Carl Casual');
      expect(optionButtons(picker())).toHaveLength(1);
    });
  });

  describe('Add Privacy Rule: Applies to', () => {
    it('defaults to All members and says what saving will do', async () => {
      await render();
      await openDrawer();

      expect(drawer().textContent).toContain('Applies to');
      expect(trigger(drawer()).textContent).toContain('All members');
      expect(note()).toContain(`all ${ACTIVE_CAPTURING} active members`);
      expect(note()).toMatch(/join later are not added/i);
    });

    it('uses the same filter as the picker and the report pages, in several-at-a-time mode', async () => {
      await render();
      await openDrawer();
      await click(trigger(drawer()));

      expect(drawer().textContent).toMatch(/Select all/);
      expect(drawer().textContent).toMatch(/Clear/);
      expect(optionButtons(drawer())).toHaveLength(ACTIVE_CAPTURING);
    });

    it('changes the note as members are chosen', async () => {
      await render();
      await openDrawer();
      await click(trigger(drawer()));
      await click(optionNamed(drawer(), 'Alice Active'));
      expect(note()).toContain('1 selected member.');
      await click(optionNamed(drawer(), 'Bob Builder'));

      expect(note()).toContain('2 selected members.');
      expect(note()).not.toMatch(/all \d+ active/);
    });

    it('sends "all" with an application rule by default', async () => {
      await render();
      await openDrawer();
      await fillApplication();
      await click(saveButton());

      expect(posts()).toHaveLength(1);
      expect(posts()[0].path).toMatch(/\/screenshot\/applications$/);
      expect(posts()[0].body).toEqual({
        name: 'Slack', process_name: 'slack.exe', category: 'Communication', is_active: true,
        apply_to: { scope: 'all' },
      });
    });

    it('sends exactly the chosen ids, as numbers', async () => {
      await render();
      await openDrawer();
      await fillApplication();
      await click(trigger(drawer()));
      await click(optionNamed(drawer(), 'Alice Active'));
      await click(optionNamed(drawer(), 'Carl Casual'));
      await click(saveButton());

      expect(posts()[0].body.apply_to).toEqual({ scope: 'members', user_ids: [1, 3] });
    });

    it('sends the same choice with a website rule', async () => {
      await render();
      await openDrawer();
      await click(Array.from(drawer().querySelectorAll('label')).find((l) => l.textContent === 'Website URL')!);
      await type(drawer().querySelector('input[placeholder="e.g. Slack"]') as HTMLInputElement, 'Slack web');
      await type(drawer().querySelector('input[placeholder="e.g. slack.com"]') as HTMLInputElement, 'slack.com');
      await type(drawer().querySelector('input[placeholder="e.g. https://app.slack.com/*"]') as HTMLInputElement, 'https://app.slack.com/*');
      await type(drawer().querySelector('input[placeholder="e.g. Communication"]') as HTMLInputElement, 'Communication');
      await click(trigger(drawer()));
      await click(optionNamed(drawer(), 'Bob Builder'));
      await click(saveButton());

      expect(posts()[0].path).toMatch(/\/screenshot\/urls$/);
      expect(posts()[0].body).toMatchObject({ domain: 'slack.com', apply_to: { scope: 'members', user_ids: [2] } });
    });

    it('confirms how many members it was switched on for, then closes and starts clean', async () => {
      await render();
      await openDrawer();
      await fillApplication();
      await click(trigger(drawer()));
      await click(optionNamed(drawer(), 'Alice Active'));
      await click(saveButton());

      expect(showToast).toHaveBeenCalledWith('Rule added and switched on for 3 members.', 'success');
      expect(drawer().parentElement?.className).toContain('pointer-events-none');
      // Reopened: the fields and the choice are back to their defaults.
      await openDrawer();
      expect((drawer().querySelector('input[placeholder="e.g. Slack"]') as HTMLInputElement).value).toBe('');
      expect(trigger(drawer()).textContent).toContain('All members');
    });

    it('says so when there was nobody to switch it on for', async () => {
      postReply = () => json(200, { ...RULE, applied_to_count: 0 });
      await render();
      await openDrawer();
      await fillApplication();
      await click(saveButton());

      expect(showToast).toHaveBeenCalledWith('Rule added. There were no active members to switch it on for.', 'success');
    });

    it('does not let a rule be saved until it is complete', async () => {
      await render();
      await openDrawer();

      expect(saveButton().disabled).toBe(true);
      await fillApplication();
      expect(saveButton().disabled).toBe(false);
    });
  });

  describe('when saving does not work', () => {
    it('keeps the drawer open with what was typed and says why', async () => {
      postReply = () => json(400, { detail: 'These members could not be found: [99].' });
      await render();
      await openDrawer();
      await fillApplication();
      await click(trigger(drawer()));
      await click(optionNamed(drawer(), 'Alice Active'));
      await click(saveButton());

      expect(drawer().querySelector('[role="alert"]')?.textContent).toBe('These members could not be found: [99].');
      expect((drawer().querySelector('input[placeholder="e.g. Slack"]') as HTMLInputElement).value).toBe('Slack');
      expect(drawer().parentElement?.className).toContain('pointer-events-auto');
      expect(showToast).not.toHaveBeenCalled();
      expect(saveButton().disabled).toBe(false);
    });

    it('gives a plain message when the server sends nothing readable', async () => {
      postReply = () => new Response('<html>Bad gateway</html>', { status: 502 });
      await render();
      await openDrawer();
      await fillApplication();
      await click(saveButton());

      expect(drawer().querySelector('[role="alert"]')?.textContent).toMatch(/nothing was changed/i);
    });

    it('clears the message when the drawer is closed and opened again', async () => {
      postReply = () => json(403, { detail: 'Insufficient permissions for this action' });
      await render();
      await openDrawer();
      await fillApplication();
      await click(saveButton());
      expect(drawer().querySelector('[role="alert"]')).not.toBeNull();

      await click(Array.from(drawer().querySelectorAll('button')).find((b) => b.textContent === 'Cancel')!);
      await openDrawer();

      expect(drawer().querySelector('[role="alert"]')).toBeNull();
    });

    it('sends one request however many times Save is pressed while it is in flight', async () => {
      let release: (response: Response) => void = () => {};
      postReply = () => new Promise<Response>((resolve) => { release = resolve; });
      await render();
      await openDrawer();
      await fillApplication();

      await act(async () => { saveButton().click(); });
      expect(saveButton().textContent).toBe('Saving…');
      expect(saveButton().disabled).toBe(true);
      await act(async () => { saveButton().click(); });
      await act(async () => { release(json(200, { ...RULE, applied_to_count: 3 })); });
      await settle();

      expect(posts()).toHaveLength(1);
    });
  });

  describe('the rule list', () => {
    const catalogue = () => container.querySelector('[role="tablist"][aria-label="Rule type"]');
    const listedNames = () =>
      Array.from(container.querySelectorAll('tbody tr td span.truncate')).map((el) => el.textContent ?? '');

    it('shows the rules that have been added while no member is picked', async () => {
      await render();

      expect(catalogue()).not.toBeNull();
      expect(listedNames()).toEqual(['Slack']);
      expect(text()).toContain('slack.exe');
      expect(text()).toContain('No member selected');
    });

    it("gives way to the member's own switches once a member is picked", async () => {
      await render();
      await click(trigger(picker()));
      await click(optionNamed(picker(), 'Alice Active'));

      expect(catalogue()).toBeNull();
      expect(text()).toContain('Managing screenshot privacy for');
      expect(text()).toContain('Desktop Applications');
    });

    it('shows a rule as soon as it is saved from the drawer', async () => {
      const added = { ...RULE, id: 51, name: 'Zoom', process_name: 'zoom.exe', created_at: '2026-10-08T00:00:00Z' };
      postReply = () => {
        applications = [RULE, added];
        return json(200, { ...added, applied_to_count: ACTIVE_CAPTURING });
      };
      await render();
      expect(listedNames()).toEqual(['Slack']);

      await openDrawer();
      await type(drawer().querySelector('input[placeholder="e.g. Slack"]') as HTMLInputElement, 'Zoom');
      await type(drawer().querySelector('input[placeholder="e.g. slack.exe"]') as HTMLInputElement, 'zoom.exe');
      await type(drawer().querySelector('input[placeholder="e.g. Communication"]') as HTMLInputElement, 'Communication');
      await click(saveButton());

      expect(listedNames()).toEqual(['Zoom', 'Slack']);
    });

    it('says there are no rules yet when the catalogue is empty', async () => {
      applications = [];
      await render();

      expect(text()).toContain('No privacy rules yet');
      expect(catalogue()).toBeNull();
    });
  });

  describe('after saving', () => {
    it("refreshes the exclusions of the member being viewed", async () => {
      await render();
      await click(trigger(picker()));
      await click(optionNamed(picker(), 'Alice Active'));
      expect(exclusionReads(1)).toHaveLength(1);

      await openDrawer();
      await fillApplication();
      await click(saveButton());

      expect(exclusionReads(1).length).toBeGreaterThan(1);
    });
  });
});
