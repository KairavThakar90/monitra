import React from 'react';
import { ENDPOINTS } from '../../api/endpoints';
import { AuthedImage } from '../../components/AuthedImage';
import { formatHMS, formatISTTime12 } from '../../utils/duration';
import { describeDuration } from './hours';
import type { HourBlock } from './hours';
import type { ScreenshotTimelineWindow, ScreenshotView } from '../../store/api/screenshotsApi';

/**
 * One hour of a tracked day: a header saying how much of it was worked, and
 * the hour's capture windows beneath it.
 *
 * Each card is a *window*, not a screenshot. A window can hold several
 * captures — one per monitor now, more if the per-window setting changes — so
 * the card shows the first and says how many there are, and opening it starts
 * the viewer there with the rest of the day behind the arrows.
 */

/** The window's own activity colour — red / amber / green, as elsewhere. */
const activityColor = (percentage: number) =>
  percentage >= 70 ? 'bg-emerald-500' : percentage >= 40 ? 'bg-amber-400' : 'bg-rose-500';

const WindowCard: React.FC<{
  window: ScreenshotTimelineWindow;
  subjectName: string;
  onOpen: (shot: ScreenshotView) => void;
}> = ({ window: captureWindow, subjectName, onOpen }) => {
  const cover = captureWindow.screenshots[0];
  const measured = captureWindow.activity_measured_seconds;
  const tracked = captureWindow.tracked_seconds ?? 0;

  return (
    <figure className="w-[260px] shrink-0 overflow-hidden rounded-xl border border-[#E2E8F0] bg-white shadow-sm transition hover:shadow-md">
      <div className="relative">
        {cover ? (
          <button
            type="button"
            onClick={() => onOpen(cover)}
            className="block w-full cursor-zoom-in"
            title={`Captured at ${formatISTTime12(cover.captured_at)} IST`}
          >
            <AuthedImage
              url={ENDPOINTS.TIME_ENTRY_SCREENSHOTS.VIEW(cover.id)}
              alt={`Screen of ${subjectName} captured at ${formatISTTime12(cover.captured_at)}`}
              className={`aspect-video w-full bg-[#0F172A] ${
                // A merged multi-display capture is far wider than this 16:9
                // tile, so cropping it to fill would hide the outer monitors
                // completely. Single-display captures keep the original fit.
                cover.display_count > 1 ? 'object-contain' : 'object-cover'
              }`}
              frameClassName="aspect-video w-full"
            />
          </button>
        ) : (
          // Tracked time with nothing captured in it. Saying so is the honest
          // answer; a stand-in image would not be.
          <div className="flex aspect-video w-full items-center justify-center bg-[#F1F5F9]">
            <span className="text-[11px] font-semibold text-[#94A3B8]">No capture</span>
          </div>
        )}

        {captureWindow.screenshot_count > 0 && (
          <span className="absolute bottom-2 left-1/2 -translate-x-1/2 rounded-full bg-white px-3 py-1 text-[11px] font-bold text-[#2563EB] shadow">
            {captureWindow.screenshot_count} screen
            {captureWindow.screenshot_count === 1 ? '' : 's'}
          </span>
        )}
      </div>

      <figcaption className="space-y-1.5 px-3 py-2.5">
        <span className="block text-[12px] font-semibold text-[#334155]">
          {formatISTTime12(captureWindow.window_start)} – {formatISTTime12(captureWindow.window_end)}
        </span>

        {measured > 0 ? (
          <>
            <span className="block h-1.5 w-full overflow-hidden rounded-full bg-[#F1F5F9]">
              <span
                className={`block h-full ${activityColor(captureWindow.activity_percentage)}`}
                style={{ width: `${captureWindow.activity_percentage}%` }}
              />
            </span>
            <span className="block text-center text-[11px] font-medium text-[#64748B]">
              {captureWindow.activity_percentage}% of{' '}
              {describeDuration(tracked > 0 ? tracked : measured)}
            </span>
          </>
        ) : (
          // A window with no activity rows is not a measured zero, and printing
          // "0%" here would read as one.
          <span className="block text-center text-[11px] font-medium text-[#94A3B8]">
            Activity not measured
          </span>
        )}
      </figcaption>
    </figure>
  );
};

export const HourRow: React.FC<{
  block: HourBlock;
  subjectName: string;
  onOpen: (shot: ScreenshotView) => void;
}> = ({ block, subjectName, onOpen }) => (
  <div className="relative pl-8">
    <div className="absolute left-0 top-1.5 z-10 h-3 w-3 rounded-full border-2 border-[#CBD5E1] bg-white" />
    <div className="absolute bottom-[-32px] left-[5px] top-4 w-px bg-[#E2E8F0]" />

    <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-1 pt-0.5 text-sm leading-none">
      <span className="font-bold text-[#334155]">
        {block.startLabel} - {block.endLabel}
      </span>
      <span className="text-xs font-medium text-[#64748B]">
        Total time worked:{' '}
        <span className="font-bold text-[#0F172A]">{formatHMS(block.trackedSeconds)}</span>
      </span>
      <span className="text-xs font-medium text-[#94A3B8]">
        {block.screenshotCount} screenshot{block.screenshotCount === 1 ? '' : 's'}
      </span>
    </div>

    <div className="custom-scrollbar flex gap-4 overflow-x-auto pb-4">
      {block.windows.map((window) => (
        <WindowCard
          key={window.window_start}
          window={window}
          subjectName={subjectName}
          onOpen={onOpen}
        />
      ))}
    </div>
  </div>
);
