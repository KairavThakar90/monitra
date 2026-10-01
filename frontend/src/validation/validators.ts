/**
 * The validation functions themselves — one per rule in `./rules`.
 *
 * Mirrors `backend/app/core/validation/validators.py`, with one deliberate
 * difference in shape: **nothing here throws.** Each function returns a
 * discriminated union
 *
 * ```ts
 * { ok: true, value: T } | { ok: false, error: string }
 * ```
 *
 * because in a React form a rejected field is an ordinary, expected outcome to
 * be rendered next to the input — not an exception to unwind the render for.
 *
 * Every `error` string is written for the person who typed the value. None of
 * them quotes a pattern, an internal name, or a stack trace.
 */

import {
  ALLOWED_URL_SCHEMES,
  CONTROL_CHARACTER_PATTERN,
  DATE_PATTERN,
  DESCRIPTION_MAX_LENGTH,
  DOMAIN_MAX_LENGTH,
  DOMAIN_PATTERN,
  EMAIL_MAX_LENGTH,
  EMAIL_PATTERN,
  ENCODED_MARKUP_PATTERN,
  EVENT_HANDLER_PATTERN,
  HTML_TAG_PATTERN,
  IDENTIFIER_MAX,
  IDENTIFIER_MIN,
  NAME_MAX_LENGTH,
  NAME_MIN_LENGTH,
  PASSWORD_MAX_LENGTH,
  PASSWORD_MIN_LENGTH,
  PLAIN_TEXT_MAX_LENGTH,
  SCRIPT_URI_PATTERN,
  SEARCH_MAX_LENGTH,
  TEMPLATE_EXPRESSION_PATTERN,
  TIME_OF_DAY_PATTERN,
  URL_MAX_LENGTH,
  UUID_PATTERN,
  XML_PROLOG_PATTERN,
} from './rules';
import { collapseWhitespace, normalizeEmail, normalizeText } from './sanitizer';

/** A successful validation, carrying the normalised value. */
export type Valid<T> = { ok: true; value: T };
/** A rejected validation, carrying a message safe to show the user. */
export type Invalid = { ok: false; error: string };
/** The result of any validator. Never thrown — always returned. */
export type ValidationResult<T> = Valid<T> | Invalid;

const ok = <T,>(value: T): Valid<T> => ({ ok: true, value });
const fail = (error: string): Invalid => ({ ok: false, error });

// ---------------------------------------------------------------------------
// Structured-content detection (the secondary defence layer)
// ---------------------------------------------------------------------------

/**
 * True when the *entire* value is a JSON object or array.
 *
 * Scoped to the whole field on purpose. A description that happens to mention
 * `{"key": "value"}` mid-sentence is a person explaining a payload, which is
 * exactly what a bug report looks like; a field whose complete contents parse
 * as JSON is a client sending structured data where prose was expected.
 */
export function looksLikeJsonDocument(value: string): boolean {
  const text = value.trim();
  const wrapped =
    (text.startsWith('{') && text.endsWith('}')) ||
    (text.startsWith('[') && text.endsWith(']'));
  if (!wrapped) return false;
  try {
    JSON.parse(text);
  } catch {
    return false;
  }
  return true;
}

/**
 * Return a user-facing reason if `value` is not plain text, else `null`.
 *
 * The order is chosen so the most specific and most alarming explanation wins;
 * a payload usually trips several of these at once.
 */
export function findStructuredContent(value: string): string | null {
  if (HTML_TAG_PATTERN.test(value)) return 'HTML or XML tags are not allowed here.';
  if (XML_PROLOG_PATTERN.test(value)) return 'XML content is not allowed here.';
  if (SCRIPT_URI_PATTERN.test(value)) return 'Script and data links are not allowed here.';
  if (EVENT_HANDLER_PATTERN.test(value)) return 'Event handler attributes are not allowed here.';
  if (TEMPLATE_EXPRESSION_PATTERN.test(value)) return 'Template expressions are not allowed here.';
  if (ENCODED_MARKUP_PATTERN.test(value)) return 'Encoded HTML characters are not allowed here.';
  if (looksLikeJsonDocument(value)) return 'This field expects plain text, not JSON.';
  return null;
}

