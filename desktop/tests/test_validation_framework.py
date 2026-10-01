"""Tests for ``core.validation`` — the desktop's shared input rules.

Two things are being protected here.

The first is the behaviour of each rule: that legitimate input a real person
types is accepted, and that structured payloads are refused. The accept cases
matter at least as much as the reject cases — a validator that blocks "O'Brien"
or a description containing "5 < 10" is a bug that will be reported as "the app
won't let me save my work".

The second is that the desktop's limits still match the backend's. That check
reads both ``rules.py`` files and compares them, so changing a maximum on one
side and forgetting the other fails here rather than in production, where it
would appear as a form that accepts a value the server then rejects — after the
entry has already been queued for sync.
"""
from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from core.validation import (
    DESCRIPTION_MAX_LENGTH,
    NAME_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    validate_credential,
    validate_date,
    validate_decimal,
    validate_description,
    validate_domain,
    validate_email,
    validate_enum,
    validate_identifier,
    validate_integer,
    validate_name,
    validate_password,
    validate_plain_text,
    validate_search_term,
    validate_url,
    validate_username,
    validate_uuid,
)
from core.validation.validators import validate_all


# ---------------------------------------------------------------------------
# Legitimate input must not be blocked
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("Website Redesign", "Website Redesign"),
        # Real names carry apostrophes, hyphens, periods and ampersands.
        ("O'Brien & Sons, Ltd.", "O'Brien & Sons, Ltd."),
        ("Jean-Luc Picard", "Jean-Luc Picard"),
        # Non-Latin scripts: an ASCII allowlist would lock out most of the world.
        ("プロジェクト計画", "プロジェクト計画"),
        ("Проект Альфа", "Проект Альфа"),
        ("José Álvarez", "José Álvarez"),
        # Surrounding whitespace is trimmed and interior runs collapse.
        ("  Acme   Corp  ", "Acme Corp"),
        ("Q3 2026 — Phase 2 (final)", "Q3 2026 — Phase 2 (final)"),
        ("100% Coverage", "100% Coverage"),
    ],
)
def test_ordinary_names_are_accepted(value, expected):
    result = validate_name(value, field_label="Project name")
    assert result.ok, result.error
    assert result.value == expected


@pytest.mark.parametrize(
    "value",
    [
        "Fixed the timer bug. It only happened when 5 < 10 and a -> b.",
        "Line one.\nLine two.\n\nA final paragraph.",
        "Cost is 50% of budget; see item #4 (approx. $1,200).",
        # A bug report quoting a payload mid-sentence is the single most common
        # legitimate reason for a description to contain braces.
        'The API returned {"error": 1} and then stopped responding.',
        "Réunion à 14h — préparer l'ordre du jour.",
        "Math: 2 < 3 > 1, and x = y & z.",
    ],
)
def test_ordinary_descriptions_are_accepted(value):
    result = validate_description(value, field_label="Description")
    assert result.ok, result.error


def test_description_normalises_windows_line_endings():
    """A Windows QTextEdit hands back CRLF; the backend counts LF.

    If the two disagreed, a description near the limit would pass here and be
    refused on upload.
    """
    result = validate_description("first\r\nsecond\rthird")
    assert result.ok
    assert result.value == "first\nsecond\nthird"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("  user@example.com ", "user@example.com"),
        # The domain is case-insensitive; the local part is not, so only the
        # domain is folded.
        ("User.Name+tag@Example.COM", "User.Name+tag@example.com"),
    ],
)
def test_emails_are_accepted_and_normalised(value, expected):
    result = validate_email(value)
    assert result.ok, result.error
    assert result.value == expected


# ---------------------------------------------------------------------------
# Passwords: the one field nothing may alter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "password",
    [
        "p@ssw0rd!",
        "<script>alert(1)</script>",   # a legitimate, if eccentric, password
        "{}[]<>!@#$%^&*()_+-=",
        "correct horse battery staple",
        "パスワード12345",
        "  leading and trailing  ",
    ],
)
def test_passwords_accept_every_character_and_are_never_modified(password):
    """Special characters are legitimate in a password.

    Applying the plain-text rules here would reject perfectly good secrets, and
    trimming would change one — either way the user is locked out and the error
    says "wrong password".
    """
    result = validate_password(password)
    assert result.ok, result.error
    assert result.value == password, "the password was modified"


