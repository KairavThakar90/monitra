import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useAuth } from '../features/auth/authContext';
import {
  INITIAL_MAINTENANCE_NOTICE,
  MAINTENANCE_COPY,
  MAINTENANCE_POLL_INTERVAL_MS,
  applyMaintenanceAnswer,
} from '../features/system/maintenance';
import { useGetMaintenanceStatusQuery } from '../store/api/systemApi';

/**
 * The maintenance notice.
 *
 * A fixed card in the bottom-right corner, shown when the backend's
 * maintenance flag turns on and removed when it turns off. It is not a
 * dialog: it has no backdrop, takes no focus, asks for no acknowledgement and
 * covers nothing but its own corner. Everything underneath it -- navigation,
 * timers, reports, forms -- keeps working exactly as before, because nothing
 * here touches any of it.
 *
 * It can be moved out of the way, two ways, because an administrator who
 * works while maintenance is on needs the corner it sits in:
 *
 * - **Minimize** collapses the card to a small "Under maintenance" pill and
 *   the pill expands it again. The notice is never *dismissed*: it used to
 *   have no close control on purpose, since hiding it would only hide a fact
 *   that is still true, and a pill keeps the fact on screen at a fraction of
 *   the size.
 * - **Drag** moves either form anywhere on the screen. It is kept inside the
 *   viewport, also when the window is resized or the card changes size.
 *
 * The position and the minimized state live with the card, so they last for
 * one maintenance window and the next one starts in the corner, open.
 *
 * The `FeedbackProvider` toasts stack at the top-right; this sits at the
 * bottom-right and one layer beneath them, so a "saved" toast is never hidden
 * behind the notice and the notice never hides one.
 */

/** Space kept between the notice and the edge of the screen when it is dragged or re-fitted. */
const EDGE_MARGIN = 8;
/** A press that moves less than this is a click, not the start of a drag. */
const DRAG_THRESHOLD_PX = 4;
/** How long the click that follows a drag is swallowed, for a press-and-release with no pointer-down after it. */
const CLICK_SUPPRESS_MS = 300;

interface Offset {
  x: number;
  y: number;
}

/** `value` held between `low` and `high`; when the box is wider than the room, the low edge wins. */
const clamp = (value: number, low: number, high: number) => (high < low ? low : Math.min(Math.max(value, low), high));

/** A box that has been laid out. jsdom, and an element not yet in the page, report 0 x 0. */
const hasSize = (rect: DOMRect) => rect.width > 0 || rect.height > 0;

/**
 * Drag handling for the notice: pointer events (mouse, touch and pen alike),
 * the offset from its resting corner, and keeping it on screen.
 *
 * Moves and the release are listened for on the `window` for as long as a
 * press lasts, not on the notice. The minimized pill is only 36px tall, so a
 * quick drag leaves it between two move events and the browser then sends the
 * rest of them to whatever is underneath: listening on the pill itself meant
 * such a drag never started. Pointer capture is not used for the same reason
 * it would be wrong here -- capturing at the press redirects the click that
 * follows, and the buttons inside would stop answering a plain click.
 */
