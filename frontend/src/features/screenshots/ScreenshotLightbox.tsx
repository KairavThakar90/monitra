import React, { useEffect } from 'react';
import { ENDPOINTS } from '../../api/endpoints';
import { AuthedImage } from '../../components/AuthedImage';
import { formatHMS, formatISTDate, formatISTTime12 } from '../../utils/duration';
import { describeDuration } from './hours';
import type { ScreenshotTimelineWindow, ScreenshotView } from '../../store/api/screenshotsApi';

/**
 * The expanded capture, with the rest of the day either side of it.
 *
 * Opening a screenshot used to be a dead end: one image, and a close button to
 * get back to the grid before opening the next one. Reviewing a day means
 * walking it in order, so the whole day is handed in as a list and the arrows
 * (and the arrow keys) move through it.
 *
 * Everything in the caption is re-read from the item that is actually on
 * screen — the capture's own time, its own window, its own activity, whose
 * screen it is. Nothing is carried over from the image the viewer opened at,
 * which is the failure mode this has to avoid: a caption that keeps saying
 * 71% while the picture moves is a false statement about a person.
 */

export interface LightboxItem {
  shot: ScreenshotView;
  /** The capture window this screenshot was taken in. */
  window: ScreenshotTimelineWindow;
  /** Whose screen it is — the caption never guesses this from context. */
  subjectName: string;
}

const Arrow: React.FC<{
  direction: 'prev' | 'next';
  disabled: boolean;
  onClick: () => void;
}> = ({ direction, disabled, onClick }) => (
  <button
    type="button"
    onClick={onClick}
    disabled={disabled}
    aria-label={direction === 'prev' ? 'Previous screenshot' : 'Next screenshot'}
    className={
      'absolute top-1/2 z-10 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full border border-white/20 backdrop-blur transition ' +
      (direction === 'prev' ? 'left-2 sm:-left-16' : 'right-2 sm:-right-16') +
      (disabled
        ? ' cursor-not-allowed bg-white/5 text-white/25'
        : ' bg-white/10 text-white hover:bg-white/25')
    }
  >
    <svg className="h-6 w-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="2"
        d={direction === 'prev' ? 'M15 19l-7-7 7-7' : 'M9 5l7 7-7 7'}
      />
    </svg>
  </button>
);

export const ScreenshotLightbox: React.FC<{
  items: LightboxItem[];
  index: number;
  onIndexChange: (index: number) => void;
  onClose: () => void;
  /**
   * Deletes the capture on screen. Optional, and omitted is the default: the
   * member's own view never passes it, so an employee is offered nothing to
   * delete. The admin screen passes it only when the signed-in user actually
   * holds `screenshots:delete`.
   */
  onDelete?: (shot: ScreenshotView) => void;
  /** True while a delete is in flight, so the button cannot be pressed twice. */
  deleting?: boolean;
  /** Builds the authenticated image URL for a screenshot id. Defaults to the
   * staff `/time-entry-screenshots/{id}/view` route; the client portal's
   * Screenshots page passes its own project-scoped view route instead. */
  viewUrl?: (id: number) => string;
}> = ({
  items,
  index,
  onIndexChange,
  onClose,
  onDelete,
  deleting = false,
  viewUrl = (id) => ENDPOINTS.TIME_ENTRY_SCREENSHOTS.VIEW(id),
}) => {
  const item = items[index];

  // Bound to the document rather than to the dialog: the viewer has not
  // necessarily clicked inside it, and a lightbox that only answers the
  // keyboard after a click is a lightbox that looks broken.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
      if (event.key === 'ArrowLeft' && index > 0) onIndexChange(index - 1);
      if (event.key === 'ArrowRight' && index < items.length - 1) onIndexChange(index + 1);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [index, items.length, onIndexChange, onClose]);

  if (!item) return null;

  const { shot, window: captureWindow, subjectName } = item;
  const measured = captureWindow.activity_measured_seconds;
  const tracked = captureWindow.tracked_seconds ?? 0;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-[#0F172A]/95 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <button
        type="button"
        className="absolute right-6 top-6 z-10 text-white hover:text-[#CBD5E1]"
        onClick={onClose}
        aria-label="Close"
      >
        <svg className="h-8 w-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>

      <div
        className="relative max-h-full w-full max-w-5xl"
        onClick={(event) => event.stopPropagation()}
      >
        <Arrow direction="prev" disabled={index === 0} onClick={() => onIndexChange(index - 1)} />
        <Arrow
          direction="next"
          disabled={index >= items.length - 1}
          onClick={() => onIndexChange(index + 1)}
        />

        <div className="mb-3 text-center">
          <p className="text-sm font-bold text-white">
            {subjectName} · {formatISTTime12(shot.captured_at)} IST
          </p>
          <p className="mt-0.5 text-xs font-medium text-[#94A3B8]">
            {formatISTDate(shot.captured_at)} · {formatISTTime12(captureWindow.window_start)} –{' '}
            {formatISTTime12(captureWindow.window_end)} ·{' '}
            {/* A merged capture is one image of several screens, so naming a
                single monitor number would be wrong. Single-display captures
                keep the label they have always had. */}
            {shot.display_count > 1
              ? `${shot.display_count} displays`
              : `Monitor ${shot.monitor_number}`}
          </p>
        </div>

        {/* Keyed on the screenshot id so moving to the next capture remounts
            the fetch instead of leaving the previous image under a new
            caption. */}
        <AuthedImage
          key={shot.id}
          url={viewUrl(shot.id)}
          alt={`Screen of ${subjectName} captured at ${formatISTTime12(shot.captured_at)}`}
          className="mx-auto max-h-[74vh] w-auto rounded-lg object-contain shadow-2xl"
          frameClassName="h-[60vh] w-full rounded-lg"
        />

        <div className="mt-3 flex flex-wrap items-center justify-center gap-x-5 gap-y-1 text-xs font-semibold text-[#CBD5E1]">
          <span>
            {index + 1} of {items.length}
          </span>
          {measured > 0 ? (
            <span>
              {captureWindow.activity_percentage}% activity of{' '}
              {describeDuration(tracked > 0 ? tracked : measured)}
            </span>
          ) : (
            // A window with no activity rows is not a measured zero.
            <span className="text-[#94A3B8]">Activity not measured</span>
          )}
          {tracked > 0 && <span>Worked {formatHMS(tracked)} in this window</span>}
        </div>

        {/* Sits under the caption rather than beside the close control: a
            destructive action next to "close" is one mis-click from deleting
            evidence somebody meant to dismiss. */}
        {onDelete && (
          <div className="mt-4 flex justify-center">
            <button
              type="button"
              onClick={() => onDelete(shot)}
              disabled={deleting}
              className={
                'flex items-center gap-2 rounded-lg border px-4 py-2 text-xs font-bold transition ' +
                (deleting
                  ? 'cursor-not-allowed border-white/10 bg-white/5 text-white/40'
                  : 'border-rose-400/40 bg-rose-500/15 text-rose-200 hover:bg-rose-500/30')
              }
            >
              <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="2"
                  d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
                />
              </svg>
              {deleting ? 'Deleting…' : 'Delete screenshot'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
};