def test_password_minimum_length_is_enforced():
    result = validate_password("short")
    assert not result.ok
    assert str(PASSWORD_MIN_LENGTH) in result.error


def test_login_credential_accepts_a_short_legacy_password():
    """Login must not enforce the *chosen*-password minimum.

    An account predating the current policy still has to be able to sign in;
    refusing its password at the form would lock the person out of the desktop
    client entirely.
    """
    result = validate_credential("old1")
    assert result.ok, result.error
    assert result.value == "old1"


def test_login_credential_still_rejects_an_empty_password():
    assert not validate_credential("").ok


# ---------------------------------------------------------------------------
# Structured payloads must be refused
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        "<div>test</div>",
        '<img src=x onerror=alert(1)>',
        "<iframe src='http://evil.example'></iframe>",
        "<a href='javascript:alert(1)'>click</a>",
        "<?xml version='1.0'?><note>hi</note>",
        "<![CDATA[payload]]>",
        "&lt;script&gt;alert(1)&lt;/script&gt;",
        "${7*7}",
        "{{constructor.constructor('alert(1)')()}}",
        "javascript:alert(1)",
        "click onerror=alert(1) here",
    ],
)
def test_markup_and_script_payloads_are_rejected_from_text_fields(payload):
    assert not validate_description(payload, field_label="Description").ok
    assert not validate_name(payload, field_label="Name").ok


@pytest.mark.parametrize(
    "payload",
    ['{"key":"value"}', '["unexpected","json"]', '{"a": {"b": [1,2,3]}}', "  {}  "],
)
def test_a_whole_json_document_is_rejected_where_prose_is_expected(payload):
    result = validate_description(payload, field_label="Description")
    assert not result.ok
    assert "JSON" in result.error


@pytest.mark.parametrize(
    "payload",
    ["!!!", "@@@", "...", "---", "???", "$$$", "***", "&&&", "##", "()",
     "/", "\\", ".", "-", "_", "+++", "~~~", ";;", "!@#$%^&*()", "   ---   "],
)
def test_a_value_of_only_punctuation_is_refused(payload):
    """Regression: these were saved.

    Length and structure alone passed them — "!!!" has a length, is not markup
    and is not JSON — so a required field could be satisfied without being
    answered.
    """
    assert not validate_name(payload).ok
    assert not validate_description(payload, required=True).ok
    assert not validate_plain_text(payload, required=True).ok


def test_the_punctuation_only_message_says_what_to_do():
    result = validate_name("!!!", field_label="Task name")
    assert not result.ok
    assert result.error == "Task name must contain at least one letter or number."


@pytest.mark.parametrize(
    "value",
    ["Fixed!!!", "...and then it crashed", "C++", "v2.0!", "R&D / Prototype #4",
     "50% !!!", "A", "プロジェクト", "Проект", "مشروع"],
)
def test_punctuation_alongside_real_content_is_still_accepted(value):
    """The check asks for *some* content, not for the absence of symbols.

    The non-Latin cases matter too: a letter is a letter in any script, so this
    must never become an ASCII test.
    """
    assert validate_name(value).ok


def test_a_search_term_may_still_be_punctuation_only():
    """The backend does not require content in a search box.

    Searching for "???" is harmless — it filters, it is not stored. Requiring
    content here would make the desktop stricter than the server, which is the
    one direction a client must never be.
    """
    assert validate_search_term("???").ok


def test_control_characters_are_rejected_not_silently_stripped():
    """The value must be refused, not quietly repaired.

    ``normalize_text`` would remove the NUL and return something that looks
    clean; validating the raw input first is what makes this fail loudly.
    """
    result = validate_name("Project\x00Name")
    assert not result.ok
    assert "unsupported characters" in result.error


def test_terminal_escape_sequences_are_rejected():
    assert not validate_description("status: \x1b[31mFAILED\x1b[0m").ok


# ---------------------------------------------------------------------------
# Lengths
# ---------------------------------------------------------------------------

def test_a_name_at_the_limit_is_accepted_and_one_over_is_not():
    assert validate_name("x" * NAME_MAX_LENGTH).ok
    over = validate_name("x" * (NAME_MAX_LENGTH + 1))
    assert not over.ok
    assert str(NAME_MAX_LENGTH) in over.error


