import React, { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useFeedback } from "../../components/FeedbackProvider";
import type { FeedbackAttachment } from "../../store/api/feedbackApi";
import {
  DOWNLOAD_FAILED_MESSAGE,
  attachmentCountLabel,
  downloadAttachment,
  fetchAttachmentBlob,
  formatFileSize,
} from "./attachmentFiles";

/**
 * The attachments of one feedback: the paperclip chip in the list, and the
 * section, thumbnails and preview in the "Feedback Description" dialog.
 *
 * The list rows hold metadata only and draw no image. Bytes are fetched here,
 * with the bearer token, once per attachment and only once the dialog that
 * shows them is open; the object URL is revoked when the tile goes away. A
 * failed fetch becomes an "Attachment unavailable" tile -- the rest of the
 * feedback stays readable, and Download still tries and says so if it cannot.
 */

const focusRing =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#2563EB]/60 focus-visible:ring-offset-1";

const PaperclipIcon: React.FC<{ className?: string }> = ({ className = "h-3.5 w-3.5" }) => (
  <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={2}
      d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"
    />
  </svg>
);

const FileIcon: React.FC<{ className?: string }> = ({ className = "h-7 w-7" }) => (
  <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={1.6}
      d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
    />
  </svg>
);

/** Rendered only when `count > 0`; the caller decides, so a row without files gains nothing. */
export const AttachmentChip: React.FC<{ count: number; className?: string }> = ({
  count,
  className = "",
}) => (
  <span
    role="img"
    aria-label={attachmentCountLabel(count)}
    className={`inline-flex items-center gap-1 whitespace-nowrap rounded-md bg-[#F1F5F9] px-1.5 py-0.5 text-[11px] font-semibold text-[#64748B] ${className}`}
  >
    <PaperclipIcon />
    {attachmentCountLabel(count)}
  </span>
);

type ImageState =
  | { status: "idle" | "loading" | "failed"; src: null }
  | { status: "ready"; src: string };

/**
 * The attachment's image as an object URL. Keyed on the id, so re-rendering
 * with the same attachment never refetches, and revoked on unmount or change.
 */
const useAttachmentImage = (id: number, enabled: boolean): ImageState => {
  const [state, setState] = useState<{ id: number; value: ImageState } | null>(null);

  useEffect(() => {
    if (!enabled) return undefined;
    const controller = new AbortController();
    let created: string | null = null;
    let cancelled = false;

    fetchAttachmentBlob(id, { signal: controller.signal })
      .then((blob) => {
        if (cancelled) return;
        created = URL.createObjectURL(blob);
        setState({ id, value: { status: "ready", src: created } });
      })
      .catch(() => {
        if (!cancelled) setState({ id, value: { status: "failed", src: null } });
      });

    return () => {
      cancelled = true;
      controller.abort();
      if (created) URL.revokeObjectURL(created);
    };
  }, [id, enabled]);

  if (!enabled) return { status: "idle", src: null };
  // A result for a previous id is not this attachment's result.
  if (!state || state.id !== id) return { status: "loading", src: null };
  return state.value;
};

const FOCUSABLE = 'button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

/**
 * The larger preview. A portal so it sits above the feedback dialog whatever
 * that dialog's overflow or stacking is. Escape and a click on the backdrop
 * close this and only this; focus moves in on open, is kept inside while open
 * and goes back to the control that opened it.
 */
