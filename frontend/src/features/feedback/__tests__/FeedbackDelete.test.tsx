// @vitest-environment jsdom
/**
 * The row's Manage dropdown and the Delete in it.
 *
 * The real `FeedbackTable` is rendered against a real RTK Query store with only
 * `fetch` stubbed, so what is asserted is the request the app would actually
 * send. The things that matter:
 *
 *  - the row shows a Manage button, in Project Management's style, and nothing
 *    is in the menu until it is opened;
 *  - an administrator's menu is View and Delete; HR and Leader (`canManage`
 *    false) get View only, so the page never offers what the server refuses;
 *  - Delete asks first, sends one `DELETE /feedback/{id}` only after the
 *    confirmation, and sends nothing when it is declined;
 *  - a failure says why in words (storage down, not permitted) and the row is
 *    not reported deleted.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { Feedback } from '../../../store/api/feedbackApi';

const showToast = vi.fn();
const confirmAction = vi.fn(async () => true);
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction }),
}));

import { FeedbackTable } from '../FeedbackTable';
import { deleteConfirmation, deleteErrorMessage, DELETE_SUCCESS_MESSAGE } from '../feedbackActions';

const row = (id: number, extra: Partial<Feedback> = {}): Feedback => ({
  id, employee_id: 7, employee_name: `Employee ${id}`, category: 'report_a_problem',
  message: `Message number ${id}`, status: 'new', created_at: '2026-10-01T05:00:00+00:00',
  updated_at: null, ...extra,
});

const urlOf = (input: RequestInfo | URL) =>
  typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
const settle = async () => {
  for (let i = 0; i < 6; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }
};

describe('the row Manage dropdown', () => {
  let container: HTMLDivElement;
  let root: Root;
  let calls: { url: string; method: string }[];
  let deleteReply: () => Response;

  const render = async (items: Feedback[], canManage = true) => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(
        <Provider store={store}>
          <FeedbackTable items={items} showActions canManage={canManage} />
        </Provider>,
      );
    });
    await settle();
  };

  const manageButton = (index = 0) =>
    Array.from(container.querySelectorAll('tbody button'))
      .filter((b) => b.textContent?.trim() === 'Manage')[index] as HTMLButtonElement;
  const menuItems = () => Array.from(document.querySelectorAll('[role="menuitem"]')) as HTMLButtonElement[];
  const menuItem = (label: string) => menuItems().find((b) => b.textContent === label);
  const openMenu = async (index = 0) => {
    await act(async () => { manageButton(index).click(); });
  };
  const deletes = () => calls.filter((call) => call.method === 'DELETE');

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.setItem('accessToken', 'tok-123');
    showToast.mockClear();
    confirmAction.mockClear();
    confirmAction.mockResolvedValue(true);
    calls = [];
    deleteReply = () => new Response(null, { status: 204 });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const method = input instanceof Request ? input.method : (init?.method ?? 'GET');
      calls.push({ url: urlOf(input), method });
      if (method === 'DELETE') return deleteReply();
      return new Response('', { status: 404 });
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => { root.unmount(); });
    container.remove();
    vi.unstubAllGlobals();
  });

  it('draws Manage like Project Management: a grey button with a chevron, and a closed menu', async () => {
    await render([row(1)]);
    const button = manageButton();
    expect(button).toBeTruthy();
    expect(button.className).toContain('bg-slate-100');
    expect(button.className).toContain('text-slate-700');
    expect(button.querySelector('svg')).toBeTruthy();
    expect(menuItems()).toHaveLength(0);
  });

  it('keeps Working, Resolved and Manage on one line in the table, so the rows line up', async () => {
    await render([row(1), row(2, { status: 'in_progress' })]);
    for (const index of [0, 1]) {
      const controls = manageButton(index).parentElement!;
      expect(controls.className).toContain('flex-nowrap');
      expect(controls.className).not.toContain('flex-wrap');
      expect(Array.from(controls.children).map((c) => c.textContent?.replace('✓ ', '').trim())).toEqual(['Working', 'Resolved', 'Manage']);
    }
  });

  it('opens a menu of View and Delete for an administrator, styled like Project Management', async () => {
    await render([row(1)]);
    await openMenu();

    expect(menuItems().map((b) => b.textContent)).toEqual(['View', 'Delete']);
    expect(menuItem('View')!.className).toContain('text-indigo-600');
    expect(menuItem('Delete')!.className).toContain('text-rose-600');
    expect(document.querySelector('[role="menu"]')!.className).toContain('shadow-xl');
  });

  it('offers HR and Leader View only -- no Delete', async () => {
    await render([row(1)], false);
    await openMenu();

    expect(menuItems().map((b) => b.textContent)).toEqual(['View']);
  });

  it('closes on Escape and on a click elsewhere, deleting nothing', async () => {
    await render([row(1)]);
    await openMenu();
    await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); });
    expect(menuItems()).toHaveLength(0);

    await openMenu();
    await act(async () => { (document.querySelector('.fixed.inset-0') as HTMLElement).click(); });
    expect(menuItems()).toHaveLength(0);
    expect(deletes()).toHaveLength(0);
  });

  it('asks first, naming the employee, then sends one DELETE for that row', async () => {
    await render([row(1), row(2)]);
    await openMenu(1);
    await act(async () => { menuItem('Delete')!.click(); });
    await settle();

    const { title, message } = deleteConfirmation('Employee 2');
    expect(confirmAction).toHaveBeenCalledWith(title, message);
    expect(message).toContain('Employee 2');
    expect(deletes()).toHaveLength(1);
    expect(deletes()[0].url).toMatch(/\/feedback\/2$/);
    expect(showToast).toHaveBeenCalledWith(DELETE_SUCCESS_MESSAGE, 'success');
    expect(menuItems()).toHaveLength(0);                       // the menu closed on press
  });

  it('sends nothing when the confirmation is declined', async () => {
    confirmAction.mockResolvedValue(false);
    await render([row(1)]);
    await openMenu();
    await act(async () => { menuItem('Delete')!.click(); });
    await settle();

    expect(confirmAction).toHaveBeenCalledTimes(1);
    expect(deletes()).toHaveLength(0);
    expect(showToast).not.toHaveBeenCalled();
  });

  it('says storage was unavailable and nothing was deleted when the server answers 502', async () => {
    deleteReply = () => new Response(JSON.stringify({ detail: 'down' }), { status: 502, headers: { 'content-type': 'application/json' } });
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    await render([row(1)]);
    await openMenu();
    await act(async () => { menuItem('Delete')!.click(); });
    await settle();

    expect(showToast).toHaveBeenCalledWith(deleteErrorMessage(502), 'error');
    expect(showToast).not.toHaveBeenCalledWith(DELETE_SUCCESS_MESSAGE, 'success');
    expect(deleteErrorMessage(502)).toContain('nothing was deleted');
  });

  it('words a refusal and a vanished row in plain terms', () => {
    expect(deleteErrorMessage(403)).toBe('Only an administrator can delete feedback.');
    expect(deleteErrorMessage(404)).toBe('That feedback no longer exists.');
    expect(deleteErrorMessage(500)).toBe('Unable to delete this feedback. Please try again.');
  });
});
