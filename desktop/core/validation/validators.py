"""Field validators for the desktop client.

These mirror ``backend/app/core/validation/validators.py`` rule for rule, with
one deliberate difference in shape: **they return a result instead of raising.**

The dialogs in ``ui/`` are all written the same way — a sequence of guard
clauses that show the first problem and return::

    result = validate_name(self.name_input.text(), field_label="Task name")
    if not result.ok:
        self._show_error(result.error)
        return

An exception-based API would have every one of those become a ``try``/``except``
around a single call, which reads worse and, more importantly, invites someone
to wrap a whole submit handler in one ``except`` — swallowing real errors along
with validation ones. ``DO_NOT_DO.md`` is explicit that exceptions must not be
swallowed; returning a value keeps validation failures and genuine faults on
visibly different paths.

Every ``error`` string is written to be shown to a person as-is. None of them
names a pattern, a field limit's origin, or anything about the implementation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from .rules import (
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
    SHA256_LENGTH,
    SHA256_PATTERN,
    TIME_OF_DAY_PATTERN,
    TEMPLATE_EXPRESSION_PATTERN,
    URL_MAX_LENGTH,
    UUID_PATTERN,
    VERSION_MAX_LENGTH,
    VERSION_PATTERN,
    XML_PROLOG_PATTERN,
)
from .sanitizer import collapse_whitespace, normalize_email, normalize_text


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of validating one field.

    ``ok`` is the only thing a caller should branch on. When it is true,
    ``value`` holds the normalised value to submit — use it rather than
    re-reading the widget, or the normalisation is thrown away and the server
    receives the raw text after all. When false, ``error`` is a finished
    sentence to show the user.
    """

    ok: bool
    value: Any = None
    error: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.ok


def _ok(value: Any) -> ValidationResult:
    return ValidationResult(ok=True, value=value)


def _fail(message: str) -> ValidationResult:
    return ValidationResult(ok=False, error=message)


# --- Structured-content detection ------------------------------------------


def looks_like_json_document(value: str) -> bool:
    """True when the *entire* value is a JSON object or array."""
    text = value.strip()
    if not (text.startswith("{") and text.endswith("}")) and not (
        text.startswith("[") and text.endswith("]")
    ):
        return False
    try:
        json.loads(text)
    except (ValueError, RecursionError):
        return False
    return True


def find_structured_content(value: str) -> Optional[str]:
    """Return a user-facing reason if *value* is not plain text, else ``None``."""
    if HTML_TAG_PATTERN.search(value):
        return "HTML or XML tags are not allowed here."
    if XML_PROLOG_PATTERN.search(value):
        return "XML content is not allowed here."
    if SCRIPT_URI_PATTERN.search(value):
        return "Script and data links are not allowed here."
    if EVENT_HANDLER_PATTERN.search(value):
        return "Event handler attributes are not allowed here."
    if TEMPLATE_EXPRESSION_PATTERN.search(value):
        return "Template expressions are not allowed here."
    if ENCODED_MARKUP_PATTERN.search(value):
        return "Encoded HTML characters are not allowed here."
    if looks_like_json_document(value):
        return "This field expects plain text, not JSON."
    return None


def contains_letter_or_digit(value: str) -> bool:
    """True if *value* holds at least one letter or digit, in any script.

    ``str.isalnum`` is Unicode-aware, so Japanese, Cyrillic and Arabic satisfy
    this as readily as ASCII — the question is "is there real content here",
    not "is this English".
    """
    return any(character.isalnum() for character in value)


def _plain_text_problem(
    raw: str,
    normalized: str,
    field_label: str,
    *,
    require_content: bool = True,
) -> Optional[str]:
    """The shared content check. Control characters are tested on the raw text.

    Testing the raw value matters: ``normalize_text`` strips control characters,
    so checking afterwards would report success on a value that had a NUL
    removed behind the user's back.

    *require_content* is false only for search terms. The backend does not
    require a search box to contain a letter or a digit, and a client must never
    be stricter than the backend -- refusing to search for "???" here would make
    the box reject a query the server would have answered.
    """
    if CONTROL_CHARACTER_PATTERN.search(raw):
        return f"{field_label} contains unsupported characters."
    reason = find_structured_content(normalized)
    if reason is not None:
        return f"{field_label}: {reason}"
    # Length and structure alone let a value through that is nothing but
    # punctuation -- "!!!", "...", "@@@" all have a length, contain no markup
    # and are not JSON, so every other check passed them. They are not names or
    # descriptions; they are a way to satisfy a required field without
    # answering it. Punctuation alongside real content is still fine: this asks
    # only that something in the value is a letter or a number.
    if require_content and not contains_letter_or_digit(normalized):
        return f"{field_label} must contain at least one letter or number."
    return None


