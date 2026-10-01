"""The validation functions themselves — one per rule in :mod:`.rules`.

Each ``validate_*`` returns the normalised value or raises
:class:`InputValidationError`, whose message is safe to show a user: it says
what is wrong with the input and never quotes a pattern, a stack trace, or
anything about how the check is implemented.

These are plain functions on purpose. Pydantic types in :mod:`.types` call
them, the desktop mirrors them, and a service that needs to check something
outside a schema can import one directly.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional, Sequence
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
    IDEMPOTENCY_KEY_MAX_LENGTH,
    IDEMPOTENCY_KEY_PATTERN,
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
from .sanitizer import (
    collapse_whitespace,
    escape_like_wildcards,
    normalize_email,
    normalize_text,
)


class InputValidationError(ValueError):
    """A user-controlled value was rejected.

    Subclasses :class:`ValueError` so that returning it from a Pydantic
    ``field_validator`` produces FastAPI's standard ``422 {"detail": [...]}``
    response — the shape this API already returns for validation failures. No
    new error envelope is introduced.

    The message is written for the person who typed the value.
    """


# ---------------------------------------------------------------------------
# Structured-content detection (the secondary defence layer)
# ---------------------------------------------------------------------------

def looks_like_json_document(value: str) -> bool:
    """True when the *entire* value is a JSON object or array.

    Scoped to the whole field on purpose. A description that happens to mention
    ``{"key": "value"}`` mid-sentence is a person explaining a payload, which is
    exactly what a bug report looks like; a field whose complete contents parse
    as JSON is a client sending structured data where prose was expected.
    """
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
    """Return a user-facing reason if *value* is not plain text, else ``None``.

    The order is chosen so the most specific and most alarming explanation wins;
    a payload usually trips several of these at once.
    """
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

    ``str.isalnum`` is Unicode-aware, so this is satisfied by Japanese, Cyrillic,
    Arabic and every other script as readily as by ASCII — the test is "is there
    real content here", not "is this English".
    """
    return any(character.isalnum() for character in value)


def require_meaningful_content(value: str, *, field_label: str) -> None:
    """Raise unless *value* contains at least one letter or digit.

    Length and structure checks alone let a field through that is nothing but
    punctuation — ``!!!``, ``...``, ``@@@`` all have a length, contain no markup
    and are not JSON, so every other rule passed them. They are not names or
    descriptions; they are a way to satisfy a "required" field without
    answering it, and they were being saved.

    Punctuation is still perfectly welcome *alongside* real content: this asks
    only that something in the value is a letter or a number.

    Known edge: a value made purely of emoji or symbols (``👍``) is refused,
    since no codepoint in it is alphanumeric. That is accepted deliberately —
    for a project name or a task description it is the right answer far more
    often than not.
    """
    if not contains_letter_or_digit(value):
        raise InputValidationError(
            f"{field_label} must contain at least one letter or number."
        )


def reject_control_characters(raw: str, *, field_label: str) -> None:
    """Raise if *raw* contains a control character other than tab/newline.

    This runs on the value **as submitted**, before any normalisation. That
    ordering is deliberate: :func:`~app.core.validation.sanitizer.normalize_text`
    would happily remove a NUL and hand back something that looks clean, which
    is precisely the silent-repair behaviour this framework refuses. A NUL or an
    ANSI escape in a field a human typed is not a typo to be tidied up — it is a
    client doing something it should not, and the request should fail loudly.
    """
    if CONTROL_CHARACTER_PATTERN.search(raw):
        raise InputValidationError(f"{field_label} contains unsupported characters.")


def ensure_plain_text(value: str, *, field_label: str) -> None:
    """Raise unless *value* is plain human-readable text."""
    reject_control_characters(value, field_label=field_label)
    reason = find_structured_content(value)
    if reason is not None:
        raise InputValidationError(f"{field_label}: {reason}")


# ---------------------------------------------------------------------------
# Text rules
# ---------------------------------------------------------------------------

