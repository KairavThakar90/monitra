import React, { useEffect, useState } from 'react';
import { ENDPOINTS } from '../../api/endpoints';
import { formatApiError } from '../../api/utils';
import { AuthedImage } from '../../components/AuthedImage';
import { useSendScreenshotNoticeMutation } from '../../store/api/screenshotsApi';
import type { ScreenshotView } from '../../store/api/screenshotsApi';
import { formatISTDate, formatISTTime12 } from '../../utils/duration';
import { FieldError, useFormValidation } from '../../validation';

/**
 * The "message about this screenshot" panel, opened from the message button on
 * a screenshot tile.
 *
 * It shows the screenshot and a box for the notice. Submitting emails the
 * screenshot, with the notice, to the employee it belongs to -- the server
 * takes the recipient from the screenshot's own owner, so this panel sends a
 * screenshot id and the words, never an address. Open to Admin, HR and Leader;
 * the tile only offers the button to them, and the server refuses anyone else.
 *
 * It slides in from the right like the app's other drawers, and closes with the
 * X, Escape, or a click outside -- except while a notice is being sent, so a
 * stray click cannot hide the answer to it.
 */

/** Longest notice. Mirrors `SCREENSHOT_NOTICE_MAX_LENGTH` on the backend. */
export const NOTICE_MAX_LENGTH = 1000;

const FORM = {
  message: { rule: 'description', label: 'Message', required: true, maxLength: NOTICE_MAX_LENGTH },
} as const;

export const ScreenshotMessageDrawer: React.FC<{
  open: boolean;
  onClose: () => void;
  /** The employee the screenshot belongs to, shown for context. */
  subjectName?: string;
  /** The screenshot the notice is about. */
  shot?: ScreenshotView | null;
}> = ({ open, onClose, subjectName, shot }) => {
  const [message, setMessage] = useState('');
  const [sent, setSent] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [sendNotice, { isLoading }] = useSendScreenshotNoticeMutation();
  const form = useFormValidation(FORM);

  // A fresh panel for each screenshot: nothing typed for one carries to the next.
  useEffect(() => {
    setMessage('');
    setSent(null);
    setFailure(null);
    form.clear();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, shot?.id]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !isLoading) onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, isLoading, onClose]);

  if (!open) return null;

  const submit = async () => {
    if (!shot || isLoading || sent) return;
    setFailure(null);
    const checked = form.validateAll({ message });
    if (!checked.ok) return;
    try {
      const result = await sendNotice({ id: shot.id, message: checked.values.message as string }).unwrap();
      setSent(result.message);
    } catch (error) {
      const data = (error as { data?: unknown } | null)?.data;
      setFailure(formatApiError(data, 'The notice could not be sent. Please try again.'));
    }
  };

  return (
    <div className="fixed inset-0 z-[60] overflow-hidden">
      <div className="absolute inset-0 bg-slate-900/40 backdrop-blur-sm" onClick={() => !isLoading && onClose()} />
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
                To {subjectName}
                {shot && ` · captured ${formatISTDate(shot.captured_at)}, ${formatISTTime12(shot.captured_at)} IST`}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isLoading}
            aria-label="Close"
            className="rounded-md p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700 disabled:opacity-40"
          >
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeWidth="2.5" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </header>

        <div className="flex-1 space-y-5 overflow-y-auto px-6 py-5">
          {shot && (
            <figure className="overflow-hidden rounded-xl border border-slate-200 bg-[#0F172A]">
              <AuthedImage
                url={ENDPOINTS.TIME_ENTRY_SCREENSHOTS.VIEW(shot.id)}
                alt={`Screenshot ${subjectName ? `of ${subjectName} ` : ''}captured at ${formatISTTime12(shot.captured_at)}`}
                className={`aspect-video w-full ${shot.display_count > 1 ? 'object-contain' : 'object-cover'}`}
                frameClassName="aspect-video w-full"
              />
              {(shot.project_name || shot.task_name) && (
                <figcaption className="bg-white px-3 py-2">
                  {shot.project_name && (
                    <p className="truncate text-[12px] font-semibold text-[#1D4ED8]">{shot.project_name}</p>
                  )}
                  {shot.task_name && <p className="truncate text-[11px] text-[#64748B]">{shot.task_name}</p>}
                </figcaption>
              )}
            </figure>
          )}

          {sent ? (
            <div role="status" className="rounded-xl border border-emerald-200 bg-emerald-50 p-4">
              <p className="text-sm font-bold text-emerald-800">{sent}</p>
              <p className="mt-1 text-xs text-emerald-700">
                They will receive the screenshot and your notice. They can reply to the email to answer you.
              </p>
            </div>
          ) : (
            <div>
              <label htmlFor="screenshot-notice" className="mb-1.5 block text-[11px] font-bold uppercase tracking-wider text-slate-500">
                Notice
              </label>
              <textarea
                id="screenshot-notice"
                value={message}
                rows={6}
                disabled={isLoading}
                placeholder="Write a notice about this screenshot…"
                onChange={(event) => {
                  setMessage(event.target.value);
                  if (failure) setFailure(null);
                  if (form.errors.message) form.clearField('message');
                }}
                onBlur={() => form.validateField('message', message)}
                {...form.fieldProps('message')}
                className="w-full resize-none rounded-lg border border-slate-200 px-3 py-2.5 text-sm text-slate-800 outline-none transition placeholder:text-slate-400 focus:border-[#38BDF8] focus:ring-2 focus:ring-[#38BDF8]/20 disabled:bg-slate-50"
              />
              <div className="mt-1 flex items-start justify-between gap-3">
                <FieldError id={form.errorId('message')} message={form.errors.message} />
                <span className="ml-auto shrink-0 text-[11px] font-medium tabular-nums text-slate-400">
                  {message.length}/{NOTICE_MAX_LENGTH}
                </span>
              </div>
              <p className="mt-2 text-xs text-slate-500">
                This is emailed to {subjectName ?? 'the employee'} together with the screenshot.
              </p>
              {failure && (
                <p role="alert" className="mt-3 rounded-lg bg-[#FEF2F2] px-3 py-2 text-[12px] font-semibold text-[#DC2626]">
                  {failure}
                </p>
              )}
            </div>
          )}
        </div>

        <footer className="flex items-center justify-end gap-2 border-t border-slate-200 bg-slate-50 px-6 py-4">
          {sent ? (
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg bg-[#0F172A] px-4 py-2 text-[13px] font-bold text-white shadow-sm transition hover:bg-[#1E293B]"
            >
              Done
            </button>
          ) : (
            <>
              <button
                type="button"
                onClick={onClose}
                disabled={isLoading}
                className="rounded-lg border border-[#E2E8F0] bg-white px-4 py-2 text-[13px] font-bold text-[#64748B] transition hover:text-[#0F172A] disabled:opacity-40"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void submit()}
                disabled={isLoading || !shot || message.trim().length === 0}
                className="flex items-center gap-1.5 rounded-lg bg-[#2563EB] px-4 py-2 text-[13px] font-bold text-white shadow-sm transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {isLoading ? 'Sending…' : 'Send notice'}
              </button>
            </>
          )}
        </footer>
      </aside>
    </div>
  );
};
