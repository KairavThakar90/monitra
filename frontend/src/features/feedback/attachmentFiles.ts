import { ENDPOINTS } from "../../api/endpoints";
import type { Feedback, FeedbackAttachment } from "../../store/api/feedbackApi";

/**
 * What the feedback screens need to know about a file attached to a feedback,
 * kept apart from the markup so it can be tested as logic.
 *
 * The list carries metadata only. The bytes sit behind a route that wants the
 * bearer token, so they are fetched here and handed to the page as object URLs
 * -- a token or a storage URL never goes into a link.
 */

/** Attachments of a row. Both fields are absent on older payloads and caches. */
export const attachmentsOf = (item: Pick<Feedback, "attachments">): FeedbackAttachment[] =>
  item.attachments ?? [];

export const attachmentCountOf = (item: Pick<Feedback, "attachment_count" | "attachments">): number =>
  item.attachment_count ?? item.attachments?.length ?? 0;

export const attachmentCountLabel = (count: number): string =>
  `${count} ${count === 1 ? "attachment" : "attachments"}`;

/** "512 B", "48 KB", "1.2 MB". Binary units, one decimal under ten. */
export const formatFileSize = (bytes: number): string => {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const text = value < 10 ? value.toFixed(1).replace(/\.0$/, "") : String(Math.round(value));
  return `${text} ${units[unit]}`;
};

/** The attachment's bytes. Rejects on any non-2xx so callers show one failure state. */
export const fetchAttachmentBlob = async (
  id: number,
  options: { download?: boolean; signal?: AbortSignal } = {},
): Promise<Blob> => {
  const url = ENDPOINTS.FEEDBACK.ATTACHMENT(id) + (options.download ? "?download=true" : "");
  const token = localStorage.getItem("accessToken");
  const response = await fetch(url, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    signal: options.signal,
  });
  if (!response.ok) throw new Error(`Attachment request failed (${response.status})`);
  return response.blob();
};

/**
 * Save an attachment through the browser's own download, without navigating.
 * The object URL exists only for the click and is revoked straight after.
 */
export const downloadAttachment = async (attachment: FeedbackAttachment): Promise<void> => {
  const blob = await fetchAttachmentBlob(attachment.id, { download: true });
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = attachment.original_filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  } finally {
    URL.revokeObjectURL(url);
  }
};

export const DOWNLOAD_FAILED_MESSAGE = "Couldn't download the attachment. Please try again.";
