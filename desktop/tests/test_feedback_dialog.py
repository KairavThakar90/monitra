"""Feedback & Help — the dialog's contract.

Two things are worth protecting here. The first is validation: nothing leaves
the machine until a category is chosen and a real message is typed, and a
failed submission must never cost the user what they wrote. The second is that
a double-click cannot produce two feedback records — the guard is the task
runner's de-duplication key, so the test asserts on the key actually used.
"""
import pytest

from ui.feedback_dialog import (
    CATEGORY_PLACEHOLDERS, DEFAULT_PLACEHOLDER, FEEDBACK_CATEGORIES,
    PLACEHOLDER_CATEGORY, SUBMIT_KEY, FeedbackDialog,
)


class _StubApi:
    """Stands in for BackgroundApi.

    `run_in_background` records the submission instead of running it, and
    honours the same de-duplication contract as TaskRunner: a second submit
    under a key already in flight returns None and is not run.
    """

    def __init__(self):
        self.calls = []
        self.notifications = []
        self.cancelled = []
        self._in_flight = set()

    def run_in_background(self, fn, *, on_success=None, on_error=None, key=None):
        if key in self._in_flight:
            return None
        self._in_flight.add(key)
        self.calls.append({"fn": fn, "on_success": on_success,
                           "on_error": on_error, "key": key})
        return object()

    def cancel_key(self, key):
        self.cancelled.append(key)
        self._in_flight.discard(key)

    def notify(self, message, level=None, key=None):
        self.notifications.append((message, level, key))


def _drain(qapp):
    for _ in range(6):
        qapp.processEvents()


@pytest.fixture
def submissions():
    return []


@pytest.fixture
def dialog(qapp, submissions):
    def _submitter(category, message):
        submissions.append((category, message))
        return {"id": 1, "category": category, "message": message,
                "status": "new", "created_at": "2026-09-03T10:00:00Z"}

    api = _StubApi()
    widget = FeedbackDialog(api, submitter=_submitter)
    widget.show()
    _drain(qapp)
    yield widget
    widget._alive = False
    widget.deleteLater()


def _choose(dialog, wire_value):
    index = dialog.category_combo.findData(wire_value)
    assert index >= 0
    dialog.category_combo.setCurrentIndex(index)


def test_the_dropdown_offers_exactly_the_six_supported_categories(dialog):
    labels = [dialog.category_combo.itemText(i)
              for i in range(dialog.category_combo.count())]
    assert labels == [PLACEHOLDER_CATEGORY] + [label for label, _ in FEEDBACK_CATEGORIES]
    assert labels[1:] == [
        "Suggestion", "Report a Problem", "General Feedback",
        "Need Help", "Account / Login Issue", "Other",
    ]


def test_no_category_is_selected_when_the_dialog_opens(dialog):
    assert dialog.category_combo.currentData() is None
    assert dialog.message_edit.placeholderText() == DEFAULT_PLACEHOLDER


def test_the_message_placeholder_follows_the_selected_category(dialog, qapp):
    for _label, value in FEEDBACK_CATEGORIES:
        _choose(dialog, value)
        _drain(qapp)
        assert dialog.message_edit.placeholderText() == CATEGORY_PLACEHOLDERS[value]


def test_submitting_without_a_category_sends_nothing_and_says_why(dialog, submissions):
    dialog.message_edit.setPlainText("Something useful.")
    dialog._on_submit()

    assert submissions == []
    assert dialog.api.calls == []
    assert dialog.status_label.isVisible()
    assert "category" in dialog.status_label.text().lower()


def test_submitting_an_empty_message_sends_nothing_and_says_why(dialog):
    _choose(dialog, "suggestion")
    dialog._on_submit()

    assert dialog.api.calls == []
    assert "message" in dialog.status_label.text().lower()


def test_a_whitespace_only_message_is_treated_as_empty(dialog):
    _choose(dialog, "need_help")
    dialog.message_edit.setPlainText("   \n\t   ")
    dialog._on_submit()

    assert dialog.api.calls == []
    assert "message" in dialog.status_label.text().lower()