/**
 * Return an error if `raw` contains a control character other than tab/newline.
 *
 * This runs on the value **as submitted**, before any normalisation. That
 * ordering is deliberate: normalising first would let a NUL be quietly removed
 * and hand back something that looks clean, which is precisely the
 * silent-repair behaviour this framework refuses.
 */
export function rejectControlCharacters(raw: string, fieldLabel: string): string | null {
  if (CONTROL_CHARACTER_PATTERN.test(raw)) {
    return `${fieldLabel} contains unsupported characters.`;
  }
  return null;
}

/**
 * True if `value` holds at least one letter or digit, in any script.
 *
 * The `u` flag plus the Unicode property escapes make this satisfied by
 * Japanese, Cyrillic and Arabic as readily as by ASCII — the question is "is
 * there real content here", not "is this English".
 */
export function containsLetterOrDigit(value: string): boolean {
  return /[\p{L}\p{N}]/u.test(value);
}

/**
 * Return an error unless `value` is plain human-readable text.
 *
 * `requireContent` is false only for search terms: the backend does not
 * require a search box to contain a letter or a digit, and a client must never
 * be stricter than the backend.
 */
export function ensurePlainText(
  value: string,
  fieldLabel: string,
  requireContent = true,
): string | null {
  const control = rejectControlCharacters(value, fieldLabel);
  if (control !== null) return control;
  const reason = findStructuredContent(value);
  if (reason !== null) return `${fieldLabel}: ${reason}`;
  // Length and structure alone let a value through that is nothing but
  // punctuation — "!!!", "...", "@@@" all have a length, contain no markup and
  // are not JSON, so every other check passed them. They are not names or
  // descriptions; they are a way to satisfy a required field without answering
  // it. Punctuation alongside real content is still fine.
  if (requireContent && !containsLetterOrDigit(value)) {
    return `${fieldLabel} must contain at least one letter or number.`;
  }
  return null;
}

const isString = (value: unknown): value is string => typeof value === 'string';

// ---------------------------------------------------------------------------
// Text rules
// ---------------------------------------------------------------------------

export type TextOptions = {
  /** How the field is named in its error messages. */
  fieldLabel?: string;
  maxLength?: number;
  minLength?: number;
  required?: boolean;
};

/**
 * A short human-readable label: a person, project, task or title.
 *
 * Unicode letters, digits, spaces and ordinary punctuation are all accepted —
 * real names contain apostrophes, hyphens, periods and non-Latin scripts, and
 * an allowlist of ASCII letters would lock out a large part of the world. What
 * is rejected is content that is structurally not a label: markup, scripts,
 * whole JSON or XML documents, control characters.
 */
export function validateName(value: unknown, options?: TextOptions): ValidationResult<string> {
  const label = options?.fieldLabel ?? 'Name';
  const maxLength = options?.maxLength ?? NAME_MAX_LENGTH;
  const minLength = options?.minLength ?? NAME_MIN_LENGTH;
  if (!isString(value)) return fail(`${label} must be text.`);
  const control = rejectControlCharacters(value, label);
  if (control !== null) return fail(control);
  const normalized = collapseWhitespace(normalizeText(value)).trim();
  if (normalized.length < minLength) return fail(`${label} is required.`);
  if (normalized.length > maxLength) {
    return fail(`${label} must be at most ${maxLength} characters.`);
  }
  const structural = ensurePlainText(normalized, label);
  if (structural !== null) return fail(structural);
  return ok(normalized);
}