function useDraggable() {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [offset, setOffset] = useState<Offset>({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const stopListening = useRef<(() => void) | null>(null);
  const suppressClick = useRef(false);
  const suppressTimer = useRef<number | undefined>(undefined);

  /** Slide the notice back inside the viewport if any part of it is outside. */
  const clampIntoView = useCallback(() => {
    const element = boxRef.current;
    if (!element) return;
    const rect = element.getBoundingClientRect();
    if (!hasSize(rect)) return;
    const dx = clamp(0, EDGE_MARGIN - rect.left, window.innerWidth - EDGE_MARGIN - rect.right);
    const dy = clamp(0, EDGE_MARGIN - rect.top, window.innerHeight - EDGE_MARGIN - rect.bottom);
    if (dx !== 0 || dy !== 0) setOffset((current) => ({ x: current.x + dx, y: current.y + dy }));
  }, []);

  // Leaving mid-press (the notice is removed, the user signs out) must not leave listeners on the window.
  useEffect(
    () => () => {
      stopListening.current?.();
      window.clearTimeout(suppressTimer.current);
    },
    [],
  );

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    const element = boxRef.current;
    if (!element) return;
    // A previous press whose release never reached us (a lost pointer) is over.
    stopListening.current?.();
    window.clearTimeout(suppressTimer.current);
    suppressClick.current = false;

    const press = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      base: offset,
      rect: element.getBoundingClientRect(),
      moved: false,
    };
    const selectBefore = document.body.style.userSelect;

    const move = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== press.id) return;
      let dx = moveEvent.clientX - press.x;
      let dy = moveEvent.clientY - press.y;
      if (!press.moved) {
        if (Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
        press.moved = true;
        setDragging(true);
        // A drag across the page must not select the text it passes over.
        document.body.style.userSelect = 'none';
      }
      if (hasSize(press.rect)) {
        dx = clamp(dx, EDGE_MARGIN - press.rect.left, window.innerWidth - EDGE_MARGIN - press.rect.right);
        dy = clamp(dy, EDGE_MARGIN - press.rect.top, window.innerHeight - EDGE_MARGIN - press.rect.bottom);
      }
      setOffset({ x: press.base.x + dx, y: press.base.y + dy });
    };

    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
      document.body.style.userSelect = selectBefore;
      stopListening.current = null;
    };

    function end(endEvent: PointerEvent) {
      if (endEvent.pointerId !== press.id) return;
      stop();
      if (!press.moved) return;
      setDragging(false);
      // The browser still sends a click after the release if it lands on the
      // pill or a button; it was the end of a drag, not a press on that button.
      suppressClick.current = true;
      suppressTimer.current = window.setTimeout(() => { suppressClick.current = false; }, CLICK_SUPPRESS_MS);
    }

    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
    stopListening.current = stop;
  };

  const onClickCapture = (event: React.MouseEvent<HTMLDivElement>) => {
    if (!suppressClick.current) return;
    suppressClick.current = false;
    event.preventDefault();
    event.stopPropagation();
  };

  return {
    boxRef,
    offset,
    dragging,
    clampIntoView,
    handlers: { onPointerDown, onClickCapture },
  };
}