def test_an_over_long_message_is_refused_before_any_request_is_made(dialog):
    from app.feedback.service import MESSAGE_MAX_LENGTH

    _choose(dialog, "other")
    dialog.message_edit.setPlainText("x" * (MESSAGE_MAX_LENGTH + 1))
    dialog._on_submit()

    assert dialog.api.calls == []
    assert "too long" in dialog.status_label.text().lower()


def test_a_valid_submission_sends_the_trimmed_message_and_the_wire_category(dialog, submissions):
    _choose(dialog, "report_a_problem")
    dialog.message_edit.setPlainText("  The timer resets on resume.  ")
    dialog._on_submit()

    assert len(dialog.api.calls) == 1
    dialog.api.calls[0]["fn"]()
    assert submissions == [("report_a_problem", "The timer resets on resume.")]


def test_the_button_reads_submitting_and_is_disabled_while_the_request_is_in_flight(dialog):
    _choose(dialog, "suggestion")
    dialog.message_edit.setPlainText("A thought.")
    dialog._on_submit()

    assert dialog.submit_btn.text() == "Submitting..."
    assert not dialog.submit_btn.isEnabled()
    assert not dialog.cancel_btn.isEnabled()


def test_a_second_click_while_submitting_cannot_create_a_second_record(dialog):
    _choose(dialog, "suggestion")
    dialog.message_edit.setPlainText("A thought.")
    dialog._on_submit()
    dialog._on_submit()
    dialog._on_submit()

    assert len(dialog.api.calls) == 1
    assert dialog.api.calls[0]["key"] == SUBMIT_KEY


def test_a_successful_submission_notifies_and_closes(dialog, qapp):
    _choose(dialog, "general_feedback")
    dialog.message_edit.setPlainText("Nice app.")
    dialog._on_submit()
    dialog.api.calls[0]["on_success"]({"id": 1, "status": "new"})
    _drain(qapp)

    message, _level, key = dialog.api.notifications[0]
    assert "submitted successfully" in message
    assert key == "feedback-submitted"
    assert not dialog.isVisible()


def test_a_failed_submission_keeps_what_the_user_typed_and_restores_the_button(dialog, qapp):
    _choose(dialog, "account_login_issue")
    dialog.message_edit.setPlainText("I cannot sign in.")
    dialog._on_submit()
    dialog.api.calls[0]["on_error"](
        Exception("Unable to submit feedback. Please check your internet connection and try again.")
    )
    _drain(qapp)

    assert dialog.isVisible()
    assert dialog.message_edit.toPlainText() == "I cannot sign in."
    assert dialog.category_combo.currentData() == "account_login_issue"
    assert dialog.submit_btn.text() == "Submit"
    assert dialog.submit_btn.isEnabled()
    assert "internet connection" in dialog.status_label.text()


def _dark_pixels(widget, strip_width=40):
    """Count non-background pixels in the widget's right-hand strip."""
    image = widget.grab().toImage()
    count = 0
    for x in range(max(0, image.width() - strip_width), image.width()):
        for y in range(image.height()):
            if image.pixelColor(x, y).lightness() < 150:
                count += 1
    return count


def test_the_category_field_actually_paints_a_drop_down_arrow(dialog, qapp):
    """Styling `::drop-down` stops Qt painting the arrow entirely.

    The field then looks like a plain text box with no sign that it opens,
    which is how it first shipped and what was reported.
    """
    _drain(qapp)
    assert _dark_pixels(dialog.category_combo) > 0


def test_the_character_counter_never_overlaps_the_message_field(dialog, qapp):
    _drain(qapp)
    counter = dialog.counter_label.geometry()
    message = dialog.message_edit.geometry()

    assert not counter.intersects(message)
    # It belongs with the Message label, above the field it counts.
    assert counter.bottom() <= message.top()