# --- Text rules ------------------------------------------------------------


def validate_name(
    value: Any,
    *,
    field_label: str = "Name",
    max_length: int = NAME_MAX_LENGTH,
    min_length: int = NAME_MIN_LENGTH,
    required: bool = True,
) -> ValidationResult:
    """A short human-readable label: a person, project, task or title."""
    if value is None:
        return _ok(None) if not required else _fail(f"{field_label} is required.")
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    normalized = collapse_whitespace(normalize_text(value)).strip()
    if not normalized:
        return _fail(f"{field_label} is required.") if required else _ok(None)
    if len(normalized) < min_length:
        return _fail(f"{field_label} is required.")
    if len(normalized) > max_length:
        return _fail(f"{field_label} must be at most {max_length} characters.")
    problem = _plain_text_problem(value, normalized, field_label)
    return _fail(problem) if problem else _ok(normalized)


def validate_description(
    value: Any,
    *,
    field_label: str = "Description",
    max_length: int = DESCRIPTION_MAX_LENGTH,
    required: bool = False,
) -> ValidationResult:
    """Multi-line prose: descriptions, reasons, comments, feedback."""
    if value is None:
        return _fail(f"{field_label} is required.") if required else _ok(None)
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    normalized = normalize_text(value, allow_newlines=True)
    if not normalized:
        return _fail(f"{field_label} is required.") if required else _ok(None)
    if len(normalized) > max_length:
        return _fail(f"{field_label} must be at most {max_length} characters.")
    problem = _plain_text_problem(value, normalized, field_label)
    return _fail(problem) if problem else _ok(normalized)


def validate_plain_text(
    value: Any,
    *,
    field_label: str = "Value",
    max_length: int = PLAIN_TEXT_MAX_LENGTH,
    required: bool = False,
) -> ValidationResult:
    """Single-line free text — no newlines survive normalisation."""
    if value is None:
        return _fail(f"{field_label} is required.") if required else _ok(None)
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    normalized = normalize_text(value)
    if not normalized:
        return _fail(f"{field_label} is required.") if required else _ok(None)
    if len(normalized) > max_length:
        return _fail(f"{field_label} must be at most {max_length} characters.")
    problem = _plain_text_problem(value, normalized, field_label)
    return _fail(problem) if problem else _ok(normalized)


def validate_search_term(
    value: Any,
    *,
    field_label: str = "Search",
    max_length: int = SEARCH_MAX_LENGTH,
) -> ValidationResult:
    """A search box's contents.

    Unlike the backend's, this does **not** escape SQL wildcards — the desktop
    sends the term to the API, it does not build a query. Escaping here would
    put a literal backslash in front of every ``%`` the server then escapes
    again, and the user's search would stop matching.
    """
    if value is None:
        return _ok(None)
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    normalized = normalize_text(value)
    if not normalized:
        return _ok(None)
    if len(normalized) > max_length:
        return _fail(f"{field_label} must be at most {max_length} characters.")
    problem = _plain_text_problem(
        value, normalized, field_label, require_content=False
    )
    return _fail(problem) if problem else _ok(normalized)


# --- Email and password ----------------------------------------------------


def validate_email(value: Any, *, field_label: str = "Email") -> ValidationResult:
    """An email address, trimmed with its domain lower-cased."""
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    if CONTROL_CHARACTER_PATTERN.search(value):
        return _fail(f"{field_label} contains unsupported characters.")
    normalized = normalize_email(value)
    if not normalized:
        return _fail(f"{field_label} is required.")
    if len(normalized) > EMAIL_MAX_LENGTH:
        return _fail(f"{field_label} must be at most {EMAIL_MAX_LENGTH} characters.")
    if not EMAIL_PATTERN.match(normalized):
        return _fail("Please enter a valid email address.")
    return _ok(normalized)


