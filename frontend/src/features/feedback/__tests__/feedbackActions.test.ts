/**
 * The Working / Resolved row controls, tested as the decisions they are.
 *
 * Four properties matter here, and all four are logic rather than markup:
 *
 * 1. **HR and Leader get no controls.** They read every submission and change
 *    none of them. This is the UI half of the rule; the enforcing half is
 *    `PATCH /feedback/{id}/status`, which refuses them whatever the browser
 *    renders, and is covered by the backend suite.
 * 2. **A button that would do nothing is not pressable.** Already-resolved
 *    feedback cannot be reopened, and pressing Working twice must not become a
 *    second request for the server to refuse.
 * 3. **A press in flight blocks every other press on that row.** This is the
 *    duplicate-click guard, and it is why `canPerformAction` takes the pending
 *    action rather than the component simply disabling on click.
 * 4. **The toast reports what the server said happened.** A repeated request
 *    comes back with `notification_queued: false` and must not be worded as
 *    though an email went out.
 */
import { describe, expect, it } from 'vitest';

import {
  ACTION_LABELS,
  canManageFeedback,
  canPerformAction,
  confirmationFor,
  errorMessage,
  isActionComplete,
  statusLabel,
  statusAfter,
  successMessage,
} from '../feedbackActions';
import type { FeedbackAction } from '../feedbackActions';
import type { UserRead } from '../../../api/auth';
import type { FeedbackStatus } from '../../../store/api/feedbackApi';

const ACTIONS: FeedbackAction[] = ['in_progress', 'resolved'];

const user = (role_name: string) => ({ role_name } as UserRead);

describe('canManageFeedback', () => {
  it('admits every administrator spelling the backend accepts', () => {
    for (const role of ['administrator', 'org_admin', 'super_admin']) {
      expect(canManageFeedback(user(role))).toBe(true);
    }
  });

  it('refuses HR, who may read all feedback and change none of it', () => {
    expect(canManageFeedback(user('hr'))).toBe(false);
  });

  it('refuses leaders, managers and employees', () => {
    for (const role of ['leader', 'project_leader', 'manager', 'employee']) {
      expect(canManageFeedback(user(role))).toBe(false);
    }
  });

  it('refuses a signed-out reader rather than throwing', () => {
    expect(canManageFeedback(null)).toBe(false);
    expect(canManageFeedback(undefined)).toBe(false);
  });

  it('is not confused by casing or stray whitespace on the stored role', () => {
    expect(canManageFeedback(user('  Administrator  '))).toBe(true);
  });
});

describe('isActionComplete', () => {
  it('marks Working complete once the row is in progress', () => {
    expect(isActionComplete('in_progress', 'in_progress')).toBe(true);
  });

  it('marks both controls complete once the row is resolved', () => {
    for (const action of ACTIONS) {
      expect(isActionComplete('resolved', action)).toBe(true);
    }
  });

  it('leaves both open while the row is new', () => {
    for (const action of ACTIONS) {
      expect(isActionComplete('new', action)).toBe(false);
    }
  });

  it('still offers Resolved on a row that is only in progress', () => {
    expect(isActionComplete('in_progress', 'resolved')).toBe(false);
  });
});

describe('canPerformAction', () => {
  it('allows both actions on new feedback when nothing is in flight', () => {
    for (const action of ACTIONS) {
      expect(canPerformAction('new', action, null)).toBe(true);
    }
  });

  it('blocks a second press of a button whose state is already reached', () => {
    expect(canPerformAction('in_progress', 'in_progress', null)).toBe(false);
    expect(canPerformAction('resolved', 'resolved', null)).toBe(false);
  });

  it('blocks reopening resolved feedback', () => {
    expect(canPerformAction('resolved', 'in_progress', null)).toBe(false);
  });

  it('blocks every action on the row while one is in flight', () => {
    // The duplicate-click guard: a double-click on Working, and the
    // Working-then-Resolved case, both stop here.
    expect(canPerformAction('new', 'in_progress', 'in_progress')).toBe(false);
    expect(canPerformAction('new', 'resolved', 'in_progress')).toBe(false);
  });

  it('reopens the controls once the request settles', () => {
    expect(canPerformAction('new', 'resolved', null)).toBe(true);
  });
});

describe('confirmationFor', () => {
  it('says an email is going to the employee, by name, before Working', () => {
    const { title, message } = confirmationFor('in_progress', 'Ada Lovelace');
    expect(title).toBe('Send a Working update?');
    expect(message).toContain('Ada Lovelace');
    expect(message).toContain('emailed');
  });

  it('says the same before Resolved', () => {
    const { title, message } = confirmationFor('resolved', 'Ada Lovelace');
    expect(title).toContain('resolved');
    expect(message).toContain('Ada Lovelace');
    expect(message).toContain('emailed');
  });
});

describe('successMessage', () => {
  it('confirms the notification when the server queued one', () => {
    const message = successMessage('in_progress', 'Ada', true);
    expect(message).toContain('Working');
    expect(message).toContain('notified by email');
  });

  it('does not claim an email when the server queued none', () => {
    // What a repeated request returns. Saying "Ada has been notified" here
    // would be the UI inventing an event the backend explicitly declined.
    const message = successMessage('resolved', 'Ada', false);
    expect(message).toContain('already');
    expect(message).toContain('No new email was sent');
    expect(message).not.toContain('has been notified');
  });
});

describe('errorMessage', () => {
  it('explains a 403 as an administrator-only action', () => {
    expect(errorMessage(403)).toContain('administrator');
  });

  it('explains a 409 as feedback that cannot be reopened', () => {
    expect(errorMessage(409)).toContain('cannot be reopened');
  });

  it('explains a 404 and a 401 in the reader’s own terms', () => {
    expect(errorMessage(404)).toContain('no longer exists');
    expect(errorMessage(401)).toContain('sign in again');
  });

  it('falls back to a retryable message for anything else, including no status', () => {
    for (const status of [undefined, 0, 500, 503]) {
      expect(errorMessage(status)).toContain('Please try again');
    }
  });
});

describe('row state after an update', () => {
  it('maps each action onto the status the server will report', () => {
    expect(statusAfter('in_progress')).toBe('in_progress');
    expect(statusAfter('resolved')).toBe('resolved');
  });

  it('labels the workflow states the way the buttons are labelled', () => {
    expect(statusLabel({ status: 'new' })).toBe('New');
    expect(statusLabel({ status: 'in_progress' })).toBe(ACTION_LABELS.in_progress);
    expect(statusLabel({ status: 'resolved' })).toBe(ACTION_LABELS.resolved);
  });

  it('shows an unmapped status as itself rather than blank', () => {
    expect(statusLabel({ status: 'something_new' as FeedbackStatus })).toBe('something_new');
  });
});