def test_the_counter_tracks_what_is_typed(dialog, qapp):
    dialog.message_edit.setPlainText("improve UI")
    _drain(qapp)

    assert dialog.counter_label.text().startswith("10 / ")


def test_cancel_sends_nothing(dialog, submissions):
    _choose(dialog, "other")
    dialog.message_edit.setPlainText("Never mind.")
    dialog._on_cancel()

    assert submissions == []
    assert dialog.api.calls == []


def test_a_late_reply_cannot_touch_a_dialog_the_user_already_closed(dialog, qapp):
    _choose(dialog, "suggestion")
    dialog.message_edit.setPlainText("A thought.")
    dialog._on_submit()
    dialog._alive = False

    # Neither callback may raise, and neither may notify.
    dialog.api.calls[0]["on_success"]({"id": 1})
    dialog.api.calls[0]["on_error"](Exception("boom"))
    _drain(qapp)

    assert dialog.api.notifications == []


# ── Attachments ───────────────────────────────────────────────────────────────
#
# The attachment section is optional. What these protect: a user who attaches
# nothing sees no change; what is chosen is checked when it is chosen, with the
# exact sentence; a retry of an unchanged payload reuses its idempotency key
# and an edited one never does; and nothing leaves the machine on Cancel.

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest

from app.feedback.service import MAX_TOTAL_ATTACHMENT_BYTES, format_file_size
from ui.styles import ERROR

TYPE_MESSAGE = "That file type isn't supported. Please attach a PNG, JPG or WEBP image."
TOO_LARGE = "Attachment is too large. Maximum allowed size is 10 MB."
TOO_MANY = "Maximum 3 attachments allowed."
UNREADABLE = "That file couldn't be read. Please choose another."


def _image(tmp_path, name, fmt, width=64, height=48):
    path = tmp_path / name
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0x3366CC)
    assert image.save(str(path), fmt), f"Qt cannot write {fmt} here"
    return str(path)


def _padded_png(tmp_path, name, total):
    path = tmp_path / name
    with open(_image(tmp_path, "seed-" + name, "PNG"), "rb") as seed:
        head = seed.read()
    path.write_bytes(head + b"\0" * (total - len(head)))
    return str(path)


@pytest.fixture
def attach_calls():
    return []


@pytest.fixture
def attach_dialog(qapp, submissions, attach_calls):
    def _submitter(category, message):
        submissions.append((category, message))
        return {"id": 1}

    def _attachment_submitter(category, message, paths, client_op):
        attach_calls.append((category, message, list(paths), client_op))
        return {"id": 2, "duplicate": False, "attachments": []}

    widget = FeedbackDialog(
        _StubApi(), submitter=_submitter, attachment_submitter=_attachment_submitter
    )
    widget.show()
    _drain(qapp)
    yield widget
    widget._alive = False
    widget.deleteLater()


def _pick(dialog, qapp, *paths):
    dialog._choose_files = lambda: list(paths)
    dialog._on_attach_clicked()
    _drain(qapp)


def _ready(dialog):
    _choose(dialog, "report_a_problem")
    dialog.message_edit.setPlainText("The timer resets on resume.")


def _fail_and_report_key(dialog, qapp, attach_calls):
    """Submit, run the work, fail it, and return the key that attempt used."""
    dialog._on_submit()
    dialog.api.calls[-1]["fn"]()
    dialog.api._in_flight.clear()
    dialog.api.calls[-1]["on_error"](Exception("Try again."))
    _drain(qapp)
    return attach_calls[-1][3]


def test_without_an_attachment_submitter_the_section_is_not_offered(dialog):
    assert not dialog.attachment_section.isVisible()


def test_a_submission_without_an_attachment_is_exactly_what_it_was(
    attach_dialog, submissions, attach_calls,
):
    _ready(attach_dialog)
    attach_dialog._on_submit()

    attach_dialog.api.calls[0]["fn"]()
    assert submissions == [("report_a_problem", "The timer resets on resume.")]
    assert attach_calls == []
    assert attach_dialog.api.calls[0]["key"] == SUBMIT_KEY