def validate_password(value: Any, *, field_label: str = "Password") -> ValidationResult:
    """A password being *chosen*. Length policy only; the value is untouched.

    The desktop must be at least as permissive as the server here. If this
    trimmed a password, or refused one containing ``<``, someone whose real
    password has a trailing space or an angle bracket could not log in from the
    desktop app while the web client worked — and the failure would look like
    "wrong password", not like a validation bug.
    """
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    if len(value) < PASSWORD_MIN_LENGTH:
        return _fail(f"{field_label} must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(value) > PASSWORD_MAX_LENGTH:
        return _fail(f"{field_label} must be at most {PASSWORD_MAX_LENGTH} characters.")
    return _ok(value)


def validate_credential(value: Any, *, field_label: str = "Password") -> ValidationResult:
    """A password being presented at *login*. Upper bound only.

    An account may predate the current minimum length. Enforcing the minimum on
    the login form would lock that person out of the desktop client entirely,
    so only the ceiling applies.
    """
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    if not value:
        return _fail(f"{field_label} is required.")
    if len(value) > PASSWORD_MAX_LENGTH:
        return _fail(f"{field_label} must be at most {PASSWORD_MAX_LENGTH} characters.")
    return _ok(value)


def validate_username(value: Any, *, field_label: str = "Username") -> ValidationResult:
    """The desktop login field, which accepts either a username or an email.

    Held to plain-text rules rather than email rules precisely because it may
    legitimately be either. Rejecting anything without an ``@`` would break
    username logins, which this deployment supports.
    """
    return validate_plain_text(
        value, field_label=field_label, max_length=EMAIL_MAX_LENGTH, required=True
    )


# --- Numbers and identifiers -----------------------------------------------


def validate_integer(
    value: Any,
    *,
    field_label: str = "Value",
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> ValidationResult:
    """A whole number within an optional range."""
    if isinstance(value, bool):
        return _fail(f"{field_label} must be a number.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        try:
            number = int(value.strip(), 10)
        except ValueError:
            return _fail(f"{field_label} must be a whole number.")
    else:
        return _fail(f"{field_label} must be a whole number.")
    if minimum is not None and number < minimum:
        return _fail(f"{field_label} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        return _fail(f"{field_label} must be at most {maximum}.")
    return _ok(number)


def validate_identifier(value: Any, *, field_label: str = "Identifier") -> ValidationResult:
    """A database key: a positive whole number.

    In the dialogs this is what a combo box's ``currentData()`` should hold. A
    ``None`` there means nothing is selected, which is why the error reads as a
    missing selection rather than a malformed number.
    """
    if value is None:
        return _fail(f"{field_label} is required.")
    return validate_integer(
        value, field_label=field_label, minimum=IDENTIFIER_MIN, maximum=IDENTIFIER_MAX
    )


def validate_decimal(
    value: Any,
    *,
    field_label: str = "Value",
    minimum: Optional[Decimal] = None,
    maximum: Optional[Decimal] = None,
    allow_negative: bool = True,
) -> ValidationResult:
    """A fractional number. NaN and infinity are refused."""
    if isinstance(value, bool):
        return _fail(f"{field_label} must be a number.")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return _fail(f"{field_label} must be a number.")
    if not number.is_finite():
        return _fail(f"{field_label} must be a number.")
    if not allow_negative and number < 0:
        return _fail(f"{field_label} cannot be negative.")
    if minimum is not None and number < minimum:
        return _fail(f"{field_label} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        return _fail(f"{field_label} must be at most {maximum}.")
    return _ok(number)


# --- Strict formats --------------------------------------------------------


def validate_uuid(value: Any, *, field_label: str = "Identifier") -> ValidationResult:
    """A canonical hyphenated UUID, returned lower-cased."""
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    candidate = value.strip()
    if not UUID_PATTERN.match(candidate):
        return _fail(f"{field_label} is not a valid identifier.")
    return _ok(candidate.lower())


def validate_date(value: Any, *, field_label: str = "Date") -> ValidationResult:
    """A calendar date, from a ``date`` or a ``YYYY-MM-DD`` string."""
    if isinstance(value, datetime):
        return _ok(value.date())
    if isinstance(value, date):
        return _ok(value)
    if not isinstance(value, str):
        return _fail(f"{field_label} must be a date.")
    candidate = value.strip()
    if not DATE_PATTERN.match(candidate):
        return _fail(f"{field_label} must be in YYYY-MM-DD format.")
    try:
        return _ok(date.fromisoformat(candidate))
    except ValueError:
        return _fail(f"{field_label} is not a real date.")


def validate_enum(
    value: Any, allowed: Iterable[Any], *, field_label: str = "Value"
) -> ValidationResult:
    """A value drawn from a fixed set."""
    allowed_values = list(allowed)
    if value not in allowed_values:
        readable = ", ".join(str(item) for item in allowed_values)
        return _fail(f"{field_label} must be one of: {readable}.")
    return _ok(value)


def validate_domain(value: Any, *, field_label: str = "Domain") -> ValidationResult:
    """A hostname, lower-cased."""
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    candidate = normalize_text(value).lower()
    if not candidate:
        return _fail(f"{field_label} is required.")
    if len(candidate) > DOMAIN_MAX_LENGTH:
        return _fail(f"{field_label} must be at most {DOMAIN_MAX_LENGTH} characters.")
    if not DOMAIN_PATTERN.match(candidate):
        return _fail(f"{field_label} is not a valid domain.")
    return _ok(candidate)


def validate_url(
    value: Any, *, field_label: str = "URL", required: bool = False
) -> ValidationResult:
    """An absolute ``http``/``https`` URL.

    URL fields get URL rules, never the prose ones: ``?``, ``&``, ``=``, ``%``
    and ``#`` are ordinary URL punctuation that the plain-text checks would
    reject.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return _fail(f"{field_label} is required.") if required else _ok(None)
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    candidate = normalize_text(value)
    if len(candidate) > URL_MAX_LENGTH:
        return _fail(f"{field_label} must be at most {URL_MAX_LENGTH} characters.")
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return _fail(f"Please enter a valid {field_label}.")
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        return _fail(f"{field_label} must start with http:// or https://.")
    if not parsed.netloc:
        return _fail(f"Please enter a valid {field_label}.")
    return _ok(candidate)


def validate_all(*results: ValidationResult) -> ValidationResult:
    """Return the first failure among *results*, or the last success.

    Lets a dialog check several fields in declaration order and report the
    first problem, which is the order the fields appear on screen and so the
    order a person expects to be told about them.
    """
    for result in results:
        if not result.ok:
            return result
    return results[-1] if results else _ok(None)


def validate_version(value: Any, *, field_label: str = "Version") -> ValidationResult:
    """A ``major.minor.patch`` release version.

    Mirrors the backend rule exactly. Rejected rather than repaired: a version
    is the identity of a build, and quietly turning ``v1.2`` into ``1.2.0``
    would let two spellings name what the update system treats as one release.
    """
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    candidate = value.strip()
    if not candidate:
        return _fail(f"{field_label} is required.")
    if len(candidate) > VERSION_MAX_LENGTH:
        return _fail(f"{field_label} must be at most {VERSION_MAX_LENGTH} characters.")
    if not VERSION_PATTERN.match(candidate):
        return _fail(
            f"{field_label} must look like 1.2.3 — three numbers separated by dots."
        )
    return _ok(candidate)


def validate_sha256(value: Any, *, field_label: str = "Checksum") -> ValidationResult:
    """A SHA-256 digest, returned lower-cased.

    Case folding is the only transformation, and it is meaning-preserving:
    ``Get-FileHash`` reports upper case and ``shasum`` lower case for the very
    same bytes, so comparing the spellings rather than the digest would reject
    an artifact that downloaded perfectly.
    """
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    candidate = value.strip().lower()
    if not SHA256_PATTERN.match(candidate):
        return _fail(
            f"{field_label} must be a SHA-256 digest: "
            f"{SHA256_LENGTH} hexadecimal characters."
        )
    return _ok(candidate)


def validate_time_of_day(value: Any, *, field_label: str = "Time") -> ValidationResult:
    """A wall-clock time, ``HH:MM`` on a 24-hour clock.

    Mirrors the backend rule exactly. Rejected rather than repaired, so a
    schedule has one spelling per time of day.
    """
    if not isinstance(value, str):
        return _fail(f"{field_label} must be text.")
    candidate = value.strip()
    if not candidate:
        return _fail(f"{field_label} is required.")
    if not TIME_OF_DAY_PATTERN.match(candidate):
        return _fail(f"{field_label} must be a time like 09:30 (24-hour, HH:MM).")
    return _ok(candidate)


__all__ = [
    "ValidationResult",
    "looks_like_json_document",
    "find_structured_content",
    "validate_name",
    "validate_description",
    "validate_plain_text",
    "validate_search_term",
    "validate_email",
    "validate_password",
    "validate_credential",
    "validate_username",
    "validate_integer",
    "validate_identifier",
    "validate_decimal",
    "validate_uuid",
    "validate_date",
    "validate_enum",
    "validate_domain",
    "validate_url",
    "validate_version",
    "validate_time_of_day",
    "validate_sha256",
    "validate_all",
]