def test_a_description_one_over_the_limit_is_rejected():
    assert validate_description("x" * DESCRIPTION_MAX_LENGTH).ok
    assert not validate_description("x" * (DESCRIPTION_MAX_LENGTH + 1)).ok


@pytest.mark.parametrize("blank", ["", "   ", "\t\n  "])
def test_a_required_field_rejects_whitespace_only_input(blank):
    assert not validate_name(blank, field_label="Task name").ok
    assert not validate_description(blank, required=True).ok


def test_an_optional_description_maps_blank_to_none():
    """Two spellings of "not filled in" would make every later query test both."""
    result = validate_description("   ", required=False)
    assert result.ok
    assert result.value is None


# ---------------------------------------------------------------------------
# Numbers, identifiers and strict formats
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [(42, 42), ("42", 42), (" 7 ", 7)])
def test_identifiers_accept_positive_whole_numbers(value, expected):
    result = validate_identifier(value)
    assert result.ok, result.error
    assert result.value == expected


@pytest.mark.parametrize("value", [0, -1, "abc", 1.5, "", None])
def test_identifiers_reject_anything_that_is_not_a_positive_key(value):
    assert not validate_identifier(value).ok


def test_identifier_rejects_a_boolean():
    """``bool`` is an ``int`` subclass, so ``True`` would otherwise pass as 1."""
    assert not validate_identifier(True).ok


def test_a_missing_combo_selection_reads_as_a_required_field():
    result = validate_identifier(None, field_label="Project")
    assert not result.ok
    assert result.error == "Project is required."


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "abc"])
def test_decimal_rejects_non_finite_and_non_numeric_values(value):
    """NaN parses happily and then poisons every later comparison."""
    assert not validate_decimal(value).ok


def test_decimal_enforces_its_range():
    assert validate_decimal("12.50", minimum=Decimal("0")).ok
    assert not validate_decimal("-1", allow_negative=False).ok
    assert not validate_decimal("101", maximum=Decimal("100")).ok


def test_integer_range_is_enforced():
    assert validate_integer("5", minimum=1, maximum=10).ok
    assert not validate_integer("0", minimum=1).ok
    assert not validate_integer("11", maximum=10).ok


def test_uuid_accepts_canonical_form_and_rejects_the_rest():
    assert validate_uuid("123E4567-E89B-12D3-A456-426614174000").value == (
        "123e4567-e89b-12d3-a456-426614174000"
    )
    assert not validate_uuid("123e4567e89b12d3a456426614174000").ok
    assert not validate_uuid("not-a-uuid").ok


def test_date_accepts_iso_and_rejects_impossible_days():
    assert validate_date("2026-09-08").ok
    assert not validate_date("08/09/2026").ok
    assert not validate_date("2026-02-30").ok


def test_enum_names_the_permitted_values():
    """Unlike a password, the allowed set is part of the API contract."""
    allowed = ["open", "in_progress", "done"]
    assert validate_enum("open", allowed).ok
    rejected = validate_enum("deleted", allowed, field_label="Status")
    assert not rejected.ok
    assert "in_progress" in rejected.error


# ---------------------------------------------------------------------------
# URLs get URL rules, not prose rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://docs.example.com/path/to/page?query=1&other=2#fragment",
        "https://example.com/search?q=100%25",
    ],
)
def test_legitimate_urls_are_accepted(url):
    """A URL is full of punctuation the plain-text rules would refuse."""
    assert validate_url(url, field_label="URL").ok


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "not a url", "ftp://example.com/f"],
)
def test_dangerous_and_malformed_urls_are_rejected(url):
    assert not validate_url(url, field_label="URL").ok


def test_domain_is_lowercased_and_validated():
    assert validate_domain("Docs.Example.COM").value == "docs.example.com"
    assert not validate_domain("not a domain").ok


# ---------------------------------------------------------------------------
# Desktop-specific helpers
# ---------------------------------------------------------------------------

def test_username_field_accepts_both_a_username_and_an_email():
    """The desktop login box takes either; email rules would break usernames."""
    assert validate_username("jsmith").ok
    assert validate_username("jsmith@example.com").ok
    assert not validate_username("   ").ok


def test_search_terms_are_not_wildcard_escaped_on_the_desktop():
    """The desktop sends the term to the API; it does not build the query.

    Escaping here would put a backslash in front of every ``%`` that the server
    then escapes again, and the search would stop matching.
    """
    result = validate_search_term("100% done_now")
    assert result.ok
    assert result.value == "100% done_now"


