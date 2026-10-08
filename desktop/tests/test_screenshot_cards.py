"""The Screenshots tab renders real captures, in IST, with real activity.

Three defects were reported from a running build, and all three were visible
on screen as confident, plausible, wrong information:

* the thumbnails were gradients drawn by a `SimulatedScreenshotWidget`, not
  the user's screen;
* the capture times were the backend's UTC values printed verbatim, so a
  7:34 PM capture was labelled 2:04 PM;
* every card read "0% Activity", because the endpoint it fetched carries no
  activity at all.
"""
from __future__ import annotations

import io

import pytest

from ui.activity_section import (
    ScreenshotCard, ScreenshotThumbnail, ScreenshotsTabView,
    _flatten_timeline, _ist_clock,
)

pytest.importorskip("PIL", reason="Pillow builds the test images")


def _png(color=(20, 120, 200)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (200, 200), color).save(buffer, format="PNG")
    return buffer.getvalue()


TIMELINE = {
    "success": True,
    "window_minutes": 10,
    "windows": [
        {
            # 14:00 UTC == 19:30 IST
            "window_start": "2026-09-07T14:00:00+00:00",
            "window_end": "2026-09-07T14:10:00+00:00",
            "activity_percentage": 62,
            "activity_measured_seconds": 120,
            "screenshots": [
                {"id": 1, "captured_at": "2026-09-07T14:04:00+00:00",
                 "monitor_number": 1, "width": 1000, "height": 1000,
                 "file_size_bytes": 26114,
                 "view_url": "/time-entry-screenshots/1/view",
                 "task_id": 7, "task_name": "Reviewing Client Updates",
                 "project_id": 5, "project_name": "Neurodivergent Insights"},
            ],
            "screenshot_count": 1,
        },
        {
            "window_start": "2026-09-07T14:10:00+00:00",
            "window_end": "2026-09-07T14:20:00+00:00",
            "activity_percentage": 0,
            "activity_measured_seconds": 0,
            "screenshots": [
                # No task/project: the entry (or its task/project) was
                # deleted after capture -- covered below.
                {"id": 2, "captured_at": "2026-09-07T14:17:00+00:00",
                 "monitor_number": 1, "width": 1000, "height": 1000,
                 "file_size_bytes": 25070,
                 "view_url": "/time-entry-screenshots/2/view",
                 "task_id": None, "task_name": None,
                 "project_id": None, "project_name": None},
            ],
            "screenshot_count": 1,
        },
    ],
}


class TestIstClock:
    def test_a_utc_capture_is_displayed_in_ist(self, qapp):
        # The reported bug exactly: 14:04 UTC is 19:34 IST, and was rendering
        # as 2:04 PM — a real screenshot wearing a time that never happened.
        assert _ist_clock("2026-09-07T14:04:00+00:00") == "7:34 PM"

    def test_the_trailing_z_form_the_backend_emits_is_understood(self, qapp):
        assert _ist_clock("2026-09-07T14:04:00Z") == "7:34 PM"

    def test_a_naive_timestamp_is_read_as_utc_not_as_local_time(self, qapp):
        # Reading a naive backend timestamp as local time is how displayed
        # times end up 5.5 hours out.
        assert _ist_clock("2026-09-07T14:04:00") == "7:34 PM"

    def test_a_missing_or_unparseable_value_renders_nothing_invented(self, qapp):
        assert _ist_clock(None) == ""
        assert _ist_clock("") == ""
        assert _ist_clock("not-a-time") == "not-a-time"[:16]


