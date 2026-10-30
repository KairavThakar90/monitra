import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
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
 *
 * Zoom (`ZoomableImage`) is for reading small text on a captured screen. It is
 * the same for everyone who can open a screenshot -- an administrator, a member
 * looking at their own day, a client -- because it only changes how the picture
 * is drawn, never what is fetched. It starts at "fit" (100%) for every
 * screenshot: the next capture never inherits the previous one's zoom or
 * position, for the reason the caption never inherits anything either.
 */

/** Fit-to-screen is the smallest zoom: "zoom out" returns to the whole picture. */
export const MIN_ZOOM = 1;
export const MAX_ZOOM = 5;
export const ZOOM_STEP = 0.25;
/** What a double-click zooms to from fit. */
const DOUBLE_CLICK_ZOOM = 2;

// Three decimals, not two: a trackpad sends wheel deltas of 1-3, each worth well under a
// hundredth, and rounding every one of them back to where it started would make it
// impossible to zoom with one.
const clampZoom = (value: number) => Math.round(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value)) * 1000) / 1000;
const clamp = (value: number, limit: number) => Math.min(limit, Math.max(-limit, value));

type Point = { x: number; y: number };

const ZoomButton: React.FC<{
  label: string;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}> = ({ label, disabled = false, onClick, children }) => (
  <button
    type="button"
    aria-label={label}
    title={label}
    disabled={disabled}
    onClick={onClick}
    className={
      'flex h-8 min-w-8 items-center justify-center rounded-lg border border-white/20 px-2 text-sm font-bold transition ' +
      (disabled ? 'cursor-not-allowed bg-white/5 text-white/25' : 'bg-white/10 text-white hover:bg-white/25')
    }
  >
    {children}
  </button>
);

/**
 * The picture, with zoom: buttons, the keyboard (+ / - / 0), the mouse wheel
 * (anchored on the pointer), double-click, and drag to move once zoomed.
 *
 * Zoom is drawn with a CSS transform on a wrapper, so the image is fetched once
 * and never re-requested. The picture can be moved only as far as its edges:
 * dragging it out of the viewport would leave nothing to look at.
 */