def validate_name(
    value: Any,
    *,
    field_label: str = "Name",
    max_length: int = NAME_MAX_LENGTH,
    min_length: int = NAME_MIN_LENGTH,
) -> str:
    """A short human-readable label: a person, project, task or title.

    Unicode letters, digits, spaces and ordinary punctuation are all accepted —
    real names contain apostrophes, hyphens, periods and non-Latin scripts, and
    an allowlist of ASCII letters would lock out a large part of the world. What
    is rejected is content that is structurally not a label: markup, scripts,
    whole JSON or XML documents, control characters.
    """
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    reject_control_characters(value, field_label=field_label)
    normalized = collapse_whitespace(normalize_text(value)).strip()
    if len(normalized) < min_length:
        raise InputValidationError(f"{field_label} is required.")
    if len(normalized) > max_length:
        raise InputValidationError(
            f"{field_label} must be at most {max_length} characters."
        )
    ensure_plain_text(normalized, field_label=field_label)
    require_meaningful_content(normalized, field_label=field_label)
    return normalized


def validate_description(
    value: Any,
    *,
    field_label: str = "Description",
    max_length: int = DESCRIPTION_MAX_LENGTH,
    required: bool = False,
) -> Optional[str]:
    """Multi-line prose: descriptions, reasons, comments, feedback.

    Line breaks, punctuation, digits and Unicode are all fine — this is where
    people write sentences, and over-restricting it makes the product hostile.
    The limits are length and structure only.
    """
    if value is None:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    reject_control_characters(value, field_label=field_label)
    normalized = normalize_text(value, allow_newlines=True)
    if not normalized:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if len(normalized) > max_length:
        raise InputValidationError(
            f"{field_label} must be at most {max_length} characters."
        )
    ensure_plain_text(normalized, field_label=field_label)
    require_meaningful_content(normalized, field_label=field_label)
    return normalized


def validate_plain_text(
    value: Any,
    *,
    field_label: str = "Value",
    max_length: int = PLAIN_TEXT_MAX_LENGTH,
    required: bool = False,
) -> Optional[str]:
    """Single-line free text — no newlines survive normalisation."""
    if value is None:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    reject_control_characters(value, field_label=field_label)
    normalized = normalize_text(value)
    if not normalized:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if len(normalized) > max_length:
        raise InputValidationError(
            f"{field_label} must be at most {max_length} characters."
        )
    ensure_plain_text(normalized, field_label=field_label)
    require_meaningful_content(normalized, field_label=field_label)
    return normalized