class TestFlattenTimeline:
    def test_each_screenshot_carries_its_own_windows_activity(self, qapp):
        cards = _flatten_timeline(TIMELINE)
        by_id = {c["id"]: c for c in cards}
        assert by_id[1]["activity_percent"] == 62
        assert by_id[2]["activity_percent"] == 0
        assert by_id[1]["activity_measured_seconds"] == 120

    def test_the_window_label_is_an_ist_range(self, qapp):
        cards = _flatten_timeline(TIMELINE)
        assert cards[-1]["window_label"] == "7:30 PM - 7:40 PM"

    def test_the_screen_count_comes_from_the_window_not_from_an_assumption(self, qapp):
        # What a future SCREENSHOTS_PER_WINDOW of 3 produces. Nothing here may
        # assume a window holds exactly one.
        payload = {
            "windows": [{
                "window_start": "2026-09-07T14:00:00+00:00",
                "window_end": "2026-09-07T14:10:00+00:00",
                "activity_percentage": 50, "activity_measured_seconds": 60,
                "screenshots": [
                    {"id": n, "captured_at": f"2026-09-07T14:0{n}:00+00:00",
                     "view_url": f"/time-entry-screenshots/{n}/view"}
                    for n in (1, 2, 3)
                ],
                "screenshot_count": 3,
            }]
        }
        cards = _flatten_timeline(payload)
        assert len(cards) == 3
        assert all(c["window_screenshot_count"] == 3 for c in cards)

    def test_newest_captures_come_first(self, qapp):
        cards = _flatten_timeline(TIMELINE)
        assert [c["id"] for c in cards] == [2, 1]

    def test_an_unexpected_payload_yields_no_cards_rather_than_raising(self, qapp):
        assert _flatten_timeline(None) == []
        assert _flatten_timeline([]) == []
        assert _flatten_timeline({"windows": None}) == []

    def test_the_task_and_project_pass_through_unchanged(self, qapp):
        # _flatten_timeline spreads the raw screenshot dict; the backend's
        # task_name/project_name must survive that untouched -- never
        # recomputed or renamed on this side.
        cards = _flatten_timeline(TIMELINE)
        by_id = {c["id"]: c for c in cards}
        assert by_id[1]["task_name"] == "Reviewing Client Updates"
        assert by_id[1]["project_name"] == "Neurodivergent Insights"

    def test_a_screenshot_with_no_task_or_project_carries_none_not_a_missing_key(self, qapp):
        # A screenshot whose entry (or its task/project) was deleted after
        # capture: the field must still exist, as None, so the card can tell
        # "recorded as nothing" apart from "the backend never sent this".
        cards = _flatten_timeline(TIMELINE)
        by_id = {c["id"]: c for c in cards}
        assert by_id[2]["task_name"] is None
        assert by_id[2]["project_name"] is None


class TestThumbnail:
    def test_it_shows_nothing_until_a_real_image_arrives(self, qapp):
        # The predecessor painted a convincing fake workspace here, which is
        # how fabricated pictures came to be mistaken for the user's screen.
        thumb = ScreenshotThumbnail()
        assert thumb.state == "loading"

    def test_a_real_image_is_rendered(self, qapp):
        thumb = ScreenshotThumbnail()
        thumb.set_image(_png())
        assert thumb.state == "ready"

    def test_undecodable_bytes_report_unavailable_rather_than_a_placeholder(self, qapp):
        thumb = ScreenshotThumbnail()
        thumb.set_image(b"not an image")
        assert thumb.state == "unavailable"