const AttachmentLightbox: React.FC<{
  attachment: FeedbackAttachment;
  image: ImageState;
  downloading: boolean;
  onDownload: () => void;
  onClose: () => void;
}> = ({ attachment, image, downloading, onDownload, onClose }) => {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => {
      if (opener && opener.isConnected) opener.focus();
    };
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        // The feedback dialog underneath must stay open.
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const nodes = dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE);
      if (!nodes || nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !dialogRef.current?.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !dialogRef.current?.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const name = attachment.original_filename;

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-[#0F172A]/90 p-4"
      onClick={(event) => {
        event.stopPropagation();
        onClose();
      }}
      role="presentation"
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Attachment ${name}`}
        className="flex max-h-full w-full max-w-4xl flex-col items-center gap-3"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex w-full min-w-0 items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="truncate text-sm font-bold text-white" title={name}>
              {name}
            </p>
            <p className="text-xs font-medium text-[#94A3B8]">{formatFileSize(attachment.file_size)}</p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={onDownload}
              disabled={downloading}
              aria-label={`Download attachment ${name}`}
              className={`rounded-lg border border-white/25 bg-white/10 px-3 py-1.5 text-xs font-bold text-white transition hover:bg-white/20 disabled:cursor-not-allowed disabled:opacity-60 ${focusRing}`}
            >
              {downloading ? "Downloading…" : "Download"}
            </button>
            <button
              ref={closeRef}
              type="button"
              onClick={onClose}
              aria-label="Close preview"
              className={`rounded-lg p-2 text-white transition hover:bg-white/15 ${focusRing}`}
            >
              <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>

        {image.status === "ready" ? (
          <img
            src={image.src}
            alt={name}
            className="max-h-[78vh] max-w-full rounded-lg object-contain shadow-2xl"
          />
        ) : image.status === "failed" ? (
          <div className="flex h-[40vh] w-full items-center justify-center rounded-lg bg-white/10 text-sm font-semibold text-[#CBD5E1]">
            Attachment unavailable
          </div>
        ) : (
          <div className="h-[40vh] w-full animate-pulse rounded-lg bg-white/10" aria-busy="true" />
        )}
      </div>
    </div>,
    document.body,
  );
};

const AttachmentItem: React.FC<{ attachment: FeedbackAttachment }> = ({ attachment }) => {
  const { showToast } = useFeedback();
  const image = useAttachmentImage(attachment.id, attachment.is_image);
  const [previewing, setPreviewing] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const downloadingRef = useRef(false);

  const name = attachment.original_filename;
  const unavailable = image.status === "failed";

  const download = useCallback(async () => {
    // A ref as well as state: a second click can land before the re-render.
    if (downloadingRef.current) return;
    downloadingRef.current = true;
    setDownloading(true);
    try {
      await downloadAttachment(attachment);
    } catch (err) {
      console.error("Failed to download feedback attachment", err);
      showToast(DOWNLOAD_FAILED_MESSAGE, "error");
    } finally {
      downloadingRef.current = false;
      setDownloading(false);
    }
  }, [attachment, showToast]);

  const thumbnailBox = "flex h-24 w-24 shrink-0 items-center justify-center overflow-hidden rounded-md border border-[#E2E8F0] bg-white";

  let thumbnail: React.ReactNode;
  if (!attachment.is_image) {
    thumbnail = (
      <div className={`${thumbnailBox} text-[#94A3B8]`}>
        <FileIcon />
      </div>
    );
  } else if (unavailable) {
    thumbnail = (
      <div className={`${thumbnailBox} bg-[#F1F5F9] px-2 text-center`}>
        <span className="text-[10px] font-semibold text-[#94A3B8]">Attachment unavailable</span>
      </div>
    );
  } else if (image.status === "ready") {
    thumbnail = (
      <button
        type="button"
        onClick={() => setPreviewing(true)}
        aria-label={`View attachment ${name}`}
        // The View button beside it is the keyboard route; a second tab stop
        // for the same action would only slow a keyboard user down.
        tabIndex={-1}
        className={`${thumbnailBox} cursor-zoom-in`}
      >
        <img src={image.src} alt={name} className="h-full w-full object-contain" />
      </button>
    );
  } else {
    thumbnail = <div className={`${thumbnailBox} animate-pulse bg-[#E2E8F0]`} aria-busy="true" />;
  }

  return (
    <li className="flex min-w-0 items-center gap-3 rounded-lg border border-[#E2E8F0] bg-[#F8FAFC] p-3">
      {thumbnail}
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13px] font-bold text-[#0F172A]" title={name}>
          {name}
        </div>
        <div className="mt-0.5 text-[11px] font-semibold text-[#64748B]">
          {formatFileSize(attachment.file_size)}
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {attachment.is_image && (
            <button
              type="button"
              onClick={() => setPreviewing(true)}
              disabled={unavailable}
              aria-label={`View attachment ${name}`}
              title={unavailable ? "This attachment could not be loaded." : undefined}
              className={`rounded-lg border border-[#2563EB]/25 bg-[#EFF6FF] px-3 py-1.5 text-[11px] font-bold text-[#2563EB] transition hover:bg-[#DBEAFE] disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              View
            </button>
          )}
          <button
            type="button"
            onClick={() => void download()}
            disabled={downloading}
            aria-label={`Download attachment ${name}`}
            className={`rounded-lg border border-[#E2E8F0] bg-white px-3 py-1.5 text-[11px] font-bold text-[#334155] transition hover:bg-[#F1F5F9] disabled:cursor-not-allowed disabled:opacity-60 ${focusRing}`}
          >
            {downloading ? "Downloading…" : "Download"}
          </button>
        </div>
      </div>
      {previewing && (
        <AttachmentLightbox
          attachment={attachment}
          image={image}
          downloading={downloading}
          onDownload={() => void download()}
          onClose={() => setPreviewing(false)}
        />
      )}
    </li>
  );
};

/** The dialog's "Attachments" block. The caller renders it only when there are any. */
export const AttachmentsSection: React.FC<{ attachments: FeedbackAttachment[] }> = ({ attachments }) => (
  <div className="mt-5">
    <div className="mb-2 text-[10px] font-bold uppercase tracking-wider text-[#94A3B8]">Attachments</div>
    <ul className="space-y-2">
      {attachments.map((attachment) => (
        <AttachmentItem key={attachment.id} attachment={attachment} />
      ))}
    </ul>
  </div>
);
