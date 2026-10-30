// @vitest-environment jsdom
/**
 * "Change Project": move a recorded entry whole, or split it between projects.
 *
 * The real modal is rendered against a real RTK Query store with only `fetch`
 * replaced, so what is pinned is the request a browser would make:
 *
 * - the whole-entry flow is unchanged: POST /transfer with the destination only;
 * - "Split between projects" sends POST /split with `move_seconds` and the
 *   destination -- never a start, end or total, so it cannot create time;
 * - Half, and the hours/minutes/seconds boxes, drive a preview of what stays and
 *   what moves, with the IST clock times of each half;
 * - nothing is sent for a missing destination, the same destination, no time, more
 *   time than can move, or a box that is not a whole number in range;
 * - the most that can move is the entry's net time less one second, so a deduction
 *   and the "some time must stay" rule are both honoured, and a manual or very short
 *   entry offers no split;
 * - a refusal from the server is shown in the modal, which stays open.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

import { baseApi } from '../../../store/api/baseApi';
import type { TimeEntry } from '../../../store/api/timeEntryApi';

const showToast = vi.fn();
vi.mock('../../../components/FeedbackProvider', () => ({
  useFeedback: () => ({ showToast, confirmAction: async () => true }),
}));

import { TransferEntryModal } from '../TransferEntryModal';

type Body = Record<string, unknown>;
type Call = { method: string; path: string; body: Body | null };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const projects = [
  { id: 2, project_name: 'Project A', tasks: [{ id: 3, name: 'Design' }, { id: 4, name: 'Review' }] },
  { id: 7, project_name: 'Project B', tasks: [{ id: 8, name: 'Build' }] },
];

// 09:10-10:10 IST, one hour.
const entry = (extra: Partial<TimeEntry> = {}): TimeEntry => ({
  id: 11, organization_id: 1, user_id: 54, project_id: 2, task_id: 3,
  start_time: '2026-10-27T03:40:00Z', end_time: '2026-10-27T04:40:00Z',
  total_seconds: 3600, status: 'stopped', is_manual: false, is_billable: true,
  description: null, client_op: null, created_at: '', updated_at: '',
  adjustment_seconds: 0, net_seconds: 3600, is_running: false,
  elapsed_seconds: 3600, elapsed_time: '01:00:00',
  ...extra,
});

describe('TransferEntryModal', () => {
  let container: HTMLDivElement;
  let root: Root;
  let calls: Call[];
  let refuseNext: { status: number; detail: string } | null;
  let onClose: Mock<() => void>;

  const settle = async () => {
    for (let i = 0; i < 6; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  };
  const click = async (el: Element | null | undefined) => {
    expect(el).toBeTruthy();
    await act(async () => { (el as HTMLElement).click(); });
    await settle();
  };
  const type = async (el: HTMLInputElement | null, value: string) => {
    expect(el).toBeTruthy();
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(el, value);
    await act(async () => { el!.dispatchEvent(new Event('input', { bubbles: true })); });
  };
  const choose = async (el: HTMLSelectElement, value: string) => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(el, value);
    await act(async () => { el.dispatchEvent(new Event('change', { bubbles: true })); });
  };

  const body = () => document.body;
  const text = () => body().textContent ?? '';
  const button = (label: string) =>
    Array.from(body().querySelectorAll('button')).find((b) => b.textContent?.trim() === label) as HTMLButtonElement | undefined;
  const radio = (label: string) =>
    Array.from(body().querySelectorAll('[role="radio"]')).find((b) => b.textContent?.trim() === label) as HTMLButtonElement;
  const box = (name: string) => body().querySelector(`input[aria-label="${name} to move"]`) as HTMLInputElement | null;
  const selects = () => Array.from(body().querySelectorAll('select')) as HTMLSelectElement[];
  const writes = () => calls.filter((c) => c.method !== 'GET');

  const mount = async (value: TimeEntry = entry()) => {
    const store = configureStore({
      reducer: { [baseApi.reducerPath]: baseApi.reducer },
      middleware: (getDefault) => getDefault({ serializableCheck: false }).concat(baseApi.middleware),
    });
    await act(async () => {
      root.render(<Provider store={store}><TransferEntryModal entry={value} projects={projects} onClose={onClose} /></Provider>);
    });
    await settle();
  };
  const pickDestination = async (projectId = '7', taskId = '8') => {
    await choose(selects()[0], projectId);
    await choose(selects()[1], taskId);
  };

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    showToast.mockClear();
    onClose = vi.fn<() => void>();
    calls = [];
    refuseNext = null;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), 'http://localhost');
      const method = (request?.method ?? init?.method ?? 'GET').toUpperCase();
      const raw = request ? await request.text() : (init?.body as string | undefined);
      calls.push({ method, path: url.pathname, body: raw ? (JSON.parse(raw) as Body) : null });
      if (refuseNext) {
        const refusal = refuseNext;
        refuseNext = null;
        return json({ detail: refusal.detail }, refusal.status);
      }
      if (url.pathname.endsWith('/split')) {
        return json({ original: entry({ total_seconds: 1800 }), created: entry({ id: 12, project_id: 7, task_id: 8, total_seconds: 1800 }) });
      }
      return json(entry({ project_id: 7, task_id: 8 }));
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

  it('starts as the whole-entry move, exactly as before', async () => {
    await mount();

    expect(radio('Move whole entry').getAttribute('aria-checked')).toBe('true');
    expect(radio('Split between projects').getAttribute('aria-checked')).toBe('false');
    expect(text()).toContain('Change Project');
    expect(text()).toContain('The recorded time and duration do not change.');
    expect(text()).toContain('Currently: Project A / Design');
    expect(box('Hours')).toBeNull();
    expect(button('Confirm transfer')).toBeTruthy();
  });

  it('moving the whole entry sends only the destination', async () => {
    await mount();
    await pickDestination();
    await click(button('Confirm transfer'));

    expect(writes()).toEqual([
      { method: 'POST', path: expect.stringMatching(/\/time-entries\/11\/transfer$/), body: { to_project_id: 7, to_task_id: 8 } },
    ]);
    expect(showToast).toHaveBeenCalledWith('Time entry transferred.', 'success');
    expect(onClose).toHaveBeenCalled();
  });

  it('split mode shows the time boxes, the split wording and the Confirm split button', async () => {
    await mount();
    await click(radio('Split between projects'));

    expect(radio('Split between projects').getAttribute('aria-checked')).toBe('true');
    expect(text()).toContain('Together they add up to exactly the time you recorded.');
    expect(text()).not.toContain('The recorded time and duration do not change.');
    expect(box('Hours')).toBeTruthy();
    expect(box('Minutes')).toBeTruthy();
    expect(box('Seconds')).toBeTruthy();
    expect(button('Confirm split')).toBeTruthy();
    expect(button('Confirm transfer')).toBeUndefined();
  });

  it('Half fills 00:30:00 and previews what stays and what moves, with IST times', async () => {
    await mount();
    await click(radio('Split between projects'));
    await pickDestination();
    await click(button('Half'));

    expect(box('Hours')!.value).toBe('0');
    expect(box('Minutes')!.value).toBe('30');
    expect(box('Seconds')!.value).toBe('0');
    const preview = text();
    expect(preview).toContain('Stays on Project A / Design');
    expect(preview).toContain('00:30:00');
    expect(preview).toContain('09:10–09:40');
    expect(preview).toContain('Moves to Project B / Build');
    expect(preview).toContain('09:40–10:10');
  });

  it('splits one hour into 30 minutes and 30 minutes with exactly {to_project_id, to_task_id, move_seconds, reason}', async () => {
    await mount();
    await click(radio('Split between projects'));
    await pickDestination();
    await type(box('Minutes'), '30');
    await type(body().querySelector('input[placeholder^="e.g."]') as HTMLInputElement, 'helped on B');
    await click(button('Confirm split'));

    expect(writes()).toHaveLength(1);
    expect(writes()[0].method).toBe('POST');
    expect(writes()[0].path).toMatch(/\/time-entries\/11\/split$/);
    expect(writes()[0].body).toEqual({ to_project_id: 7, to_task_id: 8, move_seconds: 1800, reason: 'helped on B' });
    // Nothing that could rewrite the recorded time is ever sent.
    ['start_time', 'end_time', 'total_seconds', 'duration'].forEach((key) => expect(Object.keys(writes()[0].body!)).not.toContain(key));
    expect(showToast).toHaveBeenCalledWith('Moved 00:30:00 to Project B / Build.', 'success');
    expect(onClose).toHaveBeenCalled();
  });

  it('adds hours, minutes and seconds', async () => {
    await mount(entry({ total_seconds: 12645, net_seconds: 12645, elapsed_seconds: 12645, elapsed_time: '03:30:45' }));
    await click(radio('Split between projects'));
    await pickDestination();
    await type(box('Hours'), '1');
    await type(box('Minutes'), '15');
    await type(box('Seconds'), '10');
    await click(button('Confirm split'));

    expect(writes()[0].body).toMatchObject({ move_seconds: 3600 + 15 * 60 + 10 });
  });

  it('a destination task in the same project is a valid split target', async () => {
    await mount();
    await click(radio('Split between projects'));
    await pickDestination('2', '4');
    await type(box('Minutes'), '10');
    await click(button('Confirm split'));

    expect(writes()[0].body).toMatchObject({ to_project_id: 2, to_task_id: 4, move_seconds: 600 });
  });

  it('sends nothing without a destination', async () => {
    await mount();
    await click(radio('Split between projects'));
    await type(box('Minutes'), '10');
    await click(button('Confirm split'));

    expect(writes()).toHaveLength(0);
    expect(text()).toContain('Choose a destination project and task.');
  });

  it('refuses the project and task the entry already has', async () => {
    await mount();
    await click(radio('Split between projects'));
    await pickDestination('2', '3');
    await type(box('Minutes'), '10');
    await click(button('Confirm split'));

    expect(writes()).toHaveLength(0);
    expect(text()).toContain('Choose a different project or task to move that time to.');
  });

  it('sends nothing when no time has been entered', async () => {
    await mount();
    await click(radio('Split between projects'));
    await pickDestination();
    await click(button('Confirm split'));

    expect(writes()).toHaveLength(0);
    expect(text()).toContain('Enter how much time to move to the other project.');
  });

  it('the whole entry cannot be moved: some time has to stay', async () => {
    await mount();
    await click(radio('Split between projects'));
    await pickDestination();
    await type(box('Hours'), '1');
    await click(button('Confirm split'));

    expect(writes()).toHaveLength(0);
    expect(text()).toContain('You can move at most 00:59:59');
  });

  it('what is already deducted from the entry caps how much can move', async () => {
    await mount(entry({ adjustment_seconds: -1800, net_seconds: 1800 }));
    await click(radio('Split between projects'));
    await pickDestination();
    await type(box('Minutes'), '31');
    expect(text()).toContain('At most 00:30:00 can be moved');
    await click(button('Confirm split'));
    expect(writes()).toHaveLength(0);

    await type(box('Minutes'), '30');
    await click(button('Confirm split'));
    expect(writes()[0].body).toMatchObject({ move_seconds: 1800 });
  });

  it.each([
    ['minutes over 59', 'Minutes', '75', 'Minutes must be at most 59.'],
    ['seconds over 59', 'Seconds', '60', 'Seconds must be at most 59.'],
    ['a negative number', 'Minutes', '-5', 'Minutes must be at least 0.'],
    ['text', 'Minutes', 'abc', 'Minutes must be a whole number.'],
    ['a decimal', 'Hours', '1.5', 'Hours must be a whole number.'],
    ['an empty box', 'Minutes', '', 'Minutes must be a whole number.'],
  ])('refuses %s in words and sends nothing', async (_name, boxName, value, message) => {
    await mount();
    await click(radio('Split between projects'));
    await pickDestination();
    await type(box(boxName), value);
    expect(text()).toContain(message);
    await click(button('Confirm split'));

    expect(writes()).toHaveLength(0);
    expect(text()).toContain(message);
  });

  it('shows the server\'s refusal and stays open', async () => {
    await mount();
    await click(radio('Split between projects'));
    await pickDestination();
    await type(box('Minutes'), '30');
    refuseNext = { status: 409, detail: 'This entry changed while you were splitting it. Reload and try again.' };
    await click(button('Confirm split'));

    expect(text()).toContain('This entry changed while you were splitting it. Reload and try again.');
    expect(onClose).not.toHaveBeenCalled();
    expect(showToast).not.toHaveBeenCalled();
  });

  it('a manual entry can be split like any other (the 00:50:45, 10:10-11:00 entry that was refused)', async () => {
    await mount(entry({
      is_manual: true, total_seconds: 3045, net_seconds: 3045, elapsed_seconds: 3045, elapsed_time: '00:50:45',
      start_time: '2026-10-27T04:40:00Z', end_time: '2026-10-27T05:30:00Z',
    }));

    expect(radio('Split between projects').disabled).toBe(false);
    expect(text()).not.toContain('can only be moved whole');
    await click(radio('Split between projects'));
    expect(radio('Split between projects').getAttribute('aria-checked')).toBe('true');
    await pickDestination();
    await click(button('Half'));
    await click(button('Confirm split'));

    expect(writes()).toHaveLength(1);
    expect(writes()[0].path).toMatch(/\/time-entries\/11\/split$/);
    expect(writes()[0].body).toEqual({ to_project_id: 7, to_task_id: 8, move_seconds: 1522 });
  });

  it('offers no split for an entry too short to split', async () => {
    await mount(entry({ total_seconds: 1, net_seconds: 1, elapsed_seconds: 1, elapsed_time: '00:00:01' }));

    expect(radio('Split between projects').disabled).toBe(true);
    expect(text()).toContain('This entry is too short to split.');
  });

  it('keeps the destination and reason when switching between the two modes', async () => {
    await mount();
    await pickDestination();
    await type(body().querySelector('input[placeholder^="e.g."]') as HTMLInputElement, 'because');
    await click(radio('Split between projects'));

    expect(selects()[0].value).toBe('7');
    expect(selects()[1].value).toBe('8');
    expect((body().querySelector('input[placeholder^="e.g."]') as HTMLInputElement).value).toBe('because');
  });

  it('Cancel closes without sending anything', async () => {
    await mount();
    await click(button('Cancel'));

    expect(onClose).toHaveBeenCalled();
    expect(writes()).toHaveLength(0);
  });
});