/** The card. Holds its own position and whether it is minimized; nothing else about it is state. */
export const MaintenanceToastCard: React.FC = () => {
  const [minimized, setMinimized] = useState(false);
  const { boxRef, offset, dragging, clampIntoView, handlers } = useDraggable();

  // The card and the pill are different sizes and both hang from the same
  // bottom-right corner, so growing back into a card can push the top-left of
  // it off a screen the pill fitted on. A resize can do the same.
  useLayoutEffect(() => {
    clampIntoView();
  }, [minimized, clampIntoView]);
  useEffect(() => {
    window.addEventListener('resize', clampIntoView);
    return () => window.removeEventListener('resize', clampIntoView);
  }, [clampIntoView]);

  return (
    <div
      ref={boxRef}
      data-testid="maintenance-toast"
      role="status"
      aria-live="polite"
      className={
        'pointer-events-none fixed bottom-5 right-5 z-[90] ' +
        (minimized ? 'max-w-[calc(100vw-2rem)]' : 'w-[min(380px,calc(100vw-2rem))]')
      }
      style={{ transform: `translate3d(${offset.x}px, ${offset.y}px, 0)` }}
    >
      {/* `touch-action: none`: without it the browser takes a finger's drag as a page scroll. */}
      <div
        {...handlers}
        data-testid="maintenance-drag-area"
        data-dragging={dragging ? 'true' : undefined}
        title="Drag to move"
        style={{ touchAction: 'none' }}
        className={'pointer-events-auto select-none ' + (dragging ? 'cursor-grabbing' : 'cursor-grab')}
      >
        {minimized ? (
          <button
            type="button"
            data-testid="maintenance-expand"
            aria-expanded="false"
            aria-label="Show the maintenance notice"
            onClick={() => setMinimized(false)}
            className="ml-auto flex items-center gap-2 rounded-full border border-rose-200 bg-white py-2 pl-3 pr-4 shadow-xl transition hover:bg-rose-50"
          >
            <span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-full bg-rose-500" />
            <span className="whitespace-nowrap text-[12px] font-bold text-[#0F172A]">Under maintenance</span>
          </button>
        ) : (
          <div
            className="relative flex items-start gap-4 rounded-xl border border-slate-200 bg-white p-4 shadow-2xl"
            style={{ borderLeft: '4px solid #EF4444' }}
          >
            <button
              type="button"
              data-testid="maintenance-minimize"
              aria-expanded="true"
              aria-label="Minimize the maintenance notice"
              title="Minimize"
              onClick={() => setMinimized(true)}
              className="absolute right-2 top-2 flex h-6 w-6 items-center justify-center rounded-md text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
            >
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5} aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 12h14" />
              </svg>
            </button>
            <img
              src="/logo.png"
              alt="Monitra"
              draggable={false}
              className="mt-0.5 h-9 w-9 shrink-0 object-contain"
            />
            <div className="min-w-0 flex-1 pr-5">
              <div className="text-[10px] font-extrabold uppercase tracking-[0.18em] text-[#64748B]">
                {MAINTENANCE_COPY.brand}
              </div>
              <h2 className="mt-1 text-sm font-bold leading-5 text-[#0F172A]">{MAINTENANCE_COPY.title}</h2>
              <p className="mt-1.5 text-[13px] leading-5 text-[#64748B]">{MAINTENANCE_COPY.body}</p>
              <div className="mt-3 flex justify-center">
                <span
                  data-testid="maintenance-status"
                  className="inline-flex items-center gap-2 rounded-full border border-rose-200 bg-rose-50 px-3 py-1 text-[11px] font-black uppercase tracking-[0.14em] text-rose-600"
                >
                  <span aria-hidden="true" className="h-2 w-2 rounded-full bg-rose-500" />
                  {MAINTENANCE_COPY.status}
                </span>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

/**
 * Polls the status while `enabled`, and shows the card on the off->on edge and
 * hides it on the on->off edge -- never on an unchanged answer.
 *
 * RTK Query keeps the last successful `data` through a failed poll, so a
 * backend that cannot be reached leaves the notice exactly where it was. The
 * slice's `refetchOnReconnect` and `refetchOnFocus` (installed in the store)
 * re-ask the moment the browser regains the network or the tab.
 */
export const MaintenanceNotice: React.FC<{ enabled: boolean }> = ({ enabled }) => {
  const { data } = useGetMaintenanceStatusQuery(undefined, {
    skip: !enabled,
    pollingInterval: enabled ? MAINTENANCE_POLL_INTERVAL_MS : 0,
    // A tab nobody is looking at has no use for the answer and was polling anyway.
    skipPollingIfUnfocused: true,
  });
  const [visible, setVisible] = useState(false);
  const noticeRef = useRef(INITIAL_MAINTENANCE_NOTICE);

  useEffect(() => {
    if (!enabled) {
      // Signed out: forget the last answer, so the next session starts from
      // unknown and a notice still on is shown again, once.
      noticeRef.current = INITIAL_MAINTENANCE_NOTICE;
      setVisible(false);
      return;
    }
    const next = applyMaintenanceAnswer(noticeRef.current, data?.maintenance_mode);
    if (next !== noticeRef.current) {
      noticeRef.current = next;
      setVisible(next.visible);
    }
  }, [enabled, data]);

  return visible ? <MaintenanceToastCard /> : null;
};

/** The notice for the signed-in session. Mount once, above the routes. */
export const MaintenanceToast: React.FC = () => {
  const { isAuthenticated } = useAuth();
  return <MaintenanceNotice enabled={isAuthenticated} />;
};