/**
 * Multi-line prose: descriptions, reasons, comments, feedback.
 *
 * Line breaks, punctuation, digits and Unicode are all fine — this is where
 * people write sentences, and over-restricting it makes the product hostile.
 * The limits are length and structure only.
 *
 * An absent or blank optional value resolves to `''`, so a caller can pass the
 * result straight into a request body without a second empty check.
 */
export function validateDescription(
  value: unknown,
  options?: TextOptions,
): ValidationResult<string> {
  const label = options?.fieldLabel ?? 'Description';
  const maxLength = options?.maxLength ?? DESCRIPTION_MAX_LENGTH;
  const required = options?.required === true;
  if (value === null || value === undefined) {
    return required ? fail(`${label} is required.`) : ok('');
  }
  if (!isString(value)) return fail(`${label} must be text.`);
  const control = rejectControlCharacters(value, label);
  if (control !== null) return fail(control);
  const normalized = normalizeText(value, { allowNewlines: true });
  if (normalized.length === 0) {
    return required ? fail(`${label} is required.`) : ok('');
  }
  if (normalized.length > maxLength) {
    return fail(`${label} must be at most ${maxLength} characters.`);
  }
  const structural = ensurePlainText(normalized, label);
  if (structural !== null) return fail(structural);
  return ok(normalized);
}

/** Single-line free text — no newlines survive normalisation. */
export function validatePlainText(
  value: unknown,
  options?: TextOptions,
): ValidationResult<string> {
  const label = options?.fieldLabel ?? 'Value';
  const maxLength = options?.maxLength ?? PLAIN_TEXT_MAX_LENGTH;
  const required = options?.required === true;
  if (value === null || value === undefined) {
    return required ? fail(`${label} is required.`) : ok('');
  }
  if (!isString(value)) return fail(`${label} must be text.`);
  const control = rejectControlCharacters(value, label);
  if (control !== null) return fail(control);
  const normalized = normalizeText(value);
  if (normalized.length === 0) {
    return required ? fail(`${label} is required.`) : ok('');
  }
  if (normalized.length > maxLength) {
    return fail(`${label} must be at most ${maxLength} characters.`);
  }
  const structural = ensurePlainText(normalized, label);
  if (structural !== null) return fail(structural);
  return ok(normalized);
}

/**
 * A search box's contents.
 *
 * Unlike the backend's, this does **not** escape SQL `LIKE` wildcards: the
 * browser is sending a query parameter, not building a pattern, and escaping
 * here would send the user a literal backslash they never typed. The backend's
 * `validate_search_term` does the escaping at the query site, where it belongs.
 *
 * A blank search box is a valid state and resolves to `''`.
 */
export function validateSearchTerm(
  value: unknown,
  options?: TextOptions,
): ValidationResult<string> {
  const label = options?.fieldLabel ?? 'Search';
  const maxLength = options?.maxLength ?? SEARCH_MAX_LENGTH;
  if (value === null || value === undefined) return ok('');
  if (!isString(value)) return fail(`${label} must be text.`);
  const control = rejectControlCharacters(value, label);
  if (control !== null) return fail(control);
  const normalized = normalizeText(value);
  if (normalized.length === 0) return ok('');
  if (normalized.length > maxLength) {
    return fail(`${label} must be at most ${maxLength} characters.`);
  }
  const structural = ensurePlainText(normalized, label, false);
  if (structural !== null) return fail(structural);
  return ok(normalized);
}

// ---------------------------------------------------------------------------
// Email and password
// ---------------------------------------------------------------------------

/** An email address, trimmed with its domain lower-cased. */
export function validateEmail(
  value: unknown,
  options?: { fieldLabel?: string },
): ValidationResult<string> {
  const label = options?.fieldLabel ?? 'Email';
  if (!isString(value)) return fail(`${label} must be text.`);
  const control = rejectControlCharacters(value, label);
  if (control !== null) return fail(control);
  const normalized = normalizeEmail(value);
  if (normalized.length === 0) return fail(`${label} is required.`);
  if (normalized.length > EMAIL_MAX_LENGTH) {
    return fail(`${label} must be at most ${EMAIL_MAX_LENGTH} characters.`);
  }
  if (!EMAIL_PATTERN.test(normalized)) return fail('Please enter a valid email address.');
  return ok(normalized);
}

