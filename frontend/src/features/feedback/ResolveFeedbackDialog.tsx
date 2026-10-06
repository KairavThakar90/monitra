import React, { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { FieldError, useFormValidation } from '../../validation';
import { resolveDialogCopy, STATUS_MESSAGE_MAX_LENGTH } from './feedbackActions';

/**
 * The dialog the Resolved button opens.
 *
 * Resolving a feedback emails the employee who wrote it, and this is where the
 * administrator may add a few words of their own to that email -- how it was
 * fixed, a question, anything worth discussing. The note is optional: leaving
 * it empty sends the standard update exactly as before.
 *
 * It stands in for the plain "are you sure?" box the button used to show, so it
 * says the same thing first (an email goes to the employee) and only then offers
 * the text box. The text is checked by the shared description rule with the
 * backend's own limit, so what this accepts the API accepts.
 *
 * Rendered by the row that opened it, and mounted only while open, so a note
 * typed for one feedback can never carry over to the next. It is portaled to
 * the page body because the table around it clips and scrolls.
 */
export const ResolveFeedbackDialog: React.FC<{
  employeeName: string;
  /** The request is in flight: the controls lock and the button says so. */
  busy: boolean;
  onCancel: () => void;
  /** The note, or `null` when none was written. */
  onConfirm: (message: string | null) => void;
}> = ({ employeeName, busy, onCancel, onConfirm }) => {
  const copy = resolveDialogCopy(employeeName);
  const [text, setText] = useState('');

  const form = useFormValidation({
    message: { rule: 'description', label: 'Message', maxLength: STATUS_MESSAGE_MAX_LENGTH },
  });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) onCancel();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [busy, onCancel]);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const result = form.validateAll({ message: text });
    if (!result.ok) return;
    const note = typeof result.values.message === 'string' ? result.values.message.trim() : '';
    onConfirm(note || null);
  };

  return createPortal(
    <div
      className="fixed inset-0 z-[110] flex items-center justify-center bg-slate-950/40 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="resolve-feedback-title"
    >
      <form
        onSubmit={submit}
        noValidate
        className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-6 shadow-2xl"
      >
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-emerald-50 text-xl font-black text-emerald-600" aria-hidden="true">
          ✓
        </div>
        <h2 id="resolve-feedback-title" className="mt-4 text-xl font-black text-slate-800">
          {copy.title}
        </h2>
        <p className="mt-2 text-sm leading-6 text-slate-500">{copy.intro}</p>

        <label htmlFor="resolve-feedback-message" className="mt-5 block text-xs font-bold uppercase tracking-wider text-slate-500">
          {copy.label}
        </label>
        <textarea
          id="resolve-feedback-message"
          value={text}
          rows={5}
          autoFocus
          disabled={busy}
          placeholder={copy.placeholder}
          onChange={(event) => setText(event.target.value)}
          onBlur={() => form.validateField('message', text)}
          {...form.fieldProps('message')}
          className="mt-2 w-full resize-none rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 text-sm text-slate-700 outline-none transition focus:border-[#047857] focus:ring-1 focus:ring-[#047857] disabled:bg-slate-50 disabled:text-slate-400"
        />
        <div className="mt-1.5 flex items-start justify-between gap-3">
          <p className="text-[11px] leading-4 text-slate-400">{copy.help}</p>
          <span
            className="shrink-0 text-[11px] font-semibold tabular-nums text-slate-400"
            data-testid="resolve-feedback-count"
          >
            {text.length}/{STATUS_MESSAGE_MAX_LENGTH}
          </span>
        </div>
        <FieldError id={form.errorId('message')} message={form.errors.message} />

        <div className="mt-6 flex justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded-lg border border-slate-200 px-4 py-2.5 text-sm font-bold text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy}
            className="rounded-lg bg-[#047857] px-4 py-2.5 text-sm font-bold text-white shadow-sm transition hover:bg-[#065F46] disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy ? 'Sending…' : copy.confirm}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  );
};
