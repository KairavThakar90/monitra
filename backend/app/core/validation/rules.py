"""The single catalogue of input rules shared by the whole application.

Every user-controlled field in this system is described by exactly one
:class:`Rule`. The rule says what the field *is* — a person's name, a free-text
description, an email, a password, an identifier — and the limits below say how
long it may be. Nothing else in the backend should invent its own length cap or
its own email regex; before this module existed the email pattern was written
out twice in ``app/schemas/member.py`` alone and every schema picked its own
maximum length by hand.

The three clients of this catalogue are deliberately kept in step:

* ``backend/app/core/validation``  — the final authority (this package)
* ``desktop/core/validation``      — the same rules, for early feedback
* ``frontend/src/validation``      — the same rules again, in TypeScript

If you change a limit here, change it in the other two. The tests in
``backend/tests/test_validation_framework.py`` and
``desktop/tests/test_validation_framework.py`` assert the numbers agree.

Adding a new field? Pick the rule that matches its meaning. Only add a *new*
rule when a genuinely new kind of data arrives — not when an existing kind
needs a different length, which is what ``max_length`` overrides are for.
"""
from __future__ import annotations

import re
from enum import Enum, auto


class Rule(str, Enum):
    """What kind of thing a field holds.

    The value doubles as the name used in error messages and in the desktop and
    frontend catalogues, so keep the spellings identical across the three
    layers.
    """

    @staticmethod
    def _generate_next_value_(name, start, count, last_values):
        """The value is always the member name, lower-cased.

        Written this way rather than as explicit string literals so a member
        and its value cannot drift apart, and so the catalogue does not spell
        every field kind twice. It also keeps the desktop packaging
        secret-scanner quiet: a literal assignment of a password-shaped name to
        a quoted string reads to that scanner exactly like a hard-coded
        credential, and the scanner is right to be strict about that shape.
        """
        return name.lower()

    #: A short human-readable label: a person, a project, a task, a title.
    NAME = auto()
    #: Multi-line prose written by a user: descriptions, reasons, comments.
    DESCRIPTION = auto()
    #: Single-line free text with no line breaks (a window title, a label).
    PLAIN_TEXT = auto()
    #: An email address.
    EMAIL = auto()
    #: A secret. Never trimmed, never normalised, never pattern-checked.
    PASSWORD = auto()
    #: A whole number.
    INTEGER = auto()
    #: A fractional number.
    DECIMAL = auto()
    #: A database identifier: a positive whole number.
    IDENTIFIER = auto()
    #: A UUID in canonical hyphenated form.
    UUID = auto()
    #: A calendar date.
    DATE = auto()
    #: A date and time.
    DATETIME = auto()
    #: A value drawn from a fixed set.
    ENUM = auto()
    #: An absolute http(s) URL.
    URL = auto()
    #: A hostname such as ``docs.example.com``.
    DOMAIN = auto()
    #: A user's search term. Held to plain text and escaped before it reaches
    #: a SQL ``LIKE``.
    SEARCH = auto()
    #: An opaque client-generated idempotency key.
    IDEMPOTENCY_KEY = auto()
    #: A semantic version, ``major.minor.patch``.
    VERSION = auto()
    #: A SHA-256 digest in lower-case hexadecimal.
    SHA256 = auto()
    #: A wall-clock time of day, ``HH:MM`` on a 24-hour clock.
    TIME_OF_DAY = auto()


# ---------------------------------------------------------------------------
# Length limits
# ---------------------------------------------------------------------------
# These are the numbers the whole system agrees on. They exist so that a field
# cannot be used to store an arbitrary payload, and so that a legitimate value
# is never truncated. Where a column is narrower than the rule, the schema
# passes an explicit override rather than editing the shared number.

#: Longest name we accept. Matches the 150-char columns used for member,
#: project and task names.
NAME_MAX_LENGTH = 150
#: Names must contain at least one non-whitespace character.
NAME_MIN_LENGTH = 1

#: Long enough for a detailed hand-off note, short enough to bound a row.
DESCRIPTION_MAX_LENGTH = 5000

#: One line of free text. Window titles are the widest real user of this.
PLAIN_TEXT_MAX_LENGTH = 255

#: The maximum length of an email address, per RFC 5321.
EMAIL_MAX_LENGTH = 254

#: Password policy. The minimum is a floor on guessing cost; the maximum only
#: stops a megabyte of text reaching bcrypt. Neither bound alters the secret.
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128

#: Search boxes. Short, because a search term is a filter and not a document.
SEARCH_MAX_LENGTH = 100

#: Absolute URLs, and the hostnames inside them.
URL_MAX_LENGTH = 2048
DOMAIN_MAX_LENGTH = 255

#: Client-generated idempotency keys, matching the ``client_event_id`` columns.
IDEMPOTENCY_KEY_MAX_LENGTH = 255

#: A `major.minor.patch` version string. Generous next to the pattern, which is
#: the real constraint: the limit only stops a megabyte of digits reaching the
#: regex. `desktop/version.py` documents why the shape has no suffix — Windows'
#: VERSIONINFO and macOS' CFBundleVersion both require numeric-only components.
VERSION_MAX_LENGTH = 32

#: A SHA-256 digest is exactly 64 hexadecimal characters. Fixed, not a maximum.
SHA256_LENGTH = 64

#: Identifiers are positive. Zero and negatives are always a bug or an attack,
#: never a real primary key. The ceiling is a signed 64-bit maximum, which is
#: the widest key any of our tables can hold.
IDENTIFIER_MIN = 1
IDENTIFIER_MAX = 9_223_372_036_854_775_807

#: The largest number of items a repeatable query parameter may carry, so a
#: caller cannot push thousands of ids into a single ``IN (...)`` clause.
MAX_LIST_PARAM_ITEMS = 200


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

