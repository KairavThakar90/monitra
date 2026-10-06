import type { CaptureState, ScreenshotTimelineWindow } from '../../store/api/screenshotsApi';

/**
 * What to say in a capture window that holds no image.
 *
 * "No capture" used to be the only thing the grid could say, and it read the
 * same whether the screen was never read, the picture was minutes from landing,
 * or Drive had refused it all afternoon. The desktop now reports why, and the
 * backend derives a state per window. Only `none` — nothing reported at all,
 * which is also what an older desktop or a machine that was switched off looks
 * like — is still plain "No capture"; every other state names what is known.
 *
 * Pure, so the wording can be tested without a DOM.
 */

export type CaptureTone = 'neutral' | 'pending' | 'problem';

export interface CaptureStateView {
  /** The headline on the tile. `No capture` is kept verbatim for `none`. */
  label: string;
  /** One line saying why, when the desktop gave a reason. */
  detail: string | null;
  tone: CaptureTone;
}

/** Reason codes the desktop reports, in words. */
const REASONS: Record<string, string> = {
  screen_unreadable: 'the screen could not be read',
  encode_failed: 'the picture could not be processed',
  store_failed: 'the picture could not be saved on the computer',
  queue_failed: 'the picture could not be queued on the computer',
  capture_exception: 'the capture failed unexpectedly',
  capture_stuck: 'the capture did not finish',
  capture_unavailable: 'screen capture is not available on that computer',
  screen_recording_blocked: 'screen recording permission has not been granted',
  privacy_rule: 'a privacy rule excluded the application on screen',
  privacy_config_unavailable: 'the privacy settings could not be loaded',
  network: 'the computer could not reach the server',
  unexpected: 'the upload failed unexpectedly',
  unconfirmed: 'the server did not confirm the upload',
};

/** `http_502` → "the server answered 502"; anything unknown is shown, not hidden. */
export const describeCaptureReason = (reason: string | null | undefined): string | null => {
  if (!reason) return null;
  const known = REASONS[reason];
  if (known) return known;
  const http = /^http_(\d{3})$/.exec(reason);
  if (http) return `the server answered ${http[1]}`;
  return reason.replace(/_/g, ' ');
};

const attemptsNote = (attempts: number | undefined): string =>
  attempts && attempts > 1 ? ` (${attempts} attempts)` : '';

export const describeCaptureState = (
  window: Pick<ScreenshotTimelineWindow, 'capture_state' | 'capture_reason' | 'capture_attempts'>,
): CaptureStateView => {
  const state: CaptureState = window.capture_state ?? 'none';
  const reason = describeCaptureReason(window.capture_reason);
  const attempts = attemptsNote(window.capture_attempts);

  switch (state) {
    case 'pending':
      return {
        label: 'Upload pending',
        detail: `Captured on the computer; ${reason ?? 'still uploading'}${attempts}`,
        tone: 'pending',
      };
    case 'failed':
      return {
        label: 'Capture failed',
        detail: reason ? `${capitalise(reason)}${attempts}` : null,
        tone: 'problem',
      };
    case 'blocked':
      return {
        label: 'Capture blocked',
        detail: reason ? capitalise(reason) : null,
        tone: 'problem',
      };
    case 'excluded':
      return {
        label: 'Held back by privacy rule',
        detail: reason ? capitalise(reason) : null,
        tone: 'neutral',
      };
    case 'unavailable':
      return {
        label: 'Capture unavailable',
        detail: reason ? capitalise(reason) : null,
        tone: 'problem',
      };
    case 'captured':
    case 'none':
    default:
      // `captured` never reaches the empty tile (it has an image); if it ever
      // did, plain "No capture" is the honest reading of an empty window.
      return { label: 'No capture', detail: null, tone: 'neutral' };
  }
};

const capitalise = (text: string): string => text.charAt(0).toUpperCase() + text.slice(1);
