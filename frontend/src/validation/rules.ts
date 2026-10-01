/**
 * The single catalogue of input rules for the web client.
 *
 * This file is a deliberate mirror of `backend/app/core/validation/rules.py`.
 * Every constant and every pattern below is the same value the backend
 * enforces, so that the browser can give immediate, accurate feedback and the
 * user is never told a value is fine only for the API to reject it (or the
 * reverse). The backend remains the final authority — this is early feedback,
 * not a replacement for it.
 *
 * **If you change a limit here, change it there too.** The three catalogues
 * (`backend/app/core/validation`, `desktop/core/validation`,
 * `frontend/src/validation`) are kept in step on purpose.
 */

/**
 * What kind of thing a field holds. The string values match the backend
 * `Rule` enum exactly, so a rule name can be read straight off either side.
 */
export type Rule =
  /** A short human-readable label: a person, a project, a task, a title. */
  | 'name'
  /** Multi-line prose written by a user: descriptions, reasons, comments. */
  | 'description'
  /** Single-line free text with no line breaks (a window title, a label). */
  | 'plain_text'
  /** An email address. */
  | 'email'
  /** A secret. Never trimmed, never normalised, never pattern-checked. */
  | 'password'
  /** A whole number. */
  | 'integer'
  /** A fractional number. */
  | 'decimal'
  /** A database identifier: a positive whole number. */
  | 'identifier'
  /** A UUID in canonical hyphenated form. */
  | 'uuid'
  /** A calendar date. */
  | 'date'
  /** A date and time. */
  | 'datetime'
  /** A value drawn from a fixed set. */
  | 'enum'
  /** An absolute http(s) URL. */
  | 'url'
  /** A hostname such as `docs.example.com`. */
  | 'domain'
  /** A user's search term. */
  | 'search'
  /** An opaque client-generated idempotency key. */
  | 'idempotency_key'
  /** A semantic version, `major.minor.patch`. */
  | 'version'
  /** A SHA-256 digest in lower-case hexadecimal. */
  | 'sha256'
  /** A wall-clock time of day, `HH:MM` on a 24-hour clock. */
  | 'time_of_day';

// ---------------------------------------------------------------------------
// Length limits — identical to the backend's
// ---------------------------------------------------------------------------

/** Longest name we accept. Matches the 150-char name columns. */
export const NAME_MAX_LENGTH = 150;
/** Names must contain at least one non-whitespace character. */
export const NAME_MIN_LENGTH = 1;

/** Long enough for a detailed hand-off note, short enough to bound a row. */
export const DESCRIPTION_MAX_LENGTH = 5000;

/** One line of free text. */
export const PLAIN_TEXT_MAX_LENGTH = 255;

/** The maximum length of an email address, per RFC 5321. */
export const EMAIL_MAX_LENGTH = 254;

/**
 * Password policy. The minimum is a floor on guessing cost; the maximum only
 * stops a megabyte of text reaching the hasher. Neither bound alters the secret.
 */
export const PASSWORD_MIN_LENGTH = 8;
export const PASSWORD_MAX_LENGTH = 128;

/** Search boxes. Short, because a search term is a filter and not a document. */
export const SEARCH_MAX_LENGTH = 100;

/** Absolute URLs, and the hostnames inside them. */
export const URL_MAX_LENGTH = 2048;
export const DOMAIN_MAX_LENGTH = 255;

/** Client-generated idempotency keys. */
export const IDEMPOTENCY_KEY_MAX_LENGTH = 255;

/**
 * A `major.minor.patch` version string. Generous next to the pattern, which is
 * the real constraint: the limit only stops a huge string reaching the regex.
 */
export const VERSION_MAX_LENGTH = 32;

/** A SHA-256 digest is exactly 64 hexadecimal characters. Fixed, not a maximum. */
export const SHA256_LENGTH = 64;

/**
 * Identifiers are positive. Zero and negatives are always a bug or an attack,
 * never a real primary key.
 */
export const IDENTIFIER_MIN = 1;

/**
 * The ceiling, which is the one number here that deliberately does *not* match
 * the backend's.
 *
 * The backend's `IDENTIFIER_MAX` is the signed 64-bit maximum,
 * 9_223_372_036_854_775_807. A JavaScript number cannot hold that value: it
 * rounds to 9223372036854775808, so writing it here would be a literal whose
 * runtime value is not the number written. More to the point, any id above
 * `Number.MAX_SAFE_INTEGER` has already lost precision by the time it reaches
 * this check, so accepting it would be accepting a value we can no longer
 * compare correctly.
 *
 * This bound is therefore *narrower* than the backend's, which is the safe
 * direction: everything the browser accepts, the API also accepts. Real keys
 * in this system are nowhere near either number.
 */
export const IDENTIFIER_MAX = Number.MAX_SAFE_INTEGER;

/** The largest number of items a repeatable query parameter may carry. */
export const MAX_LIST_PARAM_ITEMS = 200;