const ZoomableImage: React.FC<{ url: string; alt: string }> = ({ url, alt }) => {
  const [zoom, setZoom] = useState(MIN_ZOOM);
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);

  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  // The values the native listeners (wheel, keyboard) and the drag read. State
  // is only what is drawn; these are what is true *now*, between renders.
  const current = useRef<{ zoom: number; pan: Point }>({ zoom: MIN_ZOOM, pan: { x: 0, y: 0 } });
  const drag = useRef<{ startX: number; startY: number; originX: number; originY: number } | null>(null);

  /** How far the picture may move from centre at a given zoom, per axis. */
  const limits = useCallback((atZoom: number): Point => {
    const frame = viewport.current;
    const box = content.current;
    if (!frame || !box) return { x: 0, y: 0 };
    const picture = (box.querySelector('img') as HTMLElement | null) ?? box;
    const bounds = frame.getBoundingClientRect();
    return {
      x: Math.max(0, (picture.offsetWidth * atZoom - bounds.width) / 2),
      y: Math.max(0, (picture.offsetHeight * atZoom - bounds.height) / 2),
    };
  }, []);

  const commit = useCallback((nextZoom: number, nextPan: Point) => {
    current.current = { zoom: nextZoom, pan: nextPan };
    setZoom(nextZoom);
    setPan(nextPan);
  }, []);

  /** Zoom to `target`, keeping the point under `anchor` (relative to the centre) where it is. */
  const zoomTo = useCallback(
    (target: number, anchor?: Point) => {
      const { zoom: before, pan: from } = current.current;
      const next = clampZoom(target);
      if (next === before) return;
      if (next === MIN_ZOOM) {
        commit(MIN_ZOOM, { x: 0, y: 0 });
        return;
      }
      const scale = next / before;
      const wanted = anchor
        ? { x: anchor.x - (anchor.x - from.x) * scale, y: anchor.y - (anchor.y - from.y) * scale }
        : { x: from.x * scale, y: from.y * scale };
      const room = limits(next);
      commit(next, { x: clamp(wanted.x, room.x), y: clamp(wanted.y, room.y) });
    },
    [commit, limits],
  );

  // The frame grows to fill the screen once zoomed, which changes how far the
  // picture may move; settle the position after the layout has.
  useLayoutEffect(() => {
    const room = limits(zoom);
    const { pan: held } = current.current;
    const settled = { x: clamp(held.x, room.x), y: clamp(held.y, room.y) };
    if (settled.x !== held.x || settled.y !== held.y) commit(zoom, settled);
  }, [zoom, limits, commit]);

  const anchorOf = (clientX: number, clientY: number): Point => {
    const bounds = viewport.current?.getBoundingClientRect();
    if (!bounds) return { x: 0, y: 0 };
    return { x: clientX - (bounds.left + bounds.width / 2), y: clientY - (bounds.top + bounds.height / 2) };
  };

  // Wheel: a native, non-passive listener, because the page behind the viewer
  // must not scroll while the picture is being zoomed and React's own wheel
  // handler is passive.
  useEffect(() => {
    const frame = viewport.current;
    if (!frame) return undefined;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const delta = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
      zoomTo(current.current.zoom * Math.exp(-delta * 0.002), anchorOf(event.clientX, event.clientY));
    };
    frame.addEventListener('wheel', onWheel, { passive: false });
    return () => frame.removeEventListener('wheel', onWheel);
  }, [zoomTo]);

  // Keyboard, bound to the document like the arrows are: the viewer has not
  // necessarily clicked inside it. Ctrl/Cmd/Alt chords are left to the browser.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === '+' || event.key === '=') zoomTo(current.current.zoom + ZOOM_STEP);
      else if (event.key === '-' || event.key === '_') zoomTo(current.current.zoom - ZOOM_STEP);
      else if (event.key === '0') zoomTo(MIN_ZOOM);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [zoomTo]);

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (current.current.zoom <= MIN_ZOOM || event.button !== 0) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    drag.current = {
      startX: event.clientX,
      startY: event.clientY,
      originX: current.current.pan.x,
      originY: current.current.pan.y,
    };
    setDragging(true);
  };
  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const grab = drag.current;
    if (!grab) return;
    const room = limits(current.current.zoom);
    commit(current.current.zoom, {
      x: clamp(grab.originX + event.clientX - grab.startX, room.x),
      y: clamp(grab.originY + event.clientY - grab.startY, room.y),
    });
  };
  const endDrag = () => {
    drag.current = null;
    setDragging(false);
  };

  const zoomedIn = zoom > MIN_ZOOM;

  return (
    <div>
      <div
        ref={viewport}
        data-testid="zoom-viewport"
        className={
          'relative mx-auto flex w-full items-center justify-center overflow-hidden rounded-lg ' +
          (zoomedIn ? 'h-[74vh] ' + (dragging ? 'cursor-grabbing' : 'cursor-grab') : 'cursor-zoom-in')
        }
        style={{ touchAction: 'none' }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={(event) =>
          zoomTo(zoomedIn ? MIN_ZOOM : DOUBLE_CLICK_ZOOM, anchorOf(event.clientX, event.clientY))
        }
      >
        <div
          ref={content}
          data-testid="zoom-content"
          className="w-full"
          style={{
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
            transformOrigin: 'center',
            transition: dragging ? 'none' : 'transform 120ms ease-out',
          }}
        >
          <AuthedImage
            url={url}
            alt={alt}
            className="mx-auto max-h-[74vh] w-auto select-none rounded-lg object-contain shadow-2xl"
            frameClassName="h-[60vh] w-full rounded-lg"
          />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-center gap-2" role="group" aria-label="Zoom">
        <ZoomButton label="Zoom out" disabled={zoom <= MIN_ZOOM} onClick={() => zoomTo(zoom - ZOOM_STEP)}>
          &minus;
        </ZoomButton>
        <span className="min-w-[3.5rem] text-center text-xs font-bold tabular-nums text-white" aria-live="polite">
          {Math.round(zoom * 100)}%
        </span>
        <ZoomButton label="Zoom in" disabled={zoom >= MAX_ZOOM} onClick={() => zoomTo(zoom + ZOOM_STEP)}>
          +
        </ZoomButton>
        <ZoomButton label="Reset zoom" disabled={!zoomedIn} onClick={() => zoomTo(MIN_ZOOM)}>
          Fit
        </ZoomButton>
        <span className="hidden text-[11px] font-medium text-[#94A3B8] sm:inline">
          Scroll or +/&minus; to zoom &middot; drag to move &middot; double-click to zoom
        </span>
      </div>
    </div>
  );
};

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
            the fetch -- and the zoom -- instead of leaving the previous image
            under a new caption, or at the previous image's magnification. */}
        <ZoomableImage
          key={shot.id}
          url={viewUrl(shot.id)}
          alt={`Screen of ${subjectName} captured at ${formatISTTime12(shot.captured_at)}`}
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