def test_the_section_offers_the_label_the_button_and_the_helper_text(attach_dialog):
    assert attach_dialog.attachment_section.isVisible()
    assert attach_dialog.attach_btn.isEnabled()
    assert attach_dialog.attach_btn.text().strip() == "Attach file"
    assert not attach_dialog.attach_btn.icon().isNull()
    assert attach_dialog.attach_help.text() == (
        "Optional — attach a screenshot or file to help us understand the issue."
    )
    assert not attach_dialog.attachment_note.isVisible()


def test_a_chosen_png_shows_its_name_size_type_and_a_thumbnail(attach_dialog, qapp, tmp_path):
    path = _image(tmp_path, "login-error.png", "PNG", 300, 100)
    _pick(attach_dialog, qapp, path)

    (row,) = attach_dialog._rows
    assert row.name_label.full_text() == "login-error.png"
    assert row.name_label.toolTip() == "login-error.png"
    assert row.size_label.text() == format_file_size(os.path.getsize(path))
    assert row.type_label.text() == "PNG image"
    assert row.has_thumbnail
    pixmap = row.thumb_label.pixmap()
    assert not pixmap.isNull()
    logical = pixmap.deviceIndependentSize()
    assert logical.width() <= 40 and logical.height() <= 40
    # Aspect ratio is kept: 300x100 is three times as wide as it is tall.
    assert logical.width() == pytest.approx(logical.height() * 3, abs=2)
    assert attach_dialog.api.calls == []        # choosing sends nothing


def test_a_picture_that_cannot_be_decoded_shows_a_generic_glyph(attach_dialog, qapp, tmp_path):
    # A valid PNG signature and nothing else: it passes the header check and
    # cannot be decoded.
    path = tmp_path / "truncated.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 40)
    _pick(attach_dialog, qapp, str(path))

    (row,) = attach_dialog._rows
    assert not row.has_thumbnail
    assert not row.thumb_label.pixmap().isNull()


@pytest.mark.parametrize("name,fmt,label", [
    ("a.jpg", "JPG", "JPEG image"),
    ("b.JPEG", "JPG", "JPEG image"),
    ("c.webp", "WEBP", "WEBP image"),
])
def test_jpeg_and_webp_are_accepted(attach_dialog, qapp, tmp_path, name, fmt, label):
    path = _image(tmp_path, name, fmt)
    _pick(attach_dialog, qapp, path)

    assert [row.type_label.text() for row in attach_dialog._rows] == [label]
    assert not attach_dialog.status_label.isVisible()


def test_removing_the_only_file_returns_to_the_plain_submission(
    attach_dialog, qapp, tmp_path, submissions, attach_calls,
):
    _pick(attach_dialog, qapp, _image(tmp_path, "a.png", "PNG"))
    attach_dialog._rows[0].remove_btn.click()
    _drain(qapp)
    assert attach_dialog._rows == [] and attach_dialog._attachments == []

    _ready(attach_dialog)
    attach_dialog._on_submit()
    attach_dialog.api.calls[0]["fn"]()

    assert submissions == [("report_a_problem", "The timer resets on resume.")]
    assert attach_calls == []


def test_the_remove_button_is_big_visible_focusable_and_named(attach_dialog, qapp, tmp_path):
    _pick(attach_dialog, qapp, _image(tmp_path, "shot.png", "PNG"))
    remove = attach_dialog._rows[0].remove_btn

    assert remove.text() == "Remove"
    assert remove.isVisible()
    assert remove.height() >= 28
    assert remove.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert remove.accessibleName() == "Remove shot.png"
    # A visible focus ring is part of the style, not left to the platform.
    assert "QPushButton#RemoveBtn:focus" in attach_dialog.styleSheet()
    assert "QPushButton#AttachBtn:focus" in attach_dialog.styleSheet()


