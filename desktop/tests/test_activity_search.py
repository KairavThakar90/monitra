"""
Coverage for the Activity section's Apps/URLs search filter.

The filter works on lists already held in memory, so it issues no request and
builds no query. That is what makes the matching rules simple and worth
pinning: a search is a plain case-insensitive substring over exactly the
strings the row puts on screen, and `%` or `_` are ordinary characters rather
than wildcards.

The search term itself still goes through the shared validation catalogue
rather than a check written for this one box, so the desktop agrees with the
backend and the web client about what a search box may contain.
"""
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QPushButton

from core.validation import SEARCH_MAX_LENGTH
from ui.activity_section import (
    PAGE_SIZE, AppRowWidget, AppsTabView, URLRowWidget, URLsTabView,
)


def _flush_deletions() -> None:
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _names_on_screen(view, row_type) -> list:
    """The rows actually rendered, read back from the widgets themselves.

    Asserting on `visible_items()` alone would pass even if `render_view` never
    used it, which is the mistake that would make the box look broken while the
    tests stayed green.
    """
    _flush_deletions()
    rows = view.findChildren(row_type)
    key = "name" if row_type is AppRowWidget else "title"
    return [r.item_data.get(key) for r in rows]


APPS = [
    {"name": "Google Chrome", "subtitle": "Web browser", "percentage": 50, "time_str": "2h"},
    {"name": "Visual Studio Code", "subtitle": "Editor", "percentage": 30, "time_str": "1h"},
    {"name": "Slack", "subtitle": "Team chat", "percentage": 20, "time_str": "40m"},
]

URLS = [
    {"title": "GitHub", "domain": "github.com",
     "url": "https://github.com/pulls", "percentage": 50, "time_str": "2h"},
    {"title": "Google Docs", "domain": "docs.google.com",
     "url": "https://docs.google.com/document", "percentage": 30, "time_str": "1h"},
    {"title": "Stack Overflow", "domain": "stackoverflow.com",
     "url": "https://stackoverflow.com/questions", "percentage": 20, "time_str": "40m"},
]


def _apps_view() -> AppsTabView:
    view = AppsTabView()
    view.set_data(list(APPS))
    view.set_mode("data")
    return view


def _urls_view() -> URLsTabView:
    view = URLsTabView()
    view.set_data(list(URLS))
    view.set_mode("data")
    return view


# ── Apps ──────────────────────────────────────────────────────────────────────

def test_an_app_is_found_by_its_name(qapp):
    view = _apps_view()
    view.set_filter("chrome")

    assert _names_on_screen(view, AppRowWidget) == ["Google Chrome"]


def test_an_app_is_found_by_its_subtitle(qapp):
    """The subtitle is on screen, so it must be searchable."""
    view = _apps_view()
    view.set_filter("editor")

    assert _names_on_screen(view, AppRowWidget) == ["Visual Studio Code"]


def test_matching_ignores_case(qapp):
    view = _apps_view()
    view.set_filter("SLACK")

    assert _names_on_screen(view, AppRowWidget) == ["Slack"]


def test_an_empty_term_shows_everything_again(qapp):
    view = _apps_view()
    view.set_filter("chrome")
    view.set_filter("")

    assert len(_names_on_screen(view, AppRowWidget)) == len(APPS)


def test_a_term_matching_nothing_renders_no_rows(qapp):
    view = _apps_view()
    view.set_filter("nothing here")

    assert _names_on_screen(view, AppRowWidget) == []


# ── URLs ──────────────────────────────────────────────────────────────────────

def test_a_site_is_found_by_its_title(qapp):
    view = _urls_view()
    view.set_filter("stack overflow")

    assert _names_on_screen(view, URLRowWidget) == ["Stack Overflow"]


def test_a_site_is_found_by_its_domain(qapp):
    view = _urls_view()
    view.set_filter("github.com") 

    assert _names_on_screen(view, URLRowWidget) == ["GitHub"]

  
def test_a_site_is_found_by_a_fragment_of_its_url(qapp):
    """The URL is shown under the title, and is often the only thing the user
    remembers -- searching the visible path must work."""
    view = _urls_view()
    view.set_filter("/questions")

    assert _names_on_screen(view, URLRowWidget) == ["Stack Overflow"]


def test_one_term_can_match_several_sites(qapp):
    view = _urls_view()
    view.set_filter("google")

    assert _names_on_screen(view, URLRowWidget) == ["Google Docs"]


# ── Matching rules ────────────────────────────────────────────────────────────

def test_percent_is_a_literal_character_not_a_wildcard(qapp):
    """The list is filtered in memory, so there is no LIKE and no wildcard.

    A user typing `100%` is looking for the text `100%`; if it behaved as a
    wildcard it would match every row instead, which is the failure
    docs/VALIDATION.md calls out for the server-side case.
    """
    view = AppsTabView()
    view.set_data([
        {"name": "Report 100% complete", "percentage": 1, "time_str": "1s"},
        {"name": "Slack", "percentage": 1, "time_str": "1s"},
    ])
    view.set_mode("data")

    view.set_filter("100%")
    assert _names_on_screen(view, AppRowWidget) == ["Report 100% complete"]

    view.set_filter("%")
    assert _names_on_screen(view, AppRowWidget) == ["Report 100% complete"]


