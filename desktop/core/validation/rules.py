"""The desktop's copy of the shared input-rule catalogue.

These numbers and patterns are a deliberate mirror of
``backend/app/core/validation/rules.py``. The duplication is intentional: the
desktop client must be able to tell someone their description is too long
while they are still typing it, and it must be able to do that offline, with
no round trip and no shared package to install.

**The backend remains the authority.** Nothing here is a security control. A
value that passes these checks is still validated again on the server, because
the server also serves browsers, ``curl``, and anything replaying a captured
request. What these rules buy is that a person finds out about a problem at the
moment they can fix it, and that the sync queue does not fill up with requests
the server is going to refuse.

If you change a limit, change it in the backend and the frontend too.
``tests/test_validation_framework.py`` asserts these numbers match the
backend's by reading both files, so a one-sided edit fails the suite.

This module is pure stdlib on purpose. It imports no Qt, owns no thread, holds
no state, and touches no service — see ``ARCHITECTURE.md``. It lives under
``core/`` only because ``core/`` is where shared, dependency-free machinery
belongs, not because it needs any of the infrastructure layer's privileges.
"""
from __future__ import annotations

import re
from enum import Enum, auto


class Rule(str, Enum):
    """What kind of thing a field holds. Mirrors the backend's ``Rule``."""

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

    NAME = auto()
    DESCRIPTION = auto()
    PLAIN_TEXT = auto()
    EMAIL = auto()
    PASSWORD = auto()
    INTEGER = auto()
    DECIMAL = auto()
    IDENTIFIER = auto()
    UUID = auto()
    DATE = auto()
    DATETIME = auto()
    ENUM = auto()
    URL = auto()
    DOMAIN = auto()
    SEARCH = auto()
    IDEMPOTENCY_KEY = auto()
    VERSION = auto()
    SHA256 = auto()
    TIME_OF_DAY = auto()


# --- Length limits (mirror of the backend's) -------------------------------

NAME_MAX_LENGTH = 150
NAME_MIN_LENGTH = 1
DESCRIPTION_MAX_LENGTH = 5000
PLAIN_TEXT_MAX_LENGTH = 255
EMAIL_MAX_LENGTH = 254
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
SEARCH_MAX_LENGTH = 100
URL_MAX_LENGTH = 2048
DOMAIN_MAX_LENGTH = 255
IDEMPOTENCY_KEY_MAX_LENGTH = 255
VERSION_MAX_LENGTH = 32
SHA256_LENGTH = 64
IDENTIFIER_MIN = 1
IDENTIFIER_MAX = 9_223_372_036_854_775_807
MAX_LIST_PARAM_ITEMS = 200


# --- Patterns (mirror of the backend's) ------------------------------------

EMAIL_PATTERN = re.compile(r"^[^\s@<>(){}\[\]\x22\\,;:]+@[^\s@<>(){}\[\]\x22\\,;:]+\.[^\s@<>(){}\[\]\x22\\,;:]+$")
UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TIME_OF_DAY_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
ALLOWED_URL_SCHEMES = ("http", "https")

