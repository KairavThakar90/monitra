import React, { useEffect, useRef, useState } from 'react';
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
 * There is deliberately no close button. The notice stays for as long as the
 * administrator keeps it on; hiding it would only hide a fact that is still
 * true. It is small and in a corner precisely so that leaving it costs
 * nothing.
 *
 * The `FeedbackProvider` toasts stack at the top-right; this sits at the
 * bottom-right and one layer beneath them, so a "saved" toast is never hidden
 * behind the notice and the notice never hides one.
 */

/** The card. Presentation only. */
export const MaintenanceToastCard: React.FC = () => (
  <div
    data-testid="maintenance-toast"
    role="status"
    aria-live="polite"
    className="pointer-events-none fixed bottom-5 right-5 z-[90] w-[min(380px,calc(100vw-2rem))]"
  >
    <div
      className="pointer-events-auto flex items-start gap-4 rounded-xl border border-slate-200 bg-white p-4 shadow-2xl"
      style={{ borderLeft: '4px solid #EF4444' }}
    >
      <img
        src="/logo.png"
        alt="Monitra"
        className="mt-0.5 h-9 w-9 shrink-0 object-contain"
      />
      <div className="min-w-0 flex-1">
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
  </div>
);

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
