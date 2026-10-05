// @vitest-environment jsdom
/**
 * The Project Management page has no export.
 *
 * The "Export CSV" button, and the "Export Projects" dialog it opened for
 * choosing columns, were removed on purpose. Pinned so they do not come back
 * by accident, and so removing them did not take the filters beside them
 * with it: search, status, billing and member filters, and the Columns picker,
 * are all still there.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';

vi.mock('../../dashboard/v2/V2Shell', () => ({
  V2Shell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));
vi.mock('../../auth/authContext', () => ({
  useAuth: () => ({ currentUser: { id: 1, role_name: 'administrator', name: 'Admin', permissions: {} } }),
}));
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast: vi.fn(), confirmAction: async () => true }),
}));

import { AdminProjectManagement } from '../AdminProjectManagement';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

describe('Project Management: no export', () => {
  let container: HTMLDivElement;
  let root: Root;
  let projectRequests: URL[];

  const flush = async () => {
    for (let i = 0; i < 5; i += 1) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const buttons = () => Array.from(container.querySelectorAll('button'));
  const buttonText = (text: string) => buttons().find((button) => button.textContent?.trim() === text);

  beforeEach(async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    projectRequests = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      if (url.pathname.endsWith('/project-management/metadata')) {
        return json({ roles: [], project_statuses: [], task_statuses: [] });
      }
      if (url.pathname.endsWith('/projects') && request.method === 'GET') {
        projectRequests.push(url);
        return json({ items: [], pagination: { page: 1, limit: 20, total: 0, total_pages: 0 } });
      }
      if (url.pathname.endsWith('/members')) return json({ items: [], page: 1, limit: 100, total: 0, pages: 1 });
      if (url.pathname.includes('/projects/')) return json([]);
      return json({}, 404);
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(<Provider store={store}><AdminProjectManagement /></Provider>);
    });
    await flush();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('has no Export CSV button', () => {
    expect(buttonText('Export CSV')).toBeUndefined();
    expect(buttonText('Exporting…')).toBeUndefined();
    expect(container.textContent).not.toMatch(/export/i);
  });

  it('opens no export dialog, and there is nothing that would open one', () => {
    expect(container.textContent).not.toContain('Export Projects');
    expect(container.textContent).not.toContain('Choose the columns for your CSV file.');
    expect(container.querySelector('[aria-label="Close export dialog"]')).toBeNull();
  });

  it('asks the API for one page of projects only, never for an export-sized walk', () => {
    expect(projectRequests.length).toBeGreaterThan(0);
    expect(projectRequests.every((url) => url.searchParams.get('limit') !== '100')).toBe(true);
  });

  it('keeps the filters that sat beside the button', () => {
    expect(container.querySelector('input[placeholder*="earch"]')).not.toBeNull();
    expect(container.querySelector('select[aria-label="Filter by billing"]')).not.toBeNull();
    expect(container.textContent).toContain('All Statuses');
    expect(container.textContent).toContain('Columns');
  });
});