def test_the_attach_button_works_from_the_keyboard(attach_dialog, qapp):
    asked = []
    attach_dialog._choose_files = lambda: asked.append(True) or []
    attach_dialog.attach_btn.setFocus()
    QTest.keyClick(attach_dialog.attach_btn, Qt.Key.Key_Space)
    _drain(qapp)

    assert attach_dialog.attach_btn.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert asked == [True]


def test_the_tab_order_is_category_message_attach_remove_cancel_submit(
    attach_dialog, qapp, tmp_path,
):
    _pick(attach_dialog, qapp,
          _image(tmp_path, "a.png", "PNG"), _image(tmp_path, "b.png", "PNG"))
    wanted = [
        attach_dialog.category_combo, attach_dialog.message_edit,
        attach_dialog.attach_btn,
        attach_dialog._rows[0].remove_btn, attach_dialog._rows[1].remove_btn,
        attach_dialog.cancel_btn, attach_dialog.submit_btn,
    ]
    seen, widget = [], attach_dialog.category_combo
    for _ in range(80):
        if widget in wanted and widget not in seen:
            seen.append(widget)
        widget = widget.nextInFocusChain()
        if widget is attach_dialog.category_combo:
            break

    assert seen == wanted


@pytest.mark.parametrize("name,data", [
    ("setup.exe", b"MZ\x90\x00" + b"\0" * 64),
    ("run.bat", b"@echo off\r\n"),
    ("run.cmd", b"@echo off\r\n"),
    ("installer.msi", b"\xd0\xcf\x11\xe0" + b"\0" * 64),
    ("lib.dll", b"MZ\x90\x00" + b"\0" * 64),
    ("script.ps1", b"Write-Host hi"),
    ("payload.js", b"alert(1)"),
    ("archive.zip", b"PK\x03\x04" + b"\0" * 64),
    ("fake.png", b"MZ\x90\x00" + b"\0" * 64),          # renamed, wrong magic
])
def test_a_file_that_is_not_an_allowed_image_is_refused_with_the_type_message(
    attach_dialog, qapp, tmp_path, name, data,
):
    path = tmp_path / name
    path.write_bytes(data)
    _pick(attach_dialog, qapp, str(path))

    assert attach_dialog._attachments == []
    assert attach_dialog.status_label.isVisible()
    assert attach_dialog.status_label.text() == TYPE_MESSAGE
    assert ERROR in attach_dialog.status_label.styleSheet()


def test_an_empty_file_and_a_directory_are_refused_as_unreadable(attach_dialog, qapp, tmp_path):
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    folder = tmp_path / "folder.png"
    folder.mkdir()

    for path in (empty, folder, tmp_path / "missing.png"):
        _pick(attach_dialog, qapp, str(path))
        assert attach_dialog._attachments == []
        assert attach_dialog.status_label.text() == UNREADABLE


def test_a_file_over_ten_megabytes_is_refused(attach_dialog, qapp, tmp_path):
    big = _padded_png(tmp_path, "big.png", MAX_TOTAL_ATTACHMENT_BYTES + 1)
    _pick(attach_dialog, qapp, big)

    assert attach_dialog._attachments == []
    assert attach_dialog.status_label.text() == TOO_LARGE


def test_the_ten_megabytes_are_a_total_across_the_files(attach_dialog, qapp, tmp_path):
    six = 6 * 1024 * 1024
    first = _padded_png(tmp_path, "one.png", six)
    second = _padded_png(tmp_path, "two.png", six)
    small = _image(tmp_path, "small.png", "PNG")

    _pick(attach_dialog, qapp, first)
    assert [row.info.name for row in attach_dialog._rows] == ["one.png"]
    assert not attach_dialog.status_label.isVisible()

    _pick(attach_dialog, qapp, second)       # 12 MB in all: refused
    assert [row.info.name for row in attach_dialog._rows] == ["one.png"]
    assert attach_dialog.status_label.text() == TOO_LARGE

    _pick(attach_dialog, qapp, small)        # what still fits is accepted
    assert [row.info.name for row in attach_dialog._rows] == ["one.png", "small.png"]
    assert not attach_dialog.status_label.isVisible()