def _detailed_webp(edge: int = 1000) -> bytes:
    """A 1000px image with real detail, encoded as the capture pipeline does."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (edge, edge), (24, 24, 27))
    draw = ImageDraw.Draw(image)
    for i in range(0, edge, 25):
        draw.rectangle([i, (i * 7) % edge, i + 60, ((i * 7) % edge) + 40],
                       fill=((i * 3) % 255, (i * 5) % 255, (i * 11) % 255))
        draw.line([0, i, edge, edge - i], fill=(200, 200, 210), width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=72)
    return buffer.getvalue()


def _reference_render(data: bytes, width: int, height: int):
    """What the thumbnail drew before it kept a bounded master: the whole
    decoded image, scaled to fill and centred on every paint."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap

    pixmap = QPixmap()
    assert pixmap.loadFromData(data)
    out = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(QColor("#0F172A"))
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, width, height), 8, 8)
    painter.setClipPath(path)
    painter.fillRect(0, 0, width, height, QColor("#0F172A"))
    scaled = pixmap.scaled(
        out.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    painter.drawPixmap((width - scaled.width()) // 2, (height - scaled.height()) // 2, scaled)
    painter.end()
    return out


def _mean_abs_difference(a, b) -> float:
    assert a.size() == b.size()
    total = 0
    for y in range(a.height()):
        for x in range(a.width()):
            pa, pb = a.pixelColor(x, y), b.pixelColor(x, y)
            total += abs(pa.red() - pb.red()) + abs(pa.green() - pb.green()) + abs(pa.blue() - pb.blue())
    return total / (a.width() * a.height() * 3)


class TestThumbnailMemory:
    """A card drew a 120px strip from a 4 MB decoded pixmap it re-scaled on
    every paint. Twelve to twenty-four of them were most of the memory the
    dashboard held, and a rebuilt card decoded its image all over again."""

    WIDTH, HEIGHT = 300, 120

    def _render(self, thumb):
        thumb.resize(self.WIDTH, self.HEIGHT)
        return thumb.grab().toImage()

    def test_at_full_size_it_draws_exactly_what_it_drew_before(self, qapp):
        data = _detailed_webp()
        thumb = ScreenshotThumbnail(self.HEIGHT)          # no master cap
        thumb.set_image(data)
        drawn = self._render(thumb)
        expected = _reference_render(data, self.WIDTH, self.HEIGHT)
        assert _mean_abs_difference(drawn.convertToFormat(expected.format()), expected) < 0.5

    def test_the_card_keeps_a_bounded_master_and_draws_nearly_the_same(self, qapp):
        data = _detailed_webp()
        thumb = ScreenshotThumbnail(self.HEIGHT, master_edge=640)
        thumb.set_image(data)
        drawn = self._render(thumb)
        assert max(thumb._image.width(), thumb._image.height()) == 640
        expected = _reference_render(data, self.WIDTH, self.HEIGHT)
        # Two resamples instead of one: visible only to a pixel diff.
        assert _mean_abs_difference(drawn.convertToFormat(expected.format()), expected) < 4.0

    def test_what_a_card_holds_is_a_fraction_of_the_decoded_picture(self, qapp):
        thumb = ScreenshotThumbnail(self.HEIGHT, master_edge=640)
        thumb.set_image(_detailed_webp())
        self._render(thumb)
        held = thumb._image.sizeInBytes() + thumb._drawn.width() * thumb._drawn.height() * 4
        assert held < 2 * 1024 * 1024        # was 4 MB for the pixmap alone

    def test_a_repaint_at_the_same_size_does_not_rescale(self, qapp):
        thumb = ScreenshotThumbnail(self.HEIGHT, master_edge=640)
        thumb.set_image(_detailed_webp())
        self._render(thumb)
        first = thumb._drawn
        thumb.grab()
        thumb.grab()
        assert thumb._drawn is first

    def test_a_new_size_rebuilds_the_drawn_pixmap_and_nothing_else(self, qapp):
        thumb = ScreenshotThumbnail(self.HEIGHT, master_edge=640)
        thumb.set_image(_detailed_webp())
        self._render(thumb)
        master = thumb._image
        thumb.resize(420, self.HEIGHT)
        image = thumb.grab().toImage()
        assert image.width() == 420
        assert thumb._image is master
        assert thumb._drawn.width() == 420

    def test_unavailable_releases_the_picture(self, qapp):
        thumb = ScreenshotThumbnail(self.HEIGHT, master_edge=640)
        thumb.set_image(_detailed_webp())
        self._render(thumb)
        thumb.set_unavailable()
        assert thumb._image is None and thumb._drawn is None

    def test_cards_use_the_bounded_master_and_the_lightbox_does_not(self, qapp):
        from ui.activity_section import SCREENSHOT_CARD_MASTER_EDGE, ScreenshotPreviewDialog

        card = ScreenshotCard(dict(_flatten_timeline(TIMELINE)[-1]))
        assert card.thumbnail._master_edge == SCREENSHOT_CARD_MASTER_EDGE
        dialog = ScreenshotPreviewDialog(dict(_flatten_timeline(TIMELINE)[-1]))
        assert dialog.large_preview._master_edge is None


class TestCardsAreNotKeptAliveByGarbage:
    """Every refresh re-rendered the grid, and every retired card stayed alive
    -- with its 4 MB decoded picture -- until the cyclic garbage collector ran,
    because the card stored a bound method of itself on its own child
    (`thumbnail.mousePressEvent = self._on_thumbnail_clicked`). On an idle
    application the collector runs rarely, so memory climbed by a page of
    twelve cards a minute and then fell off a cliff: 424 MB of an idle
    installed process was such pictures. Measured on the real dashboard:
    12 cards -> 987 MB after 20 refreshes with the collector off; flat now."""

    def _view(self, qapp):
        from ui.activity_section import MODE_DATA, ScreenshotsTabView

        view = ScreenshotsTabView()
        view.resize(1100, 700)
        view.show()
        data = _detailed_webp()
        view.image_requested.connect(lambda shot: view.deliver_image(shot["id"], data))
        return view, MODE_DATA

    @staticmethod
    def _shots(revision, count=12):
        return [
            {"id": i, "captured_at": "2026-10-08T05:00:00+00:00", "view_url": f"/x/{i}",
             "window_label": "w", "activity_percent": (i + revision) % 100,
             "activity_measured_seconds": 60, "window_screenshot_count": 1,
             "task_name": "t", "project_name": "p"}
            for i in range(count)
        ]

    @staticmethod
    def _flush(qapp):
        from PySide6.QtCore import QCoreApplication, QEvent

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        qapp.processEvents()

    @staticmethod
    def _live(cls):
        import gc

        return sum(1 for o in gc.get_objects() if type(o) is cls)

    def test_a_retired_card_is_freed_without_the_garbage_collector(self, qapp):
        import gc
        import weakref

        view, mode = self._view(qapp)
        view.set_data(self._shots(0))
        view.set_mode(mode)
        card = view._placed[0]
        ref = weakref.ref(card)
        del card
        gc.collect()
        gc.disable()
        try:
            view.set_data(self._shots(1))      # every card's data changed: all rebuilt
            view.set_mode(mode)
            self._flush(qapp)
            assert ref() is None, "a retired card is still referenced -- a reference cycle"
        finally:
            gc.enable()

    def test_repeated_refreshes_do_not_accumulate_cards(self, qapp):
        import gc

        view, mode = self._view(qapp)
        view.set_data(self._shots(0))
        view.set_mode(mode)
        gc.collect()
        gc.disable()
        try:
            on_screen = self._live(ScreenshotThumbnail)
            for revision in range(1, 11):
                view.set_data(self._shots(revision))
                view.set_mode(mode)
                self._flush(qapp)
            assert self._live(ScreenshotThumbnail) == on_screen
            # And nothing is holding a decoded picture but the cards on screen.
            held = [c.thumbnail._image for c in view._placed]
            assert all(image is not None for image in held)
        finally:
            gc.enable()

    def test_dropping_the_grid_releases_every_picture(self, qapp):
        view, mode = self._view(qapp)
        view.set_data(self._shots(0))
        view.set_mode(mode)
        cards = list(view._placed)
        assert cards and all(c.thumbnail._image is not None for c in cards)
        view.set_data([])
        view.set_mode("empty")
        assert all(c.thumbnail._image is None for c in cards)

    def test_pressing_the_thumbnail_still_reports_the_screenshot(self, qapp):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication

        data = dict(_flatten_timeline(TIMELINE)[-1])
        card = ScreenshotCard(data)
        seen = []
        card.clicked.connect(seen.append)
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(card.thumbnail, press)
        assert seen == [data]


class TestCard:
    def _card(self, **overrides):
        data = dict(_flatten_timeline(TIMELINE)[-1])  # the 62% window
        data.update(overrides)
        return ScreenshotCard(data)

    def test_a_card_asks_for_its_image_and_shows_it(self, qapp):
        card = self._card()
        assert card.thumbnail.state == "loading"
        card.set_image(_png())
        assert card.thumbnail.state == "ready"

    def test_a_measured_window_shows_its_percentage(self, qapp):
        from PySide6.QtWidgets import QLabel

        card = self._card()
        texts = [w.text() for w in card.findChildren(QLabel)]
        assert "62% Activity" in texts
        assert "7:34 PM" in texts

    def test_an_unmeasured_window_says_so_instead_of_claiming_zero(self, qapp):
        # "0%" and "nothing was measured" are different facts. Every card in an
        # untracked hour read a confident 0% before.
        from PySide6.QtWidgets import QLabel

        card = self._card(activity_percent=0, activity_measured_seconds=0)
        texts = [w.text() for w in card.findChildren(QLabel)]
        assert "No activity data" in texts
        assert "0% Activity" not in texts

    def test_the_card_names_what_it_was_captured_under(self, qapp):
        card = self._card()  # the 62% window: has task/project, see TIMELINE
        assert card.project_lbl.toolTip() == "Neurodivergent Insights"
        assert card.task_lbl.toolTip() == "Reviewing Client Updates"

    def test_a_screenshot_with_no_recorded_task_or_project_says_so_plainly(self, qapp):
        # Never blank, and never a guessed or placeholder name -- the entry
        # was deleted, and the card says exactly that.
        card = self._card(task_id=None, task_name=None, project_id=None, project_name=None)
        assert card.project_lbl.toolTip() == "No project recorded"
        assert card.task_lbl.toolTip() == "No task recorded"


class TestTabView:
    def test_a_card_with_no_cached_image_requests_one(self, qapp):
        view = ScreenshotsTabView()
        requested = []
        view.image_requested.connect(lambda shot: requested.append(shot["id"]))
        view.set_data(_flatten_timeline(TIMELINE))
        assert sorted(requested) == [1, 2]

    def test_a_delivered_image_reaches_its_card_and_is_cached(self, qapp):
        view = ScreenshotsTabView()
        view.set_data(_flatten_timeline(TIMELINE))
        view.deliver_image(1, _png())
        assert view._cards[1].thumbnail.state == "ready"

        # A re-render repaints from memory rather than re-downloading.
        requested = []
        view.image_requested.connect(lambda shot: requested.append(shot["id"]))
        view.render_view()
        assert requested == [2]
        assert view._cards[1].thumbnail.state == "ready"

    def test_a_preview_that_gave_up_is_asked_for_again_on_refresh(self, qapp):
        # A thumbnail is one round trip that can lose a race with a cold
        # backend or a laptop's first second of wifi. Leaving the card
        # permanently "unavailable" made screenshots that were present the
        # whole time look missing.
        view = ScreenshotsTabView()
        view.set_data(_flatten_timeline(TIMELINE))
        view.deliver_image(1, _png())      # one succeeded
        view.deliver_image(2, None)        # one gave up
        assert view._cards[2].thumbnail.state == "unavailable"

        requested = []
        view.image_requested.connect(lambda shot: requested.append(shot["id"]))
        view.retry_unavailable()

        assert requested == [2], "only the failed one is re-requested"
        assert view._cards[2].thumbnail.state == "loading"
        assert view._cards[1].thumbnail.state == "ready", "a loaded card is untouched"

    def test_a_failed_download_leaves_the_card_in_place(self, qapp):
        # The row is real even when its image could not be fetched; dropping
        # the card would understate how much was captured.
        view = ScreenshotsTabView()
        view.set_data(_flatten_timeline(TIMELINE))
        view.deliver_image(1, None)
        assert view._cards[1].thumbnail.state == "unavailable"
        assert len(view._cards) == 2


def _timeline(count: int) -> dict:
    """`count` windows, one screenshot each."""
    windows = []
    for i in range(count):
        hour, minute = 10 + i // 6, (i % 6) * 10
        windows.append({
            "window_start": f"2026-09-08T{hour:02d}:{minute:02d}:00+00:00",
            "window_end": f"2026-09-08T{hour:02d}:{minute + 9:02d}:59+00:00",
            "activity_percentage": 40 + i,
            "activity_measured_seconds": 600,
            "screenshot_count": 1,
            "screenshots": [{
                "id": i + 1,
                "captured_at": f"2026-09-08T{hour:02d}:{minute + 3:02d}:00+00:00",
                "view_url": f"/time-entry-screenshots/{i + 1}/view",
            }],
        })
    return {"windows": windows}


class TestGridLayout:
    """The grid must look the same whatever the day held.

    A grid row stretches to whatever height it is given, so before the card
    height was fixed the identical card rendered compactly when there were
    three rows of results and as a tall box with a large empty area under the
    thumbnail when there was one. Same data, two layouts, decided only by how
    much had been captured.
    """

    def _view(self, count, width=1560, height=700):
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        host = QWidget()
        host.resize(width, height)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        view = ScreenshotsTabView(host)
        layout.addWidget(view)
        view.set_data(_flatten_timeline(_timeline(count)))
        host.show()
        return host, view

    def test_a_card_is_the_same_height_however_many_there_are(self, qapp):
        seen = set()
        for count in (1, 2, 4, 7, 8):
            host, view = self._view(count)
            qapp.processEvents()
            seen.update(card.height() for card in view._cards.values())
            host.close()
        assert len(seen) == 1, f"cards came out at different heights: {sorted(seen)}"

    def test_a_single_screenshot_does_not_stretch_to_fill_the_panel(self, qapp):
        # The reported symptom, pinned: one result in a tall panel.
        host, view = self._view(1, height=900)
        qapp.processEvents()
        card = view._cards[1]
        assert card.height() == card.sizeHint().height()
        assert card.height() < 300, "the card must not absorb the spare height"
        host.close()

    def _parts(self, card):
        """The card's stacked parts, top to bottom, as laid out."""
        from PySide6.QtWidgets import QLabel

        info_row = card.layout().itemAt(2).widget()
        window_lbl, count_lbl = info_row.findChildren(QLabel)[:2]
        return card.thumbnail, info_row, window_lbl, count_lbl

    def test_the_time_range_sits_below_the_thumbnail_not_inside_it(self, qapp):
        # The card is exactly as tall as its parts. It used to be a hardcoded
        # 210px, which fitted the offscreen platform's fonts with room to
        # spare and was 6px short with the Windows font engine: the layout
        # squeezed the labels to their minimums, the window range was drawn
        # into the thumbnail underneath its capture-time badge, and the
        # screen count sat on the card's bottom border.
        host, view = self._view(1)
        qapp.processEvents()
        card = view._cards[1]
        thumbnail, info_row, window_lbl, count_lbl = self._parts(card)

        assert card.height() >= card.layout().sizeHint().height()
        assert info_row.geometry().top() > thumbnail.geometry().bottom()
        for lbl in (window_lbl, count_lbl):
            assert lbl.height() >= lbl.sizeHint().height(), lbl.text()
        info_bottom = info_row.mapTo(card, info_row.rect().bottomLeft()).y()
        assert info_bottom < card.height() - card.layout().contentsMargins().bottom()
        host.close()

    def test_the_card_grows_with_its_content(self, qapp, monkeypatch):
        # The failure the real display produced, reproduced offscreen: make the
        # content taller than 210px and the card must follow it, never squeeze
        # it. (The offscreen fonts are shorter than the Windows ones, so the
        # thumbnail stands in for the 6px the real font metrics added.)
        import ui.activity_section as module

        monkeypatch.setattr(module, "SCREENSHOT_THUMB_HEIGHT", 140)
        host, view = self._view(1)
        qapp.processEvents()
        card = view._cards[1]
        thumbnail, info_row, window_lbl, count_lbl = self._parts(card)

        assert thumbnail.height() == 140
        assert card.height() >= card.layout().sizeHint().height()
        assert info_row.geometry().top() > thumbnail.geometry().bottom()
        assert count_lbl.height() >= count_lbl.sizeHint().height()
        host.close()

    def test_cards_are_laid_out_four_to_a_row(self, qapp):
        from ui.activity_section import SCREENSHOT_COLUMNS

        host, view = self._view(8)
        qapp.processEvents()
        tops = sorted({card.y() for card in view._cards.values()})
        assert len(tops) == 8 // SCREENSHOT_COLUMNS
        host.close()

    def test_every_card_in_a_row_is_the_same_width(self, qapp):
        host, view = self._view(8)
        qapp.processEvents()
        widths = {card.width() for card in view._cards.values()}
        assert len(widths) == 1
        host.close()


def _many() -> int:
    from ui.activity_section import SCREENSHOT_PAGE_SIZE

    return SCREENSHOT_PAGE_SIZE * 2 + 4


MANY = _many()


class TestPaging:
    """Overflow gets a Load more button, as the Apps and URLs tabs do."""

    def _view(self, count):
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        host = QWidget()
        host.resize(1560, 700)
        layout = QVBoxLayout(host)
        view = ScreenshotsTabView(host)
        layout.addWidget(view)
        view.set_data(_flatten_timeline(_timeline(count)))
        host.show()
        return host, view

    @staticmethod
    def _load_more(view):
        from PySide6.QtWidgets import QPushButton

        for button in view.findChildren(QPushButton):
            if button.objectName() == "LoadMoreBtn":
                return button
        return None

    def test_one_page_is_shown_when_there_are_more_than_fit(self, qapp):
        from ui.activity_section import SCREENSHOT_PAGE_SIZE

        host, view = self._view(MANY)
        assert len(view._cards) == SCREENSHOT_PAGE_SIZE
        host.close()

    def test_the_button_names_how_many_remain(self, qapp):
        from ui.activity_section import SCREENSHOT_PAGE_SIZE

        host, view = self._view(MANY)
        button = self._load_more(view)
        assert button is not None
        assert str(MANY - SCREENSHOT_PAGE_SIZE) in button.text()
        host.close()

    def test_no_button_when_everything_already_fits(self, qapp):
        host, view = self._view(5)
        assert self._load_more(view) is None
        host.close()

    def test_clicking_it_reveals_another_page(self, qapp):
        from ui.activity_section import SCREENSHOT_PAGE_SIZE

        host, view = self._view(MANY)
        self._load_more(view).click()
        assert len(view._cards) == SCREENSHOT_PAGE_SIZE * 2
        host.close()

    def test_a_refresh_keeps_what_the_user_expanded(self, qapp):
        # A refresh every few seconds that collapsed the grid back to one page
        # would undo the user's "Load more" faster than they could read it.
        from ui.activity_section import SCREENSHOT_PAGE_SIZE

        host, view = self._view(MANY)
        self._load_more(view).click()
        view.set_data(_flatten_timeline(_timeline(MANY)))
        assert len(view._cards) == SCREENSHOT_PAGE_SIZE * 2
        host.close()


class TestCardBorder:
    def test_the_border_is_bold_enough_to_separate_the_cards(self, qapp):
        # At 1px the cards read as one continuous field rather than as
        # separate screenshots.
        card = ScreenshotCard(_flatten_timeline(_timeline(1))[0])
        assert "2px solid" in card.styleSheet()