/**
 * A password being *chosen*, returned character-for-character as it was typed.
 *
 * Passwords are the one field this framework never touches. It does not trim
 * them, does not normalise Unicode, does not collapse whitespace, does not
 * apply the plain-text checks, and does not run structured-content detection.
 * Every one of those would silently change a secret and lock someone out — a
 * leading space or a combining accent is a legitimate part of a password, and
 * `<`, `{`, `}`, `[`, `]`, `!@#$%^&*` are all perfectly good characters.
 *
 * Only the length policy applies, because length is the one property that can
 * be checked without altering or inspecting the value.
 */
export function validatePassword(
  value: unknown,
  options?: { fieldLabel?: string },
): ValidationResult<string> {
  const label = options?.fieldLabel ?? 'Password';
  if (!isString(value)) return fail(`${label} must be text.`);
  if (value.length < PASSWORD_MIN_LENGTH) {
    return fail(`${label} must be at least ${PASSWORD_MIN_LENGTH} characters.`);
  }
  if (value.length > PASSWORD_MAX_LENGTH) {
    return fail(`${label} must be at most ${PASSWORD_MAX_LENGTH} characters.`);
  }
  return ok(value);
}

/**
 * A password being presented for *login*, not being chosen.
 *
 * Deliberately weaker than {@link validatePassword}: an account created before
 * today's policy may hold a shorter secret, and refusing to send it would lock
 * that person out while telling an attacker that the length policy can be
 * probed at the login form. Only non-empty and the upper bound are enforced.
 * The value is never modified.
 */
export function validateCredential(
  value: unknown,
  options?: { fieldLabel?: string },
): ValidationResult<string> {
  const label = options?.fieldLabel ?? 'Password';
  if (!isString(value)) return fail(`${label} must be text.`);
  if (value.length === 0) return fail(`${label} is required.`);
  if (value.length > PASSWORD_MAX_LENGTH) {
    return fail(`${label} must be at most ${PASSWORD_MAX_LENGTH} characters.`);
  }
  return ok(value);
}

// ---------------------------------------------------------------------------
// Numbers and identifiers
// ---------------------------------------------------------------------------

export type NumberOptions = {
  fieldLabel?: string;
  minimum?: number;
  maximum?: number;
};

/**
 * A whole number within an optional range.
 *
 * Booleans are rejected explicitly, and so is `''` — `Number('')` is `0`, which
 * is how an empty numeric box silently becomes a real value.
 */
export function validateInteger(
  value: unknown,
  options?: NumberOptions,
): ValidationResult<number> {
  const label = options?.fieldLabel ?? 'Value';
  let candidate: number;
  if (typeof value === 'boolean') return fail(`${label} must be a number.`);
  if (typeof value === 'number') {
    candidate = value;
  } else if (isString(value)) {
    const text = value.trim();
    if (text.length === 0 || !/^[+-]?\d+$/.test(text)) {
      return fail(`${label} must be a whole number.`);
    }
    candidate = Number(text);
  } else {
    return fail(`${label} must be a whole number.`);
  }
  if (!Number.isFinite(candidate) || !Number.isInteger(candidate)) {
    return fail(`${label} must be a whole number.`);
  }
  const minimum = options?.minimum;
  const maximum = options?.maximum;
  if (minimum !== undefined && candidate < minimum) {
    return fail(`${label} must be at least ${minimum}.`);
  }
  if (maximum !== undefined && candidate > maximum) {
    return fail(`${label} must be at most ${maximum}.`);
  }
  return ok(candidate);
}