def validate_search_term(
    value: Any,
    *,
    field_label: str = "Search",
    max_length: int = SEARCH_MAX_LENGTH,
) -> Optional[str]:
    """A search box's contents.

    Returned ready to interpolate into a ``LIKE`` pattern: the wildcards a user
    typed are escaped so they match literally. Always pair with ``escape="\\\\"``
    at the query site.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    reject_control_characters(value, field_label=field_label)
    normalized = normalize_text(value)
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise InputValidationError(
            f"{field_label} must be at most {max_length} characters."
        )
    ensure_plain_text(normalized, field_label=field_label)
    return escape_like_wildcards(normalized)


# ---------------------------------------------------------------------------
# Email and password
# ---------------------------------------------------------------------------

def validate_email(value: Any, *, field_label: str = "Email") -> str:
    """An email address, trimmed with its domain lower-cased."""
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    reject_control_characters(value, field_label=field_label)
    normalized = normalize_email(value)
    if not normalized:
        raise InputValidationError(f"{field_label} is required.")
    if len(normalized) > EMAIL_MAX_LENGTH:
        raise InputValidationError(
            f"{field_label} must be at most {EMAIL_MAX_LENGTH} characters."
        )
    if not EMAIL_PATTERN.match(normalized):
        raise InputValidationError("Please enter a valid email address.")
    return normalized


def validate_password(value: Any, *, field_label: str = "Password") -> str:
    """A password, returned byte-for-byte as it was typed.

    Passwords are the one field this framework never touches. It does not trim
    them, does not normalise Unicode, does not collapse whitespace, does not
    apply the plain-text allowlist, and does not run structured-content
    detection. Every one of those would silently change a secret and lock
    someone out of their account — a leading space or a combining accent is a
    legitimate part of a password, and ``<`` is a perfectly good character.

    Only the length policy applies, because length is the one property that can
    be checked without altering or inspecting the value. The upper bound exists
    so a huge body of text cannot be pushed through the password hasher.
    """
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    if len(value) < PASSWORD_MIN_LENGTH:
        raise InputValidationError(
            f"{field_label} must be at least {PASSWORD_MIN_LENGTH} characters."
        )
    if len(value) > PASSWORD_MAX_LENGTH:
        raise InputValidationError(
            f"{field_label} must be at most {PASSWORD_MAX_LENGTH} characters."
        )
    return value


def validate_credential(value: Any, *, field_label: str = "Password") -> str:
    """A password being presented for *login*, not being chosen.

    Deliberately weaker than :func:`validate_password`: an account created
    before today's policy may hold a shorter secret, and refusing to transmit it
    would lock that person out while telling an attacker that the length policy
    can be probed at the login form. Only the upper bound is enforced, purely to
    cap the work handed to the hasher. The value is never modified.
    """
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    if not value:
        raise InputValidationError(f"{field_label} is required.")
    if len(value) > PASSWORD_MAX_LENGTH:
        raise InputValidationError(
            f"{field_label} must be at most {PASSWORD_MAX_LENGTH} characters."
        )
    return value


# ---------------------------------------------------------------------------
# Numbers and identifiers
# ---------------------------------------------------------------------------

def validate_integer(
    value: Any,
    *,
    field_label: str = "Value",
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """A whole number within an optional range.

    ``bool`` is rejected explicitly: it is an ``int`` subclass in Python, so
    ``True`` would otherwise sail through as ``1``.
    """
    if isinstance(value, bool):
        raise InputValidationError(f"{field_label} must be a number.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        try:
            number = int(value.strip(), 10)
        except ValueError:
            raise InputValidationError(f"{field_label} must be a whole number.") from None
    else:
        raise InputValidationError(f"{field_label} must be a whole number.")
    if minimum is not None and number < minimum:
        raise InputValidationError(f"{field_label} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise InputValidationError(f"{field_label} must be at most {maximum}.")
    return number


def validate_identifier(value: Any, *, field_label: str = "Identifier") -> int:
    """A database key: a positive whole number inside the 64-bit range."""
    return validate_integer(
        value,
        field_label=field_label,
        minimum=IDENTIFIER_MIN,
        maximum=IDENTIFIER_MAX,
    )


def validate_decimal(
    value: Any,
    *,
    field_label: str = "Value",
    minimum: Optional[Decimal] = None,
    maximum: Optional[Decimal] = None,
    allow_negative: bool = True,
) -> Decimal:
    """A fractional number.

    Parsed as :class:`~decimal.Decimal` rather than ``float`` because these are
    hours and money-shaped quantities, where binary rounding is visible to the
    user. NaN and infinity are refused: both parse happily and then poison every
    comparison and aggregate downstream.
    """
    if isinstance(value, bool):
        raise InputValidationError(f"{field_label} must be a number.")
    try:
        number = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError, ArithmeticError):
        raise InputValidationError(f"{field_label} must be a number.") from None
    if not number.is_finite():
        raise InputValidationError(f"{field_label} must be a number.")
    if not allow_negative and number < 0:
        raise InputValidationError(f"{field_label} cannot be negative.")
    if minimum is not None and number < minimum:
        raise InputValidationError(f"{field_label} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise InputValidationError(f"{field_label} must be at most {maximum}.")
    return number


# ---------------------------------------------------------------------------
# Strict formats
# ---------------------------------------------------------------------------

def validate_uuid(value: Any, *, field_label: str = "Identifier") -> str:
    """A canonical hyphenated UUID, returned lower-cased."""
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    candidate = value.strip()
    if not UUID_PATTERN.match(candidate):
        raise InputValidationError(f"{field_label} is not a valid identifier.")
    return candidate.lower()


def validate_date(value: Any, *, field_label: str = "Date") -> date:
    """A calendar date, from a ``date`` or a ``YYYY-MM-DD`` string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be a date.")
    candidate = value.strip()
    if not DATE_PATTERN.match(candidate):
        raise InputValidationError(f"{field_label} must be in YYYY-MM-DD format.")
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        raise InputValidationError(f"{field_label} is not a real date.") from None


