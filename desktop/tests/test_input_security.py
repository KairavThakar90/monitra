"""
Hostile text on the desktop: what is refused on the way in, and what is shown literally on the way out.

Input: every name / description / reason the desktop sends goes through `core.validation` (the other
test modules pin each dialog); this module pins the shared rules against one list of malicious and one
list of legitimate values, the same lists the backend (`backend/tests/test_input_security_api.py`) and the
web client (`frontend/src/validation/__tests__/inputSecurity.test.ts`) use -- the backend being the control
and these being the early, friendly check.

Output: Qt decides on its own whether a string is rich text. A task or project named `<img src=//host/x>`,
or a server error that is a gateway's HTML page, must be shown as the text it is: never rendered, never
fetching anything.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import Qt as _GuiQt
from PySide6.QtWidgets import QLabel

from app.api.exceptions import error_detail
from core.text_safety import plain_tooltip, set_plain
from core.validation import (
    validate_description, validate_email, validate_name, validate_plain_text, validate_search_term, validate_url,
)

HOSTILE = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "javascript:alert(document.cookie)",
    'Ravi" onmouseover="alert(1)',
    '{"role": "administrator", "permissions": {"all": true}}',
    '["a", "b"]',
    "<?xml version='1.0'?><a/>",
    "{{7*7}}",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "Ravi\x00Acharya",
    "Ravi\x07Acharya",
    "!!!",
    # pasted into a 150-character box: cut short, so not valid JSON, and the whole-document check missed it
    '{ "id": 42, "name": "RandomUser", "active": true, "score": 87, "tags": ["alpha", "beta", "gamma"], "meta": { "createdAt": "2026-09-22T',
    '["alpha", "beta", "gam',
    '{"id":',
]

LEGITIMATE = [
    "Ravi Acharya", "Sinéad O'Connor", "Zoë Müller-Smith", "रवि आचार्य", "હાર્દિક રાવલ", "محمد علي", "王小明",
    "Иван Петров", "A. B. Rao Jr.", "R&D / Prototype #4 (50% done)", "Fix the login page - Non billable",
]


# ── Input ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_name_is_refused(value):
    assert validate_name(value).ok is False


@pytest.mark.parametrize("value", LEGITIMATE)
def test_a_real_name_is_accepted_unchanged(value):
    result = validate_name(value)
    assert result.ok and result.value == value


@pytest.mark.parametrize("value", [{"a": 1}, ["a"], 5, True])
def test_a_value_that_is_not_text_is_refused_not_stringified(value):
    assert validate_name(value).ok is False


def test_a_refusal_never_quotes_the_value_back():
    result = validate_name("<script>alert(1)</script>")
    assert not result.ok
    assert "<script>" not in result.error


@pytest.mark.parametrize("value", [
    "<script>alert(1)</script>", "bad\x00text", '{"a": 1}', "<?xml version='1.0'?>", "&#60;script&#62;",
])
def test_a_hostile_description_is_refused(value):
    assert validate_description(value).ok is False


def test_prose_with_punctuation_and_scripts_is_accepted():
    prose = "Fixed the login timeout (ticket #4521). Cost is $5, 5 < 10, a -> b, 100% of users: O'Brien & Sons.\nદિવસ સારો રહ્યો."
    assert validate_description(prose).ok is True


@pytest.mark.parametrize("value", ['$(".x").hide()', "'; DROP TABLE tasks; --", "x' OR '1'='1", "alert(document.cookie)"])
def test_code_is_refused_where_the_field_must_be_prose(value):
    assert validate_description(value, reject_code=True).ok is False


def test_email_url_and_search_rules():
    for bad in ("a@b.com\x00", "no-at-sign", "two@@example.com", "a b@example.com", '{"mail":"x@y.z"}', "<b>a@b.co</b>"):
        assert validate_email(bad).ok is False
    assert validate_email("o'brien+tag@example.co.uk").ok is True
    for bad in ("javascript:alert(1)", "data:text/html,<b>x</b>", "file:///etc/passwd", "//evil.example"):
        assert validate_url(bad).ok is False
    assert validate_url("https://example.com/notes?a=1&b=2").ok is True
    for ok in ("' OR 1=1 --", "100%", "'; DROP TABLE users; --"):
        assert validate_search_term(ok).ok is True            # a term is data; the server escapes it
    assert validate_search_term("<script>").ok is False
    assert validate_plain_text("one\ntwo").value == "one two"


# ── Output ────────────────────────────────────────────────────────────────────

MARKUP = ["<b>bold</b>", "<img src=//host/x.png>", "<a href='http://evil'>click</a>", "<script>alert(1)</script>"]


@pytest.mark.parametrize("text", MARKUP)
def test_a_tooltip_that_would_be_read_as_html_is_escaped(qapp, text):
    shown = plain_tooltip(text)
    assert _GuiQt.mightBeRichText(shown)                           # rich text, but of the escaped text...
    assert "<img" not in shown and "<b>" not in shown and "<a " not in shown and "<script" not in shown
    assert "&lt;" in shown                                     # ...which reads as the original


@pytest.mark.parametrize("text", ["Ravi Acharya", "R&D / Prototype #4", "5 < 10 and 7 > 3", "रवि", "Fix login - Non billable"])
def test_an_ordinary_tooltip_is_left_exactly_as_it_is(qapp, text):
    assert plain_tooltip(text) == text


def test_an_empty_tooltip_stays_empty_so_it_can_be_cleared(qapp):
    assert plain_tooltip("") == "" and plain_tooltip(None) == ""


def test_the_elided_label_that_shows_names_everywhere_does_not_render_a_markup_name(qapp):
    from ui.elided_label import ElidedLabel

    label = ElidedLabel("<img src=//host/x.png>Mallory")
    assert "<img" not in label.toolTip()
    label.setText("<b>x</b>")
    assert "<b>" not in label.toolTip()


def test_set_plain_fixes_the_format_before_the_text(qapp):
    label = QLabel()
    set_plain(label, "<b>not bold</b>")
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert label.text() == "<b>not bold</b>"


def test_the_task_list_error_line_escapes_a_server_message_but_keeps_its_retry_link(qapp):
    from ui.task_table import TaskSection

    api = MagicMock()
    api.timer_elapsed_seconds.return_value = 0
    api.is_timer_running.return_value = False
    section = TaskSection(api=api, task_service=MagicMock())

    section.set_error("<img src=//host/x.png> <b>boom</b>", retry=True)

    shown = section._status_label.text()
    assert "<img src=//host" not in shown and "<b>boom" not in shown
    assert "&lt;img" in shown
    assert '<a href="retry">Retry</a>' in shown                # the one piece of markup that is ours


def test_labels_that_show_server_text_are_plain_text(qapp):
    from ui.dashboard_window import StatusBar

    bar = StatusBar()
    assert bar._msg.textFormat() == Qt.TextFormat.PlainText
    bar.set_message("<b>Could not load tasks</b>")
    assert bar._msg.text() == "<b>Could not load tasks</b>"


@pytest.mark.parametrize("body", [
    "<html><body><h1>502 Bad Gateway</h1></body></html>",
    "<!DOCTYPE html><html></html>",
    "bad\x00body",
])
def test_a_gateways_page_is_never_shown_as_the_error_message(body):
    assert error_detail(body, "Could not reach the server.") == "Could not reach the server."


def test_the_backends_own_messages_and_short_plain_text_still_come_through():
    assert error_detail('{"detail": "Email already in use"}', "x") == "Email already in use"
    assert error_detail("Service temporarily busy", "x") == "Service temporarily busy"
    assert error_detail("", "fallback") == "fallback"
    assert error_detail('{"detail": [{"loc": ["body", "name"], "msg": "Name is required."}]}', "x") == "name: Name is required."


def test_a_duplicate_whose_name_the_rules_refuse_is_explained_and_not_sent(qapp):
    from ui.task_table import TaskSection

    api = MagicMock()
    api.timer_elapsed_seconds.return_value = 0
    api.is_timer_running.return_value = False
    service = MagicMock()
    section = TaskSection(api=api, task_service=service)
    row = MagicMock()
    row.project_id = 7
    row.task = {"id": 3, "name": "x" * 148, "description": ""}     # + " (Copy)" is over the 150 limit

    section._handle_duplicate_request(row)

    service.create_task.assert_not_called()
    assert api.notify.called and "cannot be duplicated" in api.notify.call_args.args[0]


def test_a_local_search_box_filters_on_the_validated_term_or_not_at_all():
    from core.text_safety import clean_search_text

    assert clean_search_text("  Ravi  ") == "Ravi"
    assert clean_search_text("R&D #4") == "R&D #4"
    assert clean_search_text("રવિ") == "રવિ"
    assert clean_search_text("100%") == "100%"
    for refused in ("<script>alert(1)</script>", "a\x00b", "x" * 101):
        assert clean_search_text(refused) == ""
    assert clean_search_text(None) == ""