def test_a_fourth_file_is_refused_and_attach_is_disabled_at_three(attach_dialog, qapp, tmp_path):
    files = [_image(tmp_path, f"{n}.png", "PNG") for n in range(4)]
    _pick(attach_dialog, qapp, *files[:3])

    assert len(attach_dialog._rows) == 3
    assert not attach_dialog.attach_btn.isEnabled()
    assert attach_dialog.attachment_note.isVisible()
    assert attach_dialog.attachment_note.text() == TOO_MANY

    # The disabled button is not the only guard: the handler refuses as well.
    attach_dialog._choose_files = lambda: [files[3]]
    attach_dialog._on_attach_clicked()
    assert len(attach_dialog._rows) == 3

    # Removing one frees the slot again.
    attach_dialog._rows[0].remove_btn.click()
    _drain(qapp)
    assert attach_dialog.attach_btn.isEnabled()
    assert not attach_dialog.attachment_note.isVisible()


def test_choosing_more_than_fits_adds_what_fits_and_says_so(attach_dialog, qapp, tmp_path):
    files = [_image(tmp_path, f"{n}.png", "PNG") for n in range(5)]
    _pick(attach_dialog, qapp, files[0])
    _pick(attach_dialog, qapp, *files[1:])

    assert [row.info.name for row in attach_dialog._rows] == ["0.png", "1.png", "2.png"]
    assert attach_dialog.status_label.text() == TOO_MANY


def test_the_same_file_cannot_be_attached_twice(attach_dialog, qapp, tmp_path):
    path = _image(tmp_path, "a.png", "PNG")
    _pick(attach_dialog, qapp, path)
    _pick(attach_dialog, qapp, path)

    assert len(attach_dialog._rows) == 1
    assert attach_dialog.status_label.text() == "That file is already attached."


def test_a_very_long_file_name_is_elided_and_never_widens_the_dialog(
    attach_dialog, qapp, tmp_path,
):
    long_name = "a_really_long_name_" + "x" * 90 + "_final_version.png"
    _pick(attach_dialog, qapp, _image(tmp_path, long_name, "PNG"))
    attach_dialog.resize(attach_dialog.minimumWidth(), attach_dialog.height())
    _drain(qapp)

    (row,) = attach_dialog._rows
    shown = row.name_label.text()
    assert shown != long_name and "…" in shown
    assert shown.startswith("a_really") and shown.endswith(".png")
    assert row.name_label.toolTip() == long_name
    assert attach_dialog.width() == attach_dialog.minimumWidth()
    # No horizontal overflow: the row, and its Remove button, stay inside the card.
    card_width = attach_dialog.card.width()
    assert row.geometry().right() <= card_width
    remove_right = row.remove_btn.mapTo(attach_dialog.card, row.remove_btn.rect().topRight()).x()
    assert remove_right <= card_width


def test_three_rows_fit_without_overflow_and_the_dialog_grows_for_them(
    attach_dialog, qapp, tmp_path,
):
    before = attach_dialog.height()
    files = [_image(tmp_path, f"shot-{n}.png", "PNG") for n in range(3)]
    _pick(attach_dialog, qapp, *files)

    assert attach_dialog.height() > before
    assert attach_dialog.height() >= attach_dialog.minimumHeight()
    card = attach_dialog.card
    for row in attach_dialog._rows:
        top_left = row.mapTo(card, row.rect().topLeft())
        bottom_right = row.mapTo(card, row.rect().bottomRight())
        assert card.rect().contains(top_left) and card.rect().contains(bottom_right)
        assert row.height() >= row.remove_btn.minimumHeight()
    # The message field keeps stretch 1: it is what gives way, not the rows.
    assert attach_dialog.message_edit.height() >= attach_dialog.message_edit.minimumHeight()
    # Nothing overlaps: each row ends above the next, the last above the buttons.
    rows = attach_dialog._rows
    for upper, lower in zip(rows, rows[1:]):
        assert upper.geometry().bottom() < lower.geometry().top()
    last_bottom = rows[-1].mapTo(card, rows[-1].rect().bottomLeft()).y()
    submit_top = attach_dialog.submit_btn.mapTo(card, attach_dialog.submit_btn.rect().topLeft()).y()
    assert last_bottom < submit_top


