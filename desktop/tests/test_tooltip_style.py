"""
Every tooltip in the dashboard is drawn in the one tooltip style.

A tooltip is styled through the widget it belongs to: Qt resolves it against
that widget's own sheet and its ancestors' before the application's, and a
nearer sheet wins. It is also a QWidget (and a QLabel, and a QFrame), so any
rule or selector-less sheet on the way up repaints it.

That is how "Next page" under the task pager came to be white text on a
near-white box (reported 2026-09-30): the dashboard's bare
`QWidget { background: CONTENT_BG }` reached the tooltip, and the text colour
still came from the application sheet. The same mechanism, through a
selector-less `background: transparent`, left the account name, the task
row's project marker and the Play disc with white text on the platform's own
white tooltip.

These tests show each tooltip for real and read the pixels back, rather than
asserting on stylesheet text: what matters is what Qt resolved, and the rule
that breaks it next time will be one nobody thought to grep for.
"""
from collections import Counter

import pytest
from PySide6.QtCore import QEvent, QPoint
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QToolTip, QWidget

from ui.styles import APP_QSS, SIDEBAR_BG, TEXT_PRIMARY

#: The backgrounds a tooltip may resolve to: the tooltip style's own, and the
#: sidebar's navy for the project rows, whose container paints everything
#: inside it that colour. Both are dark and both are explicit. Anything else
#: -- above all "transparent", which the offscreen platform grabs as black
#: and Windows draws as its own white tooltip -- is the defect.
ALLOWED_BACKGROUNDS = {QColor(TEXT_PRIMARY).name(), QColor(SIDEBAR_BG).name()}


def _drain(qapp) -> None:
    for _ in range(3):
        qapp.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _shown_tip(qapp, widget):
    """Show `widget`'s tooltip and return the label Qt built for it."""
    # The trailing space makes the text differ from the previous tooltip's,
    # so Qt builds a label for this widget rather than reusing the last one.
    QToolTip.showText(QPoint(40, 40), widget.toolTip() + " ", widget)
    _drain(qapp)
    return next(
        (w for w in qapp.topLevelWidgets()
         if w.metaObject().className() == "QTipLabel" and w.isVisible()),
        None,
    )


def _colours(tip):
    """(background, text) of a shown tooltip, as `#rrggbb`.

    The background is the colour most of its pixels are, read from a grab of
    the real widget; the text colour is the one Qt resolved into its palette.
    """
    image = tip.grab().toImage()
    counts = Counter(
        image.pixelColor(x, y).name()
        for x in range(0, image.width(), 2)
        for y in range(0, image.height(), 2)
    )
    return counts.most_common(1)[0][0], tip.palette().color(tip.foregroundRole()).name()


@pytest.fixture
def dashboard(qapp, runtime):
    from ui.activity_section import ActivitySection
    from ui.dashboard_window import DashboardWindow

    previous_sheet = qapp.styleSheet()
    qapp.setStyleSheet(APP_QSS)          # what main.py installs
    window = DashboardWindow(
        runtime=runtime, session_manager=runtime.session_manager,
        project_service=runtime.project_service, task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service, api_client=runtime.api_client,
    )
    window.resize(1400, 800)
    projects = [{"id": i, "project_name": f"Project {i:03d}"} for i in range(1, 46)]
    window._projects = projects
    window._sidebar.set_user({"name": "Disposable Tester", "email": "tester@e2e.invalid"})
    window._sidebar.set_projects(projects)
    window._task_section.set_tasks(
        [
            {"id": i, "name": f"Task {i}", "status": "todo",
             "created_at": f"2026-09-30T05:{i:02d}:00Z"}
            for i in range(1, 16)
        ],
        projects[2], "#F97316",
    )
    activity = window.findChild(ActivitySection)
    activity.view_apps.set_data([{"name": "Microsoft Edge", "percentage": 30, "time_str": "24m"}])
    activity.view_apps.set_mode("data")
    activity.view_urls.set_data([{
        "title": "GitHub", "domain": "github.com", "url": "https://github.com/pulls",
        "percentage": 50, "time_str": "2h",
    }])
    activity.view_urls.set_mode("data")
    window.show()
    _drain(qapp)
    try:
        yield window
    finally:
        QToolTip.hideText()
        window.hide()
        window.reset_state()
        window.deleteLater()
        qapp.setStyleSheet(previous_sheet)
        _drain(qapp)


def test_the_task_pager_tooltips_are_readable(dashboard, qapp):
    """The reported one: hovering the previous / next page arrows."""
    section = dashboard._task_section

    for button, text in (
        (section._prev_page_btn, "Previous page"),
        (section._next_page_btn, "Next page"),
    ):
        assert button.toolTip() == text
        tip = _shown_tip(qapp, button)
        assert tip is not None, f"no tooltip was shown for {text!r}"
        background, foreground = _colours(tip)
        assert background == QColor(TEXT_PRIMARY).name(), (
            f"{text!r} is drawn on {background}, not the tooltip background"
        )
        assert foreground == "#ffffff"


def test_every_tooltip_in_the_dashboard_is_light_text_on_a_dark_background(dashboard, qapp):
    """The whole window, so the next bare `QWidget` or selector-less sheet
    that catches a tooltip fails here instead of on somebody's screen."""
    owners = [w for w in dashboard.findChildren(QWidget) if w.toolTip()]
    # The fixture must actually reach the places that were broken: the pager,
    # a task row, the top bar, the account card, the Play disc, a usage row.
    assert len(owners) >= 30, f"only {len(owners)} tooltips found; the fixture is too thin"

    wrong = []
    for owner in owners:
        tip = _shown_tip(qapp, owner)
        if tip is None:
            wrong.append((type(owner).__name__, owner.toolTip(), "no tooltip shown"))
            continue
        background, foreground = _colours(tip)
        if background not in ALLOWED_BACKGROUNDS or foreground != "#ffffff":
            wrong.append((
                type(owner).__name__, owner.objectName(), owner.toolTip()[:40],
                f"{foreground} on {background}",
            ))

    assert not wrong, "tooltips not drawn in the tooltip style:\n" + "\n".join(map(str, wrong))