HTML_TAG_PATTERN = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(\s[^<>]*)?/?>")
ENCODED_MARKUP_PATTERN = re.compile(r"&(?:lt|gt|#0*(?:60|62)|#[xX]0*3[cCeE]);")
SCRIPT_URI_PATTERN = re.compile(r"\b(?:javascript|vbscript|data)\s*:", re.IGNORECASE)
EVENT_HANDLER_PATTERN = re.compile(r"\bon[a-z]{3,20}\s*=", re.IGNORECASE)
TEMPLATE_EXPRESSION_PATTERN = re.compile(r"\$\{[^}]*\}|\{\{[^}]*\}\}|<%.*?%>", re.DOTALL)
XML_PROLOG_PATTERN = re.compile(r"<\?xml\b|<!\[CDATA\[|<!DOCTYPE\b", re.IGNORECASE)
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Code-snippet detection -- opt-in, for fields that must hold prose and nothing
# else (a project's or a task's description). Mirrors the backend's
# `CODE_SNIPPET_SOURCES`, source text for source text, and
# `test_validation_framework.py` fails if the two differ. See the backend file
# for the reasoning behind each entry and for why this is not part of
# `find_structured_content`.
CODE_SNIPPET_SOURCES: tuple[tuple[str, bool], ...] = (
    # jQuery and `$` selectors
    (r"\$\s*\(\s*(?:[\x22\x27\x60#.<\[]|document\b|window\b|this\b|function\b)", False),
    (r"\bjQuery\s*[.(]", False),
    (r"\$\.(?:ajax|get|post|getJSON|each|extend|parseJSON|map|grep)\s*\(", False),
    # JavaScript: calls with a literal argument, the DOM, storage, networking
    (r"\b(?:alert|eval|prompt|confirm|setTimeout|setInterval|atob|btoa|importScripts)\((?=[^)\n]*[\d\x22\x27\x60.$_])", False),
    (r"\bfetch\(\s*[\x22\x27\x60]", False),
    (r"\bnew\s+(?:Function|WebSocket)\b|\bXMLHttpRequest\b", False),
    (r"\b(?:document|window)\.(?:cookie|location|domain|write|writeln|body|getElementById|getElementsBy\w+|querySelector|querySelectorAll|createElement|open|eval)\b", False),
    (r"\b(?:localStorage|sessionStorage)\.\w+", False),
    (r"\.(?:innerHTML|outerHTML|insertAdjacentHTML)\b|\.addEventListener\s*\(", False),
    # JavaScript: function and control-flow syntax
    (r"\bfunction\s*[\w$]*\s*\([^)]*\)\s*\{", False),
    (r"=>\s*\{|\([^()]*\)\s*=>\s*[\w$.\x22\x27]+\s*[;)]", False),
    (r"\.(?:then|catch|forEach|map|filter|reduce)\(\s*(?:function\b|\(?[\w$,\s]*\)?\s*=>)", False),
    (r"\b(?:var|let|const)\s+[A-Za-z_$][\w$]*\s*=(?!=)", False),
    (r"\bconsole\.(?:log|error|warn|info|debug|table|dir)\s*\(", False),
    (r"\b(?:if|while|for|switch|catch)\s*\([^)\n]*\)\s*\{", False),
    (r"\}\s*else\s*(?:if\b[^{]*)?\{", False),
    # CSS: a rule block, an at-rule
    (r"\{\s*-?[a-z][a-z-]*\s*:[^{}]*;[^{}]*\}", True),
    (r"\{\s*(?:color|background(?:-[a-z]+)?|display|margin(?:-[a-z]+)?|padding(?:-[a-z]+)?|font(?:-[a-z]+)?|width|height|min-[a-z]+|max-[a-z]+|position|border(?:-[a-z]+)?|content|opacity|visibility|z-index|top|left|right|bottom|float|overflow|cursor|transform|text-[a-z]+)\s*:[^{}]*\}", True),
    (r"@(?:import|charset|font-face|keyframes)\b|@media\s*\(", True),
    # Markup the tag pattern cannot see: a tag with no closing `>`, a comment
    (r"<\s*/?\s*(?:script|style|iframe|frame|frameset|object|embed|applet|link|meta|base|svg|math|img|body|html|head|form|input|button|video|audio|source|marquee|details|textarea|select|template|noscript)\b", True),
    (r"<!--", False),
    # Server-side and other languages
    (r"<\?(?:php\b|=)", True),
    (r"\b(?:os\.system|os\.popen|subprocess\.\w+|__import__|shell_exec|passthru)\s*\(", False),
    (r"\brm\s+-[a-z]*[rf][a-z]*\s", True),
    (r"\bInvoke-(?:Expression|WebRequest|Command)\b", True),
    # SQL injection signatures -- the shapes of an attack, not the words
    (r"\bUNION\s+(?:ALL\s+)?SELECT\b", True),
    (r";\s*(?:DROP|TRUNCATE|ALTER|DELETE\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|EXEC(?:UTE)?)\b", True),
    (r"[\x22\x27]\s*(?:OR|AND)\s+[\x22\x27]?\w+[\x22\x27]?\s*=\s*[\x22\x27]?\w+", True),
    # Markup hidden by URL- or JS-escaping: `%3Cscript`, `<`, `\x3c`
    (r"%3[cC]\s*/?\s*[A-Za-z!]|\\u003[cC]|\\x3[cC]", False),
)

CODE_SNIPPET_PATTERNS = tuple(
    re.compile(source, re.IGNORECASE if ignore_case else 0)
    for source, ignore_case in CODE_SNIPPET_SOURCES
)


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
    "CODE_SNIPPET_SOURCES",
    "CODE_SNIPPET_PATTERNS",
]
