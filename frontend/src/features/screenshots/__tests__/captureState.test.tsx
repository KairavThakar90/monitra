// @vitest-environment jsdom
/**
 * A window with no image says what is known about why.
 *
 * "No capture" was the only thing the grid could say, and it read the same
 * whether the screen was never read, the picture was minutes from landing, or
 * Drive had refused it all afternoon. These pin the wording for each state the
 * backend now derives — and that only a window nothing was reported for is still
 * plain "No capture".
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { describeCaptureReason, describeCaptureState } from '../captureState';
import { HourRow } from '../HourRow';
import type { HourBlock } from '../hours';
import type { CaptureState, ScreenshotTimelineWindow } from '../../../store/api/screenshotsApi';

const emptyWindow = (
  state: CaptureState | undefined,
  reason: string | null = null,
  attempts = 0,
): ScreenshotTimelineWindow => ({
  window_start: '2026-09-07T14:00:00+00:00',
  window_end: '2026-09-07T14:10:00+00:00',
  activity_percentage: 62,
  activity_measured_seconds: 120,
  tracked_seconds: 600,
  screenshots: [],
  screenshot_count: 0,
  capture_state: state,
  capture_reason: reason,
  capture_attempts: attempts,
});

describe('describeCaptureState', () => {
  it('keeps plain "No capture" for a window nothing was reported for', () => {
    expect(describeCaptureState(emptyWindow('none'))).toEqual({
      label: 'No capture', detail: null, tone: 'neutral',
    });
  });

  it('reads a response from a backend that predates the field as "none"', () => {
    expect(describeCaptureState(emptyWindow(undefined)).label).toBe('No capture');
  });

  it('does not call an upload that is still on its way "No capture"', () => {
    const view = describeCaptureState(emptyWindow('pending', 'http_502', 9));
    expect(view.label).toBe('Upload pending');
    expect(view.tone).toBe('pending');
    expect(view.detail).toContain('the server answered 502');
    expect(view.detail).toContain('9 attempts');
  });

  it('names a failed capture and why', () => {
    const view = describeCaptureState(emptyWindow('failed', 'screen_unreadable', 6));
    expect(view.label).toBe('Capture failed');
    expect(view.tone).toBe('problem');
    expect(view.detail).toBe('The screen could not be read (6 attempts)');
  });

  it('says screen recording was not granted rather than that nothing happened', () => {
    const view = describeCaptureState(emptyWindow('blocked', 'screen_recording_blocked'));
    expect(view.label).toBe('Capture blocked');
    expect(view.detail).toContain('Screen recording permission');
  });

  it('treats a privacy exclusion as intended, not as a problem', () => {
    const view = describeCaptureState(emptyWindow('excluded', 'privacy_rule'));
    expect(view.label).toBe('Held back by privacy rule');
    expect(view.tone).toBe('neutral');
  });

  it('reports a machine that cannot capture', () => {
    expect(describeCaptureState(emptyWindow('unavailable', 'capture_unavailable')).label)
      .toBe('Capture unavailable');
  });

  it('does not call a window nobody was tracking in a missing capture', () => {
    const view = describeCaptureState(emptyWindow('not_expected'));
    expect(view.label).toBe('No capture expected');
    expect(view.label).not.toBe('No capture');
    expect(view.detail).toBe('No timer was running in this window');
    expect(view.tone).toBe('neutral');
  });

  it('shows a reason code it does not know rather than hiding it', () => {
    expect(describeCaptureReason('brand_new_reason')).toBe('brand new reason');
    expect(describeCaptureReason(null)).toBeNull();
    expect(describeCaptureReason('http_404')).toBe('the server answered 404');
  });

  it('does not claim attempts for a single try', () => {
    expect(describeCaptureState(emptyWindow('failed', 'store_failed', 1)).detail)
      .not.toContain('attempt');
  });
});

describe('the tile', () => {
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

  const render = async (window: ScreenshotTimelineWindow) => {
    const block: HourBlock = {
      key: '2026-09-07 14', startLabel: '2:00 pm', endLabel: '3:00 pm',
      trackedSeconds: 600, screenshotCount: 0, windows: [window],
    };
    await act(async () => {
      root.render(<HourRow block={block} subjectName="Alex" onOpen={() => {}} />);
    });
  };

  it.each([
    ['none', null, 'No capture'],
    ['pending', 'http_503', 'Upload pending'],
    ['failed', 'capture_stuck', 'Capture failed'],
    ['blocked', 'screen_recording_blocked', 'Capture blocked'],
    ['excluded', 'privacy_rule', 'Held back by privacy rule'],
    ['unavailable', 'capture_unavailable', 'Capture unavailable'],
    ['not_expected', null, 'No capture expected'],
  ] as const)('renders the %s state', async (state, reason, label) => {
    await render(emptyWindow(state, reason, 3));
    expect(container.textContent).toContain(label);
    const tile = container.querySelector('[data-capture-state]');
    expect(tile?.getAttribute('data-capture-state')).toBe(state);
  });

  it('puts the reason on the tile, not only in a tooltip', async () => {
    await render(emptyWindow('failed', 'screen_unreadable', 6));
    expect(container.textContent).toContain('The screen could not be read');
  });

  it('still shows the activity of a window whose capture failed', async () => {
    // The activity figure is the evidence the person was working; a failed
    // capture must not hide it.
    await render(emptyWindow('failed', 'screen_unreadable', 6));
    expect(container.textContent).toContain('62%');
  });

  it('never fabricates a picture for a window without one', async () => {
    await render(emptyWindow('failed', 'screen_unreadable', 6));
    expect(container.querySelector('img')).toBeNull();
  });
});