def test_a_submission_with_attachments_goes_through_the_one_background_path(
    attach_dialog, qapp, tmp_path, submissions, attach_calls,
):
    paths = [_image(tmp_path, "a.png", "PNG"), _image(tmp_path, "b.jpg", "JPG")]
    _pick(attach_dialog, qapp, *paths)
    _ready(attach_dialog)
    attach_dialog._on_submit()

    (call,) = attach_dialog.api.calls
    assert call["key"] == SUBMIT_KEY
    call["fn"]()
    assert submissions == []                       # not the JSON path
    ((category, message, sent_paths, client_op),) = attach_calls
    assert (category, message) == ("report_a_problem", "The timer resets on resume.")
    assert sent_paths == paths                     # in the order they were chosen
    assert len(client_op) == 32 and all(c in "0123456789abcdef" for c in client_op)


def test_a_double_click_with_attachments_sends_exactly_one_request(
    attach_dialog, qapp, tmp_path,
):
    _pick(attach_dialog, qapp, _image(tmp_path, "a.png", "PNG"))
    _ready(attach_dialog)
    attach_dialog._on_submit()
    attach_dialog._on_submit()
    attach_dialog._on_submit()

    assert len(attach_dialog.api.calls) == 1


def test_the_busy_state_locks_every_control_including_attach_and_remove(
    attach_dialog, qapp, tmp_path,
):
    _pick(attach_dialog, qapp, _image(tmp_path, "a.png", "PNG"), _image(tmp_path, "b.png", "PNG"))
    _ready(attach_dialog)
    attach_dialog._on_submit()

    assert attach_dialog.submit_btn.text() == "Submitting..."
    assert not attach_dialog.submit_btn.isEnabled()
    assert not attach_dialog.cancel_btn.isEnabled()
    assert not attach_dialog.category_combo.isEnabled()
    assert attach_dialog.message_edit.isReadOnly()
    assert not attach_dialog.attach_btn.isEnabled()
    assert all(not row.remove_btn.isEnabled() for row in attach_dialog._rows)

    # Nothing can be attached or removed while the request is in flight.
    attach_dialog._choose_files = lambda: [_image(tmp_path, "c.png", "PNG")]
    attach_dialog._on_attach_clicked()
    attach_dialog._remove_attachment(attach_dialog._attachments[0])
    assert len(attach_dialog._attachments) == 2


def test_cancel_after_choosing_files_sends_and_stores_nothing(
    attach_dialog, qapp, tmp_path, submissions, attach_calls,
):
    _pick(attach_dialog, qapp, _image(tmp_path, "a.png", "PNG"))
    _ready(attach_dialog)
    attach_dialog._on_cancel()
    _drain(qapp)

    assert submissions == [] and attach_calls == []
    assert attach_dialog.api.calls == []
    assert not attach_dialog.isVisible()


def test_a_failure_keeps_the_form_the_files_and_the_key_for_an_unchanged_retry(
    attach_dialog, qapp, tmp_path, attach_calls,
):
    paths = [_image(tmp_path, "a.png", "PNG")]
    _pick(attach_dialog, qapp, *paths)
    _ready(attach_dialog)

    attach_dialog._on_submit()
    attach_dialog.api.calls[0]["fn"]()
    attach_dialog.api._in_flight.clear()
    attach_dialog.api.calls[0]["on_error"](
        Exception("Unable to upload the attachment. Please try again.")
    )
    _drain(qapp)

    assert attach_dialog.isVisible()
    assert attach_dialog.message_edit.toPlainText() == "The timer resets on resume."
    assert [row.info.path for row in attach_dialog._rows] == paths
    assert attach_dialog.submit_btn.isEnabled() and attach_dialog.submit_btn.text() == "Submit"
    assert attach_dialog.attach_btn.isEnabled()
    assert attach_dialog.status_label.text() == "Unable to upload the attachment. Please try again."

    attach_dialog._on_submit()                        # an unchanged retry
    attach_dialog.api.calls[1]["fn"]()
    assert attach_calls[0][3] == attach_calls[1][3]   # the same idempotency key


