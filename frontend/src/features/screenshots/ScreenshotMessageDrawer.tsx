import React, { useEffect } from 'react';
import { formatISTDate, formatISTTime12 } from '../../utils/duration';

/**
 * The "message about this screenshot" panel, opened from the message button on
 * a screenshot tile.
 *
 * Messaging is not built yet. The panel exists so the entry point is in place
 * and says so plainly; it sends nothing, stores nothing and asks for nothing.
 * It slides in from the right, like the app's other drawers, and closes with
 * the X, Escape, or a click outside.
 */
export const ScreenshotMessageDrawer: React.FC<{
  open: boolean;
  onClose: () => void;
  /** Whose screenshot, and when it was captured -- shown for context only. */
  subjectName?: string;
  capturedAt?: string;
}> = ({ open, onClose, subjectName, capturedAt }) => {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[60] overflow-hidden">
      <div className="absolute inset-0 bg-slate-900/40 backdrop-blur-sm" onClick={onClose} />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Screenshot message"
        className="absolute inset-y-0 right-0 flex w-full max-w-md flex-col bg-white shadow-2xl"
      >
        <header className="flex items-start justify-between gap-4 border-b border-slate-200 px-6 py-5">
          <div className="min-w-0">
            <h2 className="text-lg font-bold text-slate-800">Screenshot message</h2>
            {subjectName && (
              <p className="mt-0.5 truncate text-xs font-medium text-slate-500">
                {subjectName}
                {capturedAt && ` · ${formatISTDate(capturedAt)}, ${formatISTTime12(capturedAt)} IST`}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-md p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
          >
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeWidth="2.5" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </header>

        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
          <span className="flex h-14 w-14 items-center justify-center rounded-full bg-[#EFF6FF] text-[#2563EB]">
            <svg className="h-7 w-7" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="2"
                d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
              />
            </svg>
          </span>
          <h3 className="text-base font-bold text-slate-800">Screenshot message notice</h3>
          <p className="text-sm font-semibold text-[#2563EB]">Coming soon</p>
          <p className="max-w-xs text-xs leading-relaxed text-slate-500">
            Soon you will be able to send a message about a screenshot to the employee who captured it. This feature
            is not available yet.
          </p>
        </div>
      </aside>
    </div>
  );
};