// ---------------------------------------------------------------------------
// Patterns
// ---------------------------------------------------------------------------

/**
 * Email. The same shape the backend uses. Full RFC 5322 is not worth
 * implementing: it accepts addresses no provider issues, and the real proof an
 * address exists is a delivered message.
 */
export const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Canonical hyphenated UUID, any version. */
export const UUID_PATTERN =
  /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/** A hostname: dot-separated labels, no scheme, no path, no userinfo. */
export const DOMAIN_PATTERN =
  /^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$/;

/** `YYYY-MM-DD`. Range correctness is checked separately. */
export const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

/** Idempotency keys are opaque, so only the alphabet is constrained. */
export const IDEMPOTENCY_KEY_PATTERN = /^[A-Za-z0-9._:-]{1,255}$/;

/**
 * `major.minor.patch`, and nothing else. No `v` prefix, no pre-release suffix.
 * Strict on purpose: this pattern is what makes version *ordering* meaningful,
 * and leading zeros are refused so `1.01.0` and `1.1.0` cannot name the same
 * release in two ways.
 */
export const VERSION_PATTERN = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

/** Lower-case hex, exactly 64 characters. Case is normalised before matching. */
export const SHA256_PATTERN = /^[0-9a-f]{64}$/;

/**
 * `HH:MM` on a 24-hour clock, and nothing else -- one spelling per time of day.
 * The timezone is not part of the value; it belongs to the feature that stores
 * it (the desktop notification schedule is in IST).
 */
export const TIME_OF_DAY_PATTERN = /^([01]\d|2[0-3]):[0-5]\d$/;

/**
 * The schemes a stored URL may use. `javascript:` and `data:` are the two that
 * turn a stored link into script execution in whatever renders it.
 */
export const ALLOWED_URL_SCHEMES: readonly string[] = ['http', 'https'];

// ---------------------------------------------------------------------------
// Structured-content detection
// ---------------------------------------------------------------------------
// These are the *secondary* defence. The primary defence is that every field
// has an expected type, a length and a format. What follows only rejects
// content that is structurally wrong for a plain-text field — markup, script,
// a whole JSON document, a whole XML document — and it is written to be
// specific so that ordinary prose survives.
//
// Deliberate trade-off, so a future reader does not think it accidental: a
// sentence like "wrap it in a <div>" is rejected from a description, because
// the pattern cannot tell that apart from injected markup. Comparisons such as
// "5 < 10" and arrows such as "a -> b" are unaffected, because a tag requires
// "<" to be followed immediately by a letter or a slash.

/** An HTML/XML tag: `<p>`, `</div>`, `<img src=x>`, `<br/>`. */
export const HTML_TAG_PATTERN = /<\/?[A-Za-z][A-Za-z0-9-]*(\s[^<>]*)?\/?>/;

/** An HTML entity that could reconstitute a tag after decoding. */
export const ENCODED_MARKUP_PATTERN = /&(?:lt|gt|#0*(?:60|62)|#[xX]0*3[cCeE]);/;

/** A `javascript:`/`vbscript:`/`data:` URL, wherever it appears. */
export const SCRIPT_URI_PATTERN = /\b(?:javascript|vbscript|data)\s*:/i;

/** An inline event handler such as `onerror=` or `onclick =`. */
export const EVENT_HANDLER_PATTERN = /\bon[a-z]{3,20}\s*=/i;

/** A template-injection wrapper: `${...}`, `{{...}}`, `<%...%>`. */
export const TEMPLATE_EXPRESSION_PATTERN = /\$\{[^}]*\}|\{\{[^}]*\}\}|<%[\s\S]*?%>/;

/** An XML prolog or a CDATA section. */
export const XML_PROLOG_PATTERN = /<\?xml\b|<!\[CDATA\[|<!DOCTYPE\b/i;

/**
 * A NUL or other C0 control character. Tab, newline and carriage return are
 * excluded here and handled by the per-rule newline policy instead.
 */
// oxlint-disable-next-line no-control-regex
export const CONTROL_CHARACTER_PATTERN = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/;

/**
 * The maximum length each rule enforces, for driving `maxLength` on an input.
 * Rules with no natural character cap are absent.
 */
export const RULE_MAX_LENGTH: Partial<Record<Rule, number>> = {
  name: NAME_MAX_LENGTH,
  description: DESCRIPTION_MAX_LENGTH,
  plain_text: PLAIN_TEXT_MAX_LENGTH,
  email: EMAIL_MAX_LENGTH,
  password: PASSWORD_MAX_LENGTH,
  search: SEARCH_MAX_LENGTH,
  url: URL_MAX_LENGTH,
  domain: DOMAIN_MAX_LENGTH,
  idempotency_key: IDEMPOTENCY_KEY_MAX_LENGTH,
  version: VERSION_MAX_LENGTH,
  sha256: SHA256_LENGTH,
};
