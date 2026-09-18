/**
 * `formatApiError` — turning a FastAPI error body into words a person reads.
 *
 * `AdminProjectManagement.tsx` used to show one fixed string
 * ("Unable to save project. Please try again.") for every failure, whatever
 * the backend actually said — including a validation error a person could
 * have acted on immediately (e.g. "deadline: Input should be a valid
 * date."). That is the "does not show the proper alert message" report: the
 * real reason a save failed was thrown away before it reached the screen.
 * These tests pin the three shapes FastAPI actually sends, plus the two
 * cases with no usable detail, where the caller's own fallback message must
 * survive untouched.
 */
import { describe, expect, it } from 'vitest';

import { formatApiError } from '../utils';

describe('formatApiError', () => {
  it('a plain string detail (a raised HTTPException) is shown as-is', () => {
    expect(formatApiError({ detail: 'leader_id is required.' }, 'fallback')).toBe(
      'leader_id is required.',
    );
  });

  it('a Pydantic validation array is joined into one readable line', () => {
    const body = {
      detail: [
        { loc: ['body', 'deadline'], msg: 'Input should be a valid date' },
        { loc: ['body', 'fixed_hours'], msg: 'Input should be greater than 0' },
      ],
    };
    expect(formatApiError(body, 'fallback')).toBe(
      'deadline: Input should be a valid date; fixed_hours: Input should be greater than 0',
    );
  });

  it('an object detail with its own message field is unwrapped', () => {
    expect(formatApiError({ detail: { message: 'Name already taken.' } }, 'fallback')).toBe(
      'Name already taken.',
    );
  });

  it('no error body at all (a network failure) falls back to the callers own message', () => {
    expect(formatApiError(undefined, 'Unable to save project. Please try again.')).toBe(
      'Unable to save project. Please try again.',
    );
    expect(formatApiError(null, 'Unable to save project. Please try again.')).toBe(
      'Unable to save project. Please try again.',
    );
  });

  it('a body with no detail field falls back to the callers own message', () => {
    expect(formatApiError({}, 'Unable to save project. Please try again.')).toBe(
      'Unable to save project. Please try again.',
    );
  });
});