/** A database key: a positive whole number inside the 64-bit range. */
export function validateIdentifier(
  value: unknown,
  options?: { fieldLabel?: string },
): ValidationResult<number> {
  const label = options?.fieldLabel ?? 'Identifier';
  const result = validateInteger(value, {
    fieldLabel: label,
    minimum: IDENTIFIER_MIN,
    maximum: IDENTIFIER_MAX,
  });
  if (!result.ok) return fail(`${label} is not a valid selection.`);
  return result;
}

export type DecimalOptions = NumberOptions & {
  /** Reject negatives outright, with a message that says so. */
  allowNegative?: boolean;
};

/**
 * A fractional number.
 *
 * NaN and infinity are refused: both parse happily and then poison every
 * comparison and aggregate downstream. `''`, `'  '`, `'abc'` and `'1e'` are all
 * rejected rather than becoming `0` or `NaN`, which is the failure mode of a
 * bare `Number()` on a form field.
 */
export function validateDecimal(
  value: unknown,
  options?: DecimalOptions,
): ValidationResult<number> {
  const label = options?.fieldLabel ?? 'Value';
  let candidate: number;
  if (typeof value === 'boolean') return fail(`${label} must be a number.`);
  if (typeof value === 'number') {
    candidate = value;
  } else if (isString(value)) {
    const text = value.trim();
    if (text.length === 0 || !/^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/.test(text)) {
      return fail(`${label} must be a number.`);
    }
    candidate = Number(text);
  } else {
    return fail(`${label} must be a number.`);
  }
  if (!Number.isFinite(candidate)) return fail(`${label} must be a number.`);
  if (options?.allowNegative === false && candidate < 0) {
    return fail(`${label} cannot be negative.`);
  }
  const minimum = options?.minimum;
  const maximum = options?.maximum;
  if (minimum !== undefined && candidate < minimum) {
    return fail(`${label} must be at least ${minimum}.`);
  }
  if (maximum !== undefined && candidate > maximum) {
    return fail(`${label} must be at most ${maximum}.`);
  }
  return ok(candidate);
}

// ---------------------------------------------------------------------------
// Strict formats
// ---------------------------------------------------------------------------

/** A canonical hyphenated UUID, returned lower-cased. */
export function validateUuid(
  value: unknown,
  options?: { fieldLabel?: string },
): ValidationResult<string> {
  const label = options?.fieldLabel ?? 'Identifier';
  if (!isString(value)) return fail(`${label} must be text.`);
  const candidate = value.trim();
  if (!UUID_PATTERN.test(candidate)) return fail(`${label} is not a valid identifier.`);
  return ok(candidate.toLowerCase());
}

/**
 * A calendar date, from a `YYYY-MM-DD` string or a `Date`.
 *
 * Returns the canonical `YYYY-MM-DD` string, because that is what every date
 * input in this app holds and what every request body carries — handing back a
 * `Date` would only invite a timezone bug on the way to the API.
 *
 * The round-trip through `Date.UTC` is what catches `2025-02-30`, which matches
 * the pattern but is not a real day.
 */
export function validateDate(
  value: unknown,
  options?: { fieldLabel?: string; required?: boolean },
): ValidationResult<string> {
  const label = options?.fieldLabel ?? 'Date';
  const required = options?.required === true;
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) return fail(`${label} is not a real date.`);
    return ok(value.toISOString().slice(0, 10));
  }
  if (value === null || value === undefined) {
    return required ? fail(`${label} is required.`) : ok('');
  }
  if (!isString(value)) return fail(`${label} must be a date.`);
  const candidate = value.trim();
  if (candidate.length === 0) {
    return required ? fail(`${label} is required.`) : ok('');
  }
  if (!DATE_PATTERN.test(candidate)) return fail(`${label} must be in YYYY-MM-DD format.`);
  const year = Number(candidate.slice(0, 4));
  const month = Number(candidate.slice(5, 7));
  const day = Number(candidate.slice(8, 10));
  const stamp = Date.UTC(year, month - 1, day);
  const roundTrip = new Date(stamp);
  if (
    Number.isNaN(stamp) ||
    roundTrip.getUTCFullYear() !== year ||
    roundTrip.getUTCMonth() !== month - 1 ||
    roundTrip.getUTCDate() !== day
  ) {
    return fail(`${label} is not a real date.`);
  }
  return ok(candidate);
}

