import type { UserRead } from '../../api/auth';
import type { Feedback, FeedbackStatus } from '../../store/api/feedbackApi';

/**
 * The rules behind the Working / Resolved row controls, as plain functions.
 *
 * They live outside the component on purpose. Everything here — who may press
 * a button, whether a press would do anything, what the row should say
 * afterwards, and what a failure should be worded as — is a decision, and a
 * decision is worth testing directly rather than through a rendered table.
 * `FeedbackTable` keeps the markup and the state; this file keeps the
 * reasoning.
 *
 * None of it is a security boundary. `PATCH /feedback/{id}/status` refuses any
 * caller that is not an administrator regardless of what the browser believes,
 * so a stale copy of the role list here hides a button, never grants one.
 */

/**
 * The roles the backend lets drive the workflow.
 *
 * Mirrors `FEEDBACK_MANAGE_ROLES` in `backend/app/services/feedback.py`, the
 * three administrator spellings and nothing else. HR and Leader read every
 * submission and change none of them — they are deliberately absent.
 */
const FEEDBACK_MANAGE_ROLES = ['administrator', 'org_admin', 'super_admin'];

/** Whether this user may mark feedback Working or Resolved. */
export const canManageFeedback = (user: UserRead | null | undefined) =>
  !!user && FEEDBACK_MANAGE_ROLES.includes((user.role_name || '').trim().toLowerCase());

/** The two states a row control can ask for. Matches the API's `status` field. */
export type FeedbackAction = 'in_progress' | 'resolved';

/** What each button is called, for confirmations, toasts and labels. */
export const ACTION_LABELS: Record<FeedbackAction, string> = {
  in_progress: 'Working',
  resolved: 'Resolved',
};

/**
 * Whether a row is already in (or past) the state a button would ask for.
 *
 * Resolved feedback counts as done for *both* buttons: it has been worked on
 * and finished, so neither control has anything left to ask the server for.
 * This is what greys a button out rather than letting it fire a request the
 * backend would answer with a 409.
 */
export const isActionComplete = (status: FeedbackStatus, action: FeedbackAction) => {
  if (status === 'resolved') return true;
  return status === action;
};

/**
 * Whether pressing this button would change anything.
 *
 * False for an action that is already complete, and false while any action on
 * the same row is in flight. The second half is the duplicate-click guard: a
 * double-click, or Working followed immediately by Resolved, would otherwise
 * put two requests on one row.
 */
export const canPerformAction = (
  status: FeedbackStatus,
  action: FeedbackAction,
  pending: FeedbackAction | null,
) => pending === null && !isActionComplete(status, action);

/**
 * The confirmation shown before a press. Both wordings say plainly that an
 * email goes to the employee, because that is the part of the action the
 * administrator cannot take back.
 */
export const confirmationFor = (action: FeedbackAction, employeeName: string) =>
  action === 'in_progress'
    ? {
        title: 'Send a Working update?',
        message: `${employeeName} will be emailed to say their feedback is being worked on.`,
      }
    : {
        title: 'Mark this feedback as resolved?',
        message: `${employeeName} will be emailed to say their feedback has been resolved.`,
      };

/**
 * What to say after a successful request.
 *
 * `notificationQueued` is the server's account of what actually happened, not
 * the browser's assumption. A repeated request returns false — the row was
 * already in that state and no second email was queued — and the toast says
 * so instead of claiming a message went out.
 */
export const successMessage = (
  action: FeedbackAction,
  employeeName: string,
  notificationQueued: boolean,
) =>
  notificationQueued
    ? `Marked as ${ACTION_LABELS[action]}. ${employeeName} has been notified by email.`
    : `This feedback was already marked as ${ACTION_LABELS[action]}. No new email was sent.`;

/**
 * What to say when the request fails, chosen by status code.
 *
 * Each message describes what the reader can do about it. Nothing here repeats
 * a server error body: it is written for an operator, not for the person
 * holding the mouse.
 */
export const errorMessage = (status?: number) => {
  switch (status) {
    case 401:
      return 'Your session has expired. Please sign in again.';
    case 403:
      return 'Only an administrator can update feedback status.';
    case 404:
      return 'That feedback no longer exists.';
    case 409:
      return 'This feedback has already been resolved and cannot be reopened.';
    default:
      return 'Unable to update this feedback. Please try again.';
  }
};

/** How a row's current state is presented in the Action column. */
export const STATUS_LABELS: Record<FeedbackStatus, string> = {
  new: 'New',
  reviewing: 'Reviewing',
  in_progress: 'Working',
  resolved: 'Resolved',
  closed: 'Closed',
};

/** A row's display status, tolerating a server that sends something unmapped. */
export const statusLabel = (item: Pick<Feedback, 'status'>) =>
  STATUS_LABELS[item.status] ?? item.status;