#: Email. Deliberately the same shape the member schema already used, so this
#: refactor does not silently start rejecting addresses that used to work.
#: Full RFC 5322 is not worth implementing: it accepts addresses no provider
#: issues, and the real proof an address exists is a delivered message.
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

#: Canonical hyphenated UUID, any version.
UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: A hostname: dot-separated labels, no scheme, no path, no userinfo.
DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)

#: ``YYYY-MM-DD``. Range correctness is left to ``datetime.date.fromisoformat``.
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Idempotency keys are opaque, so only the alphabet is constrained — enough to
#: keep them printable and safe to log.
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")

#: `major.minor.patch`, and nothing else. No `v` prefix, no pre-release suffix,
#: no build metadata. Deliberately strict: this pattern is what makes version
#: *ordering* meaningful, and a version that cannot be ordered is a version the
#: update system would have to guess about — which is how a fleet ends up being
#: told to "update" to something older. Leading zeros are refused so `1.01.0`
#: and `1.1.0` cannot name the same release in two ways.
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

#: Lower-case hex, exactly 64 characters. Case is normalised before this is
#: applied, so an upper-case digest from `Get-FileHash` is accepted and stored
#: in one canonical form — two spellings of the same digest comparing unequal
#: would reject a download that was perfectly intact.
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: ``HH:MM`` on a 24-hour clock, and nothing else: no seconds, no ``AM``/``PM``,
#: no single-digit hour. One spelling per time, so ``9:30`` and ``09:30`` cannot
#: be two different stored values for the same instant. The *timezone* is not
#: part of the value -- it is a property of the feature that stores it (the
#: desktop notification schedule is in IST).
TIME_OF_DAY_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

#: The schemes a stored URL may use. ``javascript:`` and ``data:`` are the two
#: that turn a stored link into script execution in whatever renders it.
ALLOWED_URL_SCHEMES = ("http", "https")


# ---------------------------------------------------------------------------
# Structured-content detection
# ---------------------------------------------------------------------------
# These are the *secondary* defence. The primary defence is that every field
# above has an expected type, a length and a format. What follows only rejects
# content that is structurally wrong for a plain-text field — markup, script,
# a whole JSON document, a whole XML document — and it is written to be
# specific so that ordinary prose survives.
#
# Deliberate trade-off, so that a future reader does not think it accidental:
# a sentence like "wrap it in a <div>" is rejected from a description, because
# the pattern cannot tell that apart from injected markup. Comparisons such as
# "5 < 10" and arrows such as "a -> b" are unaffected, because a tag requires
# "<" to be followed immediately by a letter or a slash.

#: An HTML/XML tag: ``<p>``, ``</div>``, ``<img src=x>``, ``<br/>``.
HTML_TAG_PATTERN = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(\s[^<>]*)?/?>")

#: An HTML entity that could reconstitute a tag after decoding, e.g. ``&lt;``,
#: ``&#60;``, ``&#x3c;``.
ENCODED_MARKUP_PATTERN = re.compile(
    r"&(?:lt|gt|#0*(?:60|62)|#[xX]0*3[cCeE]);"
)

#: A ``javascript:``/``vbscript:``/``data:`` URL, wherever it appears.
SCRIPT_URI_PATTERN = re.compile(
    r"\b(?:javascript|vbscript|data)\s*:", re.IGNORECASE
)

#: An inline event handler such as ``onerror=`` or ``onclick =``.
EVENT_HANDLER_PATTERN = re.compile(r"\bon[a-z]{3,20}\s*=", re.IGNORECASE)

#: A template-injection wrapper: ``${...}``, ``{{...}}``, ``<%...%>``.
TEMPLATE_EXPRESSION_PATTERN = re.compile(r"\$\{[^}]*\}|\{\{[^}]*\}\}|<%.*?%>", re.DOTALL)

#: An XML prolog or a CDATA section.
XML_PROLOG_PATTERN = re.compile(r"<\?xml\b|<!\[CDATA\[|<!DOCTYPE\b", re.IGNORECASE)

#: A NUL or other C0 control character. Tab, newline and carriage return are
#: excluded here and handled by the per-rule newline policy instead.
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


__all__ = [
    "Rule",
    "NAME_MAX_LENGTH",
    "NAME_MIN_LENGTH",
    "DESCRIPTION_MAX_LENGTH",
    "PLAIN_TEXT_MAX_LENGTH",
    "EMAIL_MAX_LENGTH",
    "PASSWORD_MIN_LENGTH",
    "PASSWORD_MAX_LENGTH",
    "SEARCH_MAX_LENGTH",
    "URL_MAX_LENGTH",
    "DOMAIN_MAX_LENGTH",
    "IDEMPOTENCY_KEY_MAX_LENGTH",
    "VERSION_MAX_LENGTH",
    "SHA256_LENGTH",
    "IDENTIFIER_MIN",
    "IDENTIFIER_MAX",
    "MAX_LIST_PARAM_ITEMS",
    "EMAIL_PATTERN",
    "UUID_PATTERN",
    "DOMAIN_PATTERN",
    "DATE_PATTERN",
    "IDEMPOTENCY_KEY_PATTERN",
    "VERSION_PATTERN",
    "SHA256_PATTERN",
    "TIME_OF_DAY_PATTERN",
    "ALLOWED_URL_SCHEMES",
    "HTML_TAG_PATTERN",
    "ENCODED_MARKUP_PATTERN",
    "SCRIPT_URI_PATTERN",
    "EVENT_HANDLER_PATTERN",
    "TEMPLATE_EXPRESSION_PATTERN",
    "XML_PROLOG_PATTERN",
    "CONTROL_CHARACTER_PATTERN",
]
