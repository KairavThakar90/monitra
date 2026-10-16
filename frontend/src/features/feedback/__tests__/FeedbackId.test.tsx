// @vitest-environment jsdom
/**
 * The feedback id on the organization-wide Feedback page.
 *
 * An administrator who is told "feedback 42 is wrong" has to be able to find
 * row 42, so the page shows each submission's id: a column in the table, a tag
 * on the stacked card, and a line in the View dialog. The member's own page
 * (`showEmployee` off) shows it too: it is the id the status email quotes.
 * The real `FeedbackTable` is rendered; nothing is faked but the Feedback
 * toast provider.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { Feedback } from '../../../store/api/feedbackApi';

vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { FeedbackTable } from '../FeedbackTable';

const row = (id: number): Feedback => ({
  id, employee_id: 7, employee_name: 'Alice Example', category: 'report_a_problem',
  message: `Message number ${id}`, status: 'new', created_at: '2026-10-01T05:00:00+00:00',
  updated_at: null,
});

describe('FeedbackTable id', () => {
  let container: HTMLDivElement;
  let root: Root;

  const render = async (props: Partial<React.ComponentProps<typeof FeedbackTable>> = {}) => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(
        <Provider store={store}>
          <FeedbackTable items={[row(42), row(7)]} showActions canManage {...props} />
        </Provider>,
      );
    });
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it('has an ID column, one id per row, in the same order as the rows', async () => {
    await render();
    const headers = Array.from(container.querySelectorAll('thead th')).map((th) => th.textContent);
    expect(headers.slice(0, 2)).toEqual(['ID', 'Employee']);
    const ids = Array.from(container.querySelectorAll('tbody tr')).map((tr) => tr.querySelector('td')!.textContent);
    expect(ids).toEqual(['#42', '#7']);
    // The colgroup has one entry per column, or the fixed layout shifts every column.
    expect(container.querySelectorAll('colgroup col')).toHaveLength(headers.length);
  });

  it('tags the stacked card with the id', async () => {
    await render();
    const cards = container.querySelector('.md\\:hidden') as HTMLElement;
    expect(cards.textContent).toContain('#42');
    expect(cards.textContent).toContain('#7');
  });

  it('names the feedback in its View dialog', async () => {
    await render();
    const manage = Array.from(container.querySelectorAll('tbody button'))
      .find((b) => b.textContent?.trim() === 'Manage') as HTMLButtonElement;
    await act(async () => { manage.click(); });
    const view = Array.from(document.querySelectorAll('[role="menuitem"]'))
      .find((b) => b.textContent === 'View') as HTMLButtonElement;
    await act(async () => { view.click(); });
    expect(document.querySelector('h2')!.closest('.rounded-2xl')!.textContent).toContain('Feedback #42');
  });

  it('shows the id on the member page too, matching the id in the status email', async () => {
    await render({ showEmployee: false, showActions: false, canManage: false });
    const headers = Array.from(container.querySelectorAll('thead th')).map((th) => th.textContent);
    expect(headers[0]).toBe('ID');
    expect(headers).not.toContain('Employee');
    const ids = Array.from(container.querySelectorAll('tbody tr')).map((tr) => tr.querySelector('td')!.textContent);
    expect(ids).toEqual(['#42', '#7']);
    expect(container.querySelectorAll('colgroup col')).toHaveLength(headers.length);
    expect((container.querySelector('.md\\:hidden') as HTMLElement).textContent).toContain('#42');
  });
});