def test_an_edited_retry_gets_a_new_key_so_it_is_never_swallowed_as_a_duplicate(
    attach_dialog, qapp, tmp_path, attach_calls,
):
    first = _image(tmp_path, "a.png", "PNG")
    second = _image(tmp_path, "b.png", "PNG")
    _pick(attach_dialog, qapp, first)
    _ready(attach_dialog)

    original = _fail_and_report_key(attach_dialog, qapp, attach_calls)
    assert _fail_and_report_key(attach_dialog, qapp, attach_calls) == original

    attach_dialog.message_edit.setPlainText("The timer resets on resume. Also on pause.")
    after_message_edit = _fail_and_report_key(attach_dialog, qapp, attach_calls)
    assert after_message_edit != original
    assert _fail_and_report_key(attach_dialog, qapp, attach_calls) == after_message_edit

    _choose(attach_dialog, "need_help")
    after_category = _fail_and_report_key(attach_dialog, qapp, attach_calls)
    assert after_category not in {original, after_message_edit}

    _pick(attach_dialog, qapp, second)
    after_attachment = _fail_and_report_key(attach_dialog, qapp, attach_calls)
    assert after_attachment not in {original, after_message_edit, after_category}
    assert attach_calls[-1][2] == [first, second]


def test_a_file_that_changed_on_disk_between_attempts_gets_a_new_key(
    attach_dialog, qapp, tmp_path, attach_calls,
):
    path = _image(tmp_path, "a.png", "PNG")
    _pick(attach_dialog, qapp, path)
    _ready(attach_dialog)

    original = _fail_and_report_key(attach_dialog, qapp, attach_calls)
    _image(tmp_path, "a.png", "PNG", 200, 200)        # the same path, new content
    assert _fail_and_report_key(attach_dialog, qapp, attach_calls) != original


def test_a_file_that_vanished_before_submit_is_reported_without_a_request(
    attach_dialog, qapp, tmp_path,
):
    path = _image(tmp_path, "a.png", "PNG")
    _pick(attach_dialog, qapp, path)
    _ready(attach_dialog)
    os.remove(path)
    attach_dialog._on_submit()

    assert attach_dialog.api.calls == []
    assert attach_dialog.status_label.text() == (
        "An attachment could no longer be read. Please remove it and try again."
    )
    assert attach_dialog.submit_btn.isEnabled()


def test_success_resets_the_form_and_the_attachments(attach_dialog, qapp, tmp_path):
    _pick(attach_dialog, qapp, _image(tmp_path, "a.png", "PNG"))
    _ready(attach_dialog)
    attach_dialog._on_submit()
    attach_dialog.api.calls[0]["on_success"]({"id": 2, "duplicate": False})
    _drain(qapp)

    assert attach_dialog.api.notifications[0][2] == "feedback-submitted"
    assert attach_dialog.message_edit.toPlainText() == ""
    assert attach_dialog.category_combo.currentData() is None
    assert attach_dialog._attachments == [] and attach_dialog._rows == []
    assert attach_dialog._client_op is None
    assert not attach_dialog.isVisible()


def test_a_duplicate_reply_is_just_a_success_to_the_dialog(attach_dialog, qapp, tmp_path):
    _pick(attach_dialog, qapp, _image(tmp_path, "a.png", "PNG"))
    _ready(attach_dialog)
    attach_dialog._on_submit()
    attach_dialog.api.calls[0]["on_success"]({"id": 2, "duplicate": True})
    _drain(qapp)

    assert "submitted successfully" in attach_dialog.api.notifications[0][0]
    assert not attach_dialog.isVisible()