/**
 * A value drawn from a fixed set.
 *
 * The permitted values are named in the error, because unlike a rejected
 * password they are not a secret — they are part of the contract, and a user
 * cannot fix the input without them.
 */
export function validateEnum<T>(
  value: unknown,
  allowed: readonly T[],
  options?: { fieldLabel?: string },
): ValidationResult<T> {
  const label = options?.fieldLabel ?? 'Value';
  for (const candidate of allowed) {
    if (candidate === value) return ok(candidate);
  }
  const readable = allowed.map((item) => String(item)).join(', ');
  return fail(`${label} must be one of: ${readable}.`);
}

/**
 * A wall-clock time, `HH:MM` on a 24-hour clock. Mirrors the backend rule:
 * rejected rather than repaired, so a schedule has one spelling per time.
 */
export function validateTimeOfDay(
  value: unknown,
  options?: { fieldLabel?: string },
): ValidationResult<string> {
  const label = options?.fieldLabel ?? 'Time';
  if (!isString(value)) return fail(`${label} must be text.`);
  const candidate = value.trim();
  if (!candidate) return fail(`${label} is required.`);
  if (!TIME_OF_DAY_PATTERN.test(candidate)) {
    return fail(`${label} must be a time like 09:30 (24-hour, HH:MM).`);
  }
  return ok(candidate);
}

/** A hostname, lower-cased. */
export function validateDomain(
  value: unknown,
  options?: { fieldLabel?: string },
): ValidationResult<string> {
  const label = options?.fieldLabel ?? 'Domain';
  if (!isString(value)) return fail(`${label} must be text.`);
  const control = rejectControlCharacters(value, label);
  if (control !== null) return fail(control);
  const candidate = normalizeText(value).toLowerCase();
  if (candidate.length === 0) return fail(`${label} is required.`);
  if (candidate.length > DOMAIN_MAX_LENGTH) {
    return fail(`${label} must be at most ${DOMAIN_MAX_LENGTH} characters.`);
  }
  if (!DOMAIN_PATTERN.test(candidate)) return fail(`${label} is not a valid domain.`);
  return ok(candidate);
}

/**
 * An absolute `http`/`https` URL.
 *
 * URL fields get URL rules, never the plain-text ones: a legitimate URL is full
 * of characters — `?`, `&`, `=`, `%`, `#` — that the prose checks would reject.
 * The scheme allowlist is the security control here, because it is what stops a
 * stored `javascript:` link becoming script execution in whatever renders it.
 */
export function validateUrl(
  value: unknown,
  options?: { fieldLabel?: string; required?: boolean },
): ValidationResult<string> {
  const label = options?.fieldLabel ?? 'URL';
  const required = options?.required === true;
  if (value === null || value === undefined || (isString(value) && value.trim().length === 0)) {
    return required ? fail(`${label} is required.`) : ok('');
  }
  if (!isString(value)) return fail(`${label} must be text.`);
  const control = rejectControlCharacters(value, label);
  if (control !== null) return fail(control);
  const candidate = normalizeText(value);
  if (candidate.length > URL_MAX_LENGTH) {
    return fail(`${label} must be at most ${URL_MAX_LENGTH} characters.`);
  }
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    return fail(`Please enter a valid ${label}.`);
  }
  const scheme = parsed.protocol.replace(/:$/, '').toLowerCase();
  if (!ALLOWED_URL_SCHEMES.includes(scheme)) {
    return fail(`${label} must start with http:// or https://.`);
  }
  if (parsed.host.length === 0) return fail(`Please enter a valid ${label}.`);
  return ok(candidate);
}