def validate_enum(
    value: Any,
    allowed: Iterable[Any],
    *,
    field_label: str = "Value",
) -> Any:
    """A value drawn from a fixed set.

    The permitted values are named in the error, because unlike a rejected
    password or a rejected search term they are not a secret — they are part of
    the API's contract, and a caller cannot fix the request without them.
    """
    allowed_values = list(allowed)
    if value not in allowed_values:
        readable = ", ".join(str(item) for item in allowed_values)
        raise InputValidationError(f"{field_label} must be one of: {readable}.")
    return value


def validate_domain(value: Any, *, field_label: str = "Domain") -> str:
    """A hostname, lower-cased."""
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    reject_control_characters(value, field_label=field_label)
    candidate = normalize_text(value).lower()
    if not candidate:
        raise InputValidationError(f"{field_label} is required.")
    if len(candidate) > DOMAIN_MAX_LENGTH:
        raise InputValidationError(
            f"{field_label} must be at most {DOMAIN_MAX_LENGTH} characters."
        )
    if not DOMAIN_PATTERN.match(candidate):
        raise InputValidationError(f"{field_label} is not a valid domain.")
    return candidate


def validate_url(
    value: Any,
    *,
    field_label: str = "URL",
    required: bool = False,
) -> Optional[str]:
    """An absolute ``http``/``https`` URL.

    URL fields get URL rules, never the plain-text ones: a legitimate URL is
    full of characters — ``?``, ``&``, ``=``, ``%``, ``#`` — that the prose
    checks would reject. The scheme allowlist is the security control here,
    because it is what stops a stored ``javascript:`` link from becoming script
    execution in whatever later renders it as an anchor.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    reject_control_characters(value, field_label=field_label)
    candidate = normalize_text(value)
    if len(candidate) > URL_MAX_LENGTH:
        raise InputValidationError(
            f"{field_label} must be at most {URL_MAX_LENGTH} characters."
        )
    try:
        parsed = urlparse(candidate)
    except ValueError:
        raise InputValidationError(f"Please enter a valid {field_label}.") from None
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise InputValidationError(
            f"{field_label} must start with http:// or https://."
        )
    if not parsed.netloc:
        raise InputValidationError(f"Please enter a valid {field_label}.")
    return candidate


def validate_idempotency_key(
    value: Any,
    *,
    field_label: str = "Client event id",
    required: bool = False,
) -> Optional[str]:
    """An opaque client-generated de-duplication key."""
    if value is None:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    candidate = value.strip()
    if not candidate:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if len(candidate) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise InputValidationError(
            f"{field_label} must be at most {IDEMPOTENCY_KEY_MAX_LENGTH} characters."
        )
    if not IDEMPOTENCY_KEY_PATTERN.match(candidate):
        raise InputValidationError(f"{field_label} has an unsupported format.")
    return candidate


def validate_version(
    value: Any,
    *,
    field_label: str = "Version",
    required: bool = True,
) -> Optional[str]:
    """A ``major.minor.patch`` release version.

    Rejected rather than repaired, deliberately. A version is the identity of a
    build, and quietly turning ``v1.2`` into ``1.2.0`` would let two different
    spellings name what the update system then treats as one release.
    """
    if value is None:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    candidate = value.strip()
    if not candidate:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if len(candidate) > VERSION_MAX_LENGTH:
        raise InputValidationError(
            f"{field_label} must be at most {VERSION_MAX_LENGTH} characters."
        )
    if not VERSION_PATTERN.match(candidate):
        raise InputValidationError(
            f"{field_label} must look like 1.2.3 — three numbers separated by dots."
        )
    return candidate


def validate_sha256(
    value: Any,
    *,
    field_label: str = "Checksum",
    required: bool = True,
) -> Optional[str]:
    """A SHA-256 digest, normalised to lower-case hexadecimal.

    Case folding is the one transformation applied, and it is meaning-preserving:
    ``Get-FileHash`` reports upper case and ``shasum`` reports lower case for the
    very same bytes, so storing whichever arrived would make an intact download
    fail its own integrity check.
    """
    if value is None:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    candidate = value.strip().lower()
    if not candidate:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if not SHA256_PATTERN.match(candidate):
        raise InputValidationError(
            f"{field_label} must be a SHA-256 digest: "
            f"{SHA256_LENGTH} hexadecimal characters."
        )
    return candidate


def validate_time_of_day(
    value: Any,
    *,
    field_label: str = "Time",
    required: bool = True,
) -> Optional[str]:
    """A wall-clock time, ``HH:MM`` on a 24-hour clock.

    Rejected rather than repaired: ``9:30`` or ``9.30`` or ``09:30:00`` is not
    quietly turned into ``09:30``, so a schedule row has exactly one spelling
    for a time and the client is told what shape is wanted.
    """
    if value is None:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if not isinstance(value, str):
        raise InputValidationError(f"{field_label} must be text.")
    candidate = value.strip()
    if not candidate:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if not TIME_OF_DAY_PATTERN.match(candidate):
        raise InputValidationError(
            f"{field_label} must be a time like 09:30 (24-hour, HH:MM)."
        )
    return candidate


def validate_weekdays(
    value: Any,
    *,
    field_label: str = "Days",
    required: bool = True,
) -> Optional[list]:
    """A non-empty set of weekdays, ``0`` (Monday) to ``6`` (Sunday).

    Every member goes through the ``ENUM`` rule, so a stray ``7`` or ``"mon"``
    is named in the error. Duplicates are folded and the result is sorted, so
    ``[4, 0, 0]`` and ``[0, 4]`` are one stored value.
    """
    if value is None:
        if required:
            raise InputValidationError(f"{field_label} is required.")
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        raise InputValidationError(f"{field_label} must be a list of weekday numbers.")
    days = []
    for item in value:
        # A boolean is an int to Python; it is never a weekday.
        if isinstance(item, bool) or not isinstance(item, int):
            raise InputValidationError(
                f"{field_label} must be one of: 0, 1, 2, 3, 4, 5, 6."
            )
        days.append(validate_enum(item, range(7), field_label=field_label))
    if not days:
        raise InputValidationError(f"{field_label}: choose at least one day.")
    return sorted(set(days))


def validate_id_list(
    value: Optional[Sequence[Any]],
    *,
    field_label: str = "Selection",
    max_items: Optional[int] = None,
    unique: bool = True,
) -> Optional[list]:
    """A repeatable list of identifiers.

    The cardinality cap is the point: an unbounded repeatable query parameter
    lets one request push thousands of ids into a single ``IN (...)``, which is
    a cheap way to make the database do expensive work.
    """
    from .rules import MAX_LIST_PARAM_ITEMS

    if value is None:
        return None
    limit = MAX_LIST_PARAM_ITEMS if max_items is None else max_items
    items = list(value)
    if len(items) > limit:
        raise InputValidationError(
            f"{field_label} cannot contain more than {limit} items."
        )
    validated = [
        validate_identifier(item, field_label=field_label) for item in items
    ]
    if unique and len(set(validated)) != len(validated):
        raise InputValidationError(f"{field_label} contains duplicate entries.")
    return validated


__all__ = [
    "InputValidationError",
    "looks_like_json_document",
    "find_structured_content",
    "ensure_plain_text",
    "reject_control_characters",
    "require_meaningful_content",
    "contains_letter_or_digit",
    "validate_name",
    "validate_description",
    "validate_plain_text",
    "validate_search_term",
    "validate_email",
    "validate_password",
    "validate_credential",
    "validate_integer",
    "validate_identifier",
    "validate_decimal",
    "validate_uuid",
    "validate_date",
    "validate_enum",
    "validate_domain",
    "validate_url",
    "validate_idempotency_key",
    "validate_version",
    "validate_sha256",
    "validate_time_of_day",
    "validate_weekdays",
    "validate_id_list",
]
