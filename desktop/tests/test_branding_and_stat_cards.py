"""
Coverage for the redesign's two new shared pieces:

* `core.branding` -- one definition of the Monitra mark, preferring a real
  logo file dropped into desktop/assets/ and falling back to the vendored
  vector mark. The tray/window icon and the sidebar both render from it, so
  they cannot drift apart.
* `ui.stat_cards` -- the three summary cards (project status, project hours,
  active task). Every value is handed in; the row derives no duration and
  reads no service. Unknown values are said plainly rather than shown as a
  measured-looking zero.
"""
import os

import pytest
from PySide6.QtWidgets import QApplication

from core import branding
from ui.stat_cards import StatCardsRow


# ── branding ─────────────────────────────────────────────────────────────────

def test_mark_renders_at_any_requested_size(qapp):
    for size in (16, 32, 44, 256):
        pixmap = branding.logo_pixmap(size)
        assert not pixmap.isNull()
        assert max(pixmap.width(), pixmap.height()) == size


def test_pixmaps_are_cached_per_size(qapp):
    assert branding.logo_pixmap(48) is branding.logo_pixmap(48)


def test_a_bundled_logo_file_wins_over_the_vector_mark(qapp, tmp_path, monkeypatch):
    """Dropping artwork into the assets folder is the whole swap procedure --
    no other code change, and it must actually take precedence."""
    from PySide6.QtGui import QPixmap

    logo = tmp_path / "monitra_logo.png"
    canvas = QPixmap(64, 64)
    canvas.fill()
    assert canvas.save(str(logo))

    monkeypatch.setattr(branding, "ASSETS_DIR", str(tmp_path))
    monkeypatch.setattr(branding, "_logo_path_cache", None)
    monkeypatch.setattr(branding, "_logo_path_resolved", False)
    monkeypatch.setattr(branding, "_pixmap_cache", {})

    assert branding.logo_file_path() == str(logo)
    assert not branding.logo_pixmap(32).isNull()


