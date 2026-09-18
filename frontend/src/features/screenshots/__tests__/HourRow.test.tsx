// @vitest-environment jsdom
/**
 * `WindowCard` names the project and task its cover screenshot was captured
 * under.
 *
 * The card shows one "cover" screenshot per window (the first one) already,
 * for the thumbnail; this reads the same screenshot's `project_name` /
 * `task_name` -- resolved server-side, never guessed here -- rather than
 * inventing a second notion of "the window's task". A screenshot whose
 * entry, task or project has since been deleted carries `null`, and that is
 * said plainly rather than left blank.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { HourRow } from '../HourRow';
import type { HourBlock } from '../hours';
import type { ScreenshotTimelineWindow, ScreenshotView } from '../../../store/api/screenshotsApi';

const shot = (overrides: Partial<ScreenshotView> = {}): ScreenshotView => ({
  id: 1,
  captured_at: '2026-09-07T14:04:00+00:00',
  monitor_number: 1,
  display_count: 1,
  width: 1000,
  height: 1000,
  file_size_bytes: 26114,
  view_url: '/time-entry-screenshots/1/view',
  task_id: 7,
  task_name: 'Reviewing Client Updates',
  project_id: 5,
  project_name: 'Neurodivergent Insights',
  ...overrides,
});

const windowFixture = (screenshots: ScreenshotView[]): ScreenshotTimelineWindow => ({
  window_start: '2026-09-07T14:00:00+00:00',
  window_end: '2026-09-07T14:10:00+00:00',
  activity_percentage: 62,
  activity_measured_seconds: 120,
  tracked_seconds: 600,
  screenshots,
  screenshot_count: screenshots.length,
});

const block = (windows: ScreenshotTimelineWindow[]): HourBlock => ({
  key: '2026-09-07 14',
  startLabel: '2:00 pm',
  endLabel: '3:00 pm',
  trackedSeconds: 600,
  screenshotCount: windows.reduce((sum, w) => sum + w.screenshot_count, 0),
  windows,
});

describe('WindowCard (via HourRow)', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('no network in this test'))));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => { root.unmount(); });
    container.remove();
    vi.unstubAllGlobals();
  });

  const render = async (windows: ScreenshotTimelineWindow[]) => {
    await act(async () => {
      root.render(
        <HourRow block={block(windows)} subjectName="Alex" onOpen={() => {}} />,
      );
    });
  };

  it('shows the cover screenshot\'s project and task', async () => {
    await render([windowFixture([shot()])]);
    expect(container.textContent).toContain('Neurodivergent Insights');
    expect(container.textContent).toContain('Reviewing Client Updates');
  });

  it('says plainly when no project or task was recorded, rather than nothing', async () => {
    await render([
      windowFixture([shot({ task_id: null, task_name: null, project_id: null, project_name: null })]),
    ]);
    expect(container.textContent).toContain('No project recorded');
    expect(container.textContent).toContain('No task recorded');
  });

  it('shows nothing about a task or project for a window with no capture at all', async () => {
    await render([windowFixture([])]);
    expect(container.textContent).not.toContain('recorded');
    expect(container.textContent).toContain('No capture');
  });

  it('takes the label from the cover screenshot, not the window', async () => {
    // Two captures in one window (a future multi-per-window setting): the
    // label follows the cover (the first), matching the thumbnail it
    // labels, even if a later capture in the same window belonged to a
    // different task.
    await render([
      windowFixture([
        shot({ id: 1, task_name: 'Reviewing Client Updates', project_name: 'Neurodivergent Insights' }),
        shot({ id: 2, task_name: 'Something Else', project_name: 'Another Project' }),
      ]),
    ]);
    expect(container.textContent).toContain('Reviewing Client Updates');
    expect(container.textContent).not.toContain('Something Else');
  });
});