def test_underscore_is_a_literal_character_too(qapp):
    view = AppsTabView()
    view.set_data([
        {"name": "my_app", "percentage": 1, "time_str": "1s"},
        {"name": "myXapp", "percentage": 1, "time_str": "1s"},
    ])
    view.set_mode("data")
    view.set_filter("my_app")

    assert _names_on_screen(view, AppRowWidget) == ["my_app"]


# ── Paging and refresh ────────────────────────────────────────────────────────

def _load_more(view):
    _flush_deletions()
    return next(
        (b for b in view.findChildren(QPushButton) if b.objectName() == "LoadMoreBtn"),
        None,
    )


def test_a_new_term_starts_the_reveal_count_again(qapp):
    """Carrying "show 12" into a filtered list of two offers rows that do not
    exist."""
    view = AppsTabView()
    view.set_data([
        {"name": f"app{i}", "percentage": 1, "time_str": "1s"}
        for i in range(PAGE_SIZE * 3)
    ])
    view.set_mode("data")
    _load_more(view).click()

    view.set_filter("app")

    assert view._visible_count == PAGE_SIZE


def test_load_more_counts_only_matching_rows(qapp):
    view = AppsTabView()
    view.set_data(
        [{"name": f"keep{i}", "percentage": 1, "time_str": "1s"} for i in range(PAGE_SIZE + 2)]
        + [{"name": f"drop{i}", "percentage": 1, "time_str": "1s"} for i in range(20)]
    )
    view.set_mode("data")
    view.set_filter("keep")

    button = _load_more(view)
    assert button is not None
    assert "2" in button.text(), "the button must offer the matching remainder"


def test_a_refresh_keeps_the_filter_applied(qapp):
    """Activity auto-refreshes every minute; that must not clear the search."""
    view = _apps_view()
    view.set_filter("slack")

    view.set_data(list(APPS))          # what the refresh does

    assert _names_on_screen(view, AppRowWidget) == ["Slack"]


# ── The search box itself ─────────────────────────────────────────────────────

def _section(qapp):
    from unittest.mock import MagicMock

    from ui.activity_section import ActivitySection

    section = ActivitySection(MagicMock(), MagicMock())
    section.view_apps.set_data(list(APPS))
    section.view_apps.set_mode("data")
    section.view_urls.set_data(list(URLS))
    section.view_urls.set_mode("data")
    return section


def test_the_search_box_filters_both_usage_lists(qapp):
    section = _section(qapp)
    section.search_input.setText("google")
    section._apply_search()

    assert [a["name"] for a in section.view_apps.visible_items()] == ["Google Chrome"]
    assert [u["title"] for u in section.view_urls.visible_items()] == ["Google Docs"]


def test_the_search_box_stays_on_screen_on_every_tab(qapp):
    """Hiding it on Screenshots made the feature impossible to find.

    The Activity section opens on Screenshots, so a box that only exists on
    the other two tabs is a box nobody discovers.
    """
    section = _section(qapp)
    section.show()

    for tab in ("screenshots", "apps", "urls"):
        section.switch_tab(tab)
        assert section._search_bar.isVisible(), f"missing on the {tab} tab"
    section.hide()


def test_the_box_is_inert_and_says_why_on_the_screenshots_tab(qapp):
    section = _section(qapp)

    section.switch_tab("screenshots")
    assert not section.search_input.isEnabled()
    assert section.search_input.placeholderText() == section.SEARCH_UNAVAILABLE

    section.switch_tab("apps")
    assert section.search_input.isEnabled()
    assert section.search_input.placeholderText() == section.SEARCH_PLACEHOLDER


def test_the_box_accepts_exactly_the_catalogue_limit(qapp):
    """A client stricter than the backend makes a valid search impossible."""
    section = _section(qapp)

    assert section.search_input.maxLength() == SEARCH_MAX_LENGTH


def test_a_rejected_term_reports_itself_and_changes_nothing(qapp):
    """Invalid input is refused, never scrubbed and applied anyway."""
    section = _section(qapp)
    section.search_input.setText("chrome")
    section._apply_search()
    assert len(section.view_apps.visible_items()) == 1

    # Markup is refused by the shared rule; setText bypasses maxLength, which
    # is how a paste or a programmatic set would arrive.
    section.search_input.setText("<script>alert(1)</script>")
    section._apply_search()

    assert section._search_error.isHidden() is False
    assert [a["name"] for a in section.view_apps.visible_items()] == ["Google Chrome"], (
        "a refused term must leave the previous results alone"
    )


def test_a_valid_term_clears_a_previous_error(qapp):
    section = _section(qapp)
    section.search_input.setText("<b>x</b>")
    section._apply_search()
    assert not section._search_error.isHidden()

    section.search_input.setText("slack")
    section._apply_search()

    assert section._search_error.isHidden()
    assert [a["name"] for a in section.view_apps.visible_items()] == ["Slack"]