def test_no_logo_file_still_produces_the_mark(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(branding, "ASSETS_DIR", str(tmp_path / "empty"))
    monkeypatch.setattr(branding, "_logo_path_cache", None)
    monkeypatch.setattr(branding, "_logo_path_resolved", False)
    monkeypatch.setattr(branding, "_pixmap_cache", {})

    assert branding.logo_file_path() is None
    assert not branding.logo_pixmap(32).isNull()


def test_the_app_icon_is_built_from_the_same_mark(qapp):
    """The tray/window icon and the sidebar must be one artwork, not two."""
    from background_services.notifications.notification_service import create_app_icon

    icon = create_app_icon(sizes=[64])
    from_icon = icon.pixmap(64, 64).toImage()
    assert from_icon == branding.logo_pixmap(64).toImage()


def test_the_badge_tile_is_square_and_carries_the_mark(qapp):
    """The badge every floating notification card draws its logo from: a
    tile at the requested size, with the mark itself visibly present (not
    just a blank tinted square)."""
    tile = branding.logo_badge_pixmap(40)
    assert not tile.isNull()
    assert tile.width() == 40 and tile.height() == 40

    # A second call proves caching (see below), not that the tile has
    # content. Compare against a plain filled square of the tile's own
    # corner colour instead -- the mark's own colours cannot match a single
    # flat fill everywhere.
    from PySide6.QtGui import QPixmap

    flat = QPixmap(40, 40)
    flat.fill(tile.toImage().pixelColor(0, 0))
    assert tile.toImage() != flat.toImage()


def test_badge_tiles_are_cached_per_size(qapp):
    assert branding.logo_badge_pixmap(36) is branding.logo_badge_pixmap(36)


def test_the_badge_tile_scales_with_the_requested_size(qapp):
    small = branding.logo_badge_pixmap(24)
    large = branding.logo_badge_pixmap(48)
    assert small.width() == 24
    assert large.width() == 48


# ── stat cards ───────────────────────────────────────────────────────────────

def test_reset_states_are_honest_not_zeroed(qapp):
    row = StatCardsRow()
    assert row.status_card._value.full_text() == "—"
    assert row.status_card._sub.full_text() == "No project selected"
    assert row.active_card._value.full_text() == "No active task"
    assert row.total_card._value.full_text() == "00:00:00"
    assert row.total_card._sub.full_text() == "Not tracking"


def test_total_card_formats_seconds_and_flags_tracking(qapp):
    row = StatCardsRow()
    row.set_total_seconds(3_725, True)
    assert row.total_card._value.full_text() == "01:02:05"
    assert row.total_card._sub.full_text() == "Tracking now"

    row.set_total_seconds(3_725, False)
    assert row.total_card._sub.full_text() == "Not tracking"


def test_status_card_shows_the_projects_own_status(qapp):
    row = StatCardsRow()
    row.set_project_status("Active", "#3B82F6")
    assert row.status_card._value.full_text() == "Active"
    assert row.status_card._sub.full_text() == "Set by admin"


def test_status_card_without_a_project_says_so(qapp):
    row = StatCardsRow()
    row.set_project_status("Active", "#3B82F6")
    row.set_project_status(None, None)
    assert row.status_card._value.full_text() == "—"
    assert row.status_card._sub.full_text() == "No project selected"


def _laid_out(row, width):
    """The row at a real width, with its layout applied -- what decides
    elision is the width a card actually gets, so a test that does not lay the
    row out is not testing elision at all."""
    row.resize(width, row.sizeHint().height())
    row.show()
    QApplication.processEvents()
    return row


def test_active_task_card_elides_long_names_but_keeps_the_full_one(qapp):
    """Elision follows the card's actual width now, not a fixed character
    count. A name that does not fit ends in an ellipsis and is offered in
    full on hover; nothing is lost."""
    row = StatCardsRow()
    name = "A very long task name that will not possibly fit inside one card"
    row.set_active_task(name, "Project X")
    _laid_out(row, 900)

    value = row.active_card._value
    assert value.full_text() == name, "the real name is always retained"
    assert value.text().endswith("…")
    assert value.toolTip() == name
    assert row.active_card._sub.full_text() == "Project X"
    row.hide()


def test_a_name_that_fits_is_not_abbreviated(qapp):
    """Elision is decided by the card's width, so a name the card can show is
    shown whole and offers no tooltip, because nothing is hidden."""
    row = StatCardsRow()
    row.set_active_task("Write specs", "Project X")
    # Wide enough for the name beside the card's Break In / Break Out button
    # under the offscreen platform's fallback font, whose box glyphs are about
    # twice the width of Segoe UI's (264px for these eleven characters).
    _laid_out(row, 1900)

    value = row.active_card._value
    assert value.text() == "Write specs"
    assert value.toolTip() == "", "nothing is hidden, so nothing to offer on hover"
    row.hide()


def test_a_wider_card_shows_more_of_the_name(qapp):
    """The property the old fixed 24-character cut could not have: what is
    shown depends on the room there is to show it."""
    name = "Review the Q3 client update and circulate it"
    row = StatCardsRow()
    row.set_active_task(name, "Project X")

    _laid_out(row, 900)
    narrow = row.active_card._value.text()
    _laid_out(row, 1900)
    wide = row.active_card._value.text()
    row.hide()

    assert len(wide) > len(narrow)
    assert name.startswith(narrow.rstrip("…"))
    assert name.startswith(wide.rstrip("…"))


def test_the_running_duration_is_never_abbreviated(qapp):
    """The cards may shorten a sub-line; the number they exist to show is not
    negotiable. At the narrowest width the row permits, the clock still reads
    in full."""
    row = StatCardsRow()
    row.set_total_seconds(3_725, True)
    _laid_out(row, row.minimumWidth())

    assert row.total_card._value.text() == "01:02:05"
    row.hide()


# ── responsive arrangement ───────────────────────────────────────────────────
#
# Three cards side by side need much less width than the four-card row this
# replaced. A 1366x768 laptop -- the commonest screen this application runs
# on -- has about 1066px left for content once the sidebar is accounted for,
# comfortably above the three-card single-row floor; a window narrower than
# that still wraps to two-then-one rather than squeezing a card's text
# mid-word.

COMMON_LAPTOP_CONTENT_WIDTH = 1066


def test_the_row_fits_a_common_laptop(qapp):
    row = StatCardsRow()
    assert row.minimumWidth() <= COMMON_LAPTOP_CONTENT_WIDTH


def test_a_wide_window_gets_one_row_of_three(qapp):
    row = StatCardsRow()
    _laid_out(row, StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH)
    assert row.columns() == 3
    row.hide()


def test_a_narrow_window_wraps_to_two_then_one(qapp):
    row = StatCardsRow()
    _laid_out(row, StatCardsRow.TWO_COLUMN_MINIMUM_WIDTH)
    assert row.columns() == 2
    row.hide()


def test_wrapping_keeps_every_value_readable(qapp):
    """The point of wrapping rather than shrinking: at the narrow width, every
    card still shows its number whole."""
    row = StatCardsRow()
    row.set_total_seconds(3_725, True)
    row.set_project_status("Active", "#3B82F6")
    _laid_out(row, StatCardsRow.TWO_COLUMN_MINIMUM_WIDTH)

    assert row.total_card._value.text() == "01:02:05"
    assert row.status_card._value.text() == "Active"
    row.hide()


def test_the_arrangement_only_changes_on_a_real_transition(qapp):
    """Re-parenting widgets on every resize event of an unchanged layout is
    the level-triggered shape this project has paid for before."""
    row = StatCardsRow()
    _laid_out(row, StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH + 200)
    assert row.columns() == 3

    row.resize(StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH + 100, row.height())
    QApplication.processEvents()
    assert row.columns() == 3, "still three across; nothing should have moved"

    row.resize(StatCardsRow.TWO_COLUMN_MINIMUM_WIDTH, row.height())
    QApplication.processEvents()
    assert row.columns() == 2
    row.hide()