def test_plain_text_collapses_a_smuggled_second_line():
    result = validate_plain_text("first\nsecond", field_label="Title")
    assert result.ok
    assert result.value == "first second"


def test_validate_all_reports_the_first_failure_in_field_order():
    """A person expects to be told about the first broken field on screen."""
    outcome = validate_all(
        validate_name("Fine", field_label="Name"),
        validate_identifier(None, field_label="Project"),
        validate_description("", required=True, field_label="Description"),
    )
    assert not outcome.ok
    assert outcome.error == "Project is required."


def test_validate_all_passes_through_when_everything_is_valid():
    outcome = validate_all(
        validate_name("Fine"), validate_identifier(3), validate_description("Notes")
    )
    assert outcome.ok


# ---------------------------------------------------------------------------
# The desktop and backend catalogues must agree
# ---------------------------------------------------------------------------

def _load_backend_rules():
    """Import the backend's rules module directly from disk.

    Loaded by path rather than by import because the backend is a separate
    application that is not on the desktop's ``sys.path``. If it is not present
    — a packaged desktop checkout, for instance — the comparison is skipped
    rather than failed, since the absence proves nothing about the rules.
    """
    backend_rules = (
        Path(__file__).resolve().parents[2]
        / "backend" / "app" / "core" / "validation" / "rules.py"
    )
    if not backend_rules.is_file():
        pytest.skip("backend/ is not present in this checkout")
    spec = importlib.util.spec_from_file_location("_backend_rules", backend_rules)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SHARED_LIMIT_NAMES = [
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
    "IDENTIFIER_MIN",
    "IDENTIFIER_MAX",
    "MAX_LIST_PARAM_ITEMS",
]


@pytest.mark.parametrize("limit_name", SHARED_LIMIT_NAMES)
def test_desktop_limits_match_the_backend(limit_name):
    """A one-sided limit change is a bug; catch it here, not in production."""
    from core.validation import rules as desktop_rules

    backend_rules = _load_backend_rules()
    assert getattr(desktop_rules, limit_name) == getattr(backend_rules, limit_name), (
        f"{limit_name} differs between desktop and backend"
    )


def test_desktop_rule_kinds_match_the_backend():
    """The two ``Rule`` enums name the same set of field kinds."""
    from core.validation import rules as desktop_rules

    backend_rules = _load_backend_rules()
    assert {rule.value for rule in desktop_rules.Rule} == {
        rule.value for rule in backend_rules.Rule
    }


@pytest.mark.parametrize(
    "pattern_name",
    ["EMAIL_PATTERN", "UUID_PATTERN", "DOMAIN_PATTERN", "DATE_PATTERN",
     "TIME_OF_DAY_PATTERN",
     "HTML_TAG_PATTERN", "ENCODED_MARKUP_PATTERN", "SCRIPT_URI_PATTERN",
     "EVENT_HANDLER_PATTERN", "XML_PROLOG_PATTERN", "CONTROL_CHARACTER_PATTERN"],
)
def test_desktop_patterns_match_the_backend(pattern_name):
    """Identical patterns mean the client refuses exactly what the server does."""
    from core.validation import rules as desktop_rules

    backend_rules = _load_backend_rules()
    assert (
        getattr(desktop_rules, pattern_name).pattern
        == getattr(backend_rules, pattern_name).pattern
    ), f"{pattern_name} differs between desktop and backend"


# ---------------------------------------------------------------------------
# Time of day (the notification schedule's HH:MM)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["09:30", "00:00", "23:59", "12:00", "10:05"])
def test_a_time_of_day_in_hh_mm_is_accepted(value):
    from core.validation import validate_time_of_day

    result = validate_time_of_day(value)

    assert result.ok and result.value == value


@pytest.mark.parametrize(
    "value",
    ["9:30", "24:00", "09:60", "09:30:00", "", "   ", "0930", "09-30", "9.30",
     "09:3", "ab:cd", "09:30 PM", None, 930, 9.5, True, ["09:30"], {"t": "09:30"}],
)
def test_anything_else_is_rejected_as_a_time_of_day(value):
    from core.validation import validate_time_of_day

    result = validate_time_of_day(value)

    assert not result.ok and result.error
