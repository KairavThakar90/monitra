"""
content_split — the vertical divider between the task list and the Activity
panel, sized from the content instead of from a fixed proportion.

A plain `QSplitter` hands its height out in proportion to the sizes it was
last given (`setSizes`, or the user's last drag). That is the right rule for
a divider the user has placed, and the wrong one for the divider nobody has
touched yet: the dashboard opened with a 40/60 split, so a maximised window
gave the task list 40% of the content height whatever it held -- three rows
of a ten-row page, with a scrollbar -- and gave the Activity panel 60%, most
of it empty. A window dragged to the same size did exactly the same; it only
looked reasonable in smaller windows, where both panes were pinned at their
minimums and there was no surplus to misplace.

`ContentSplitter` keeps the drag handle and replaces the *default* rule:

* Until the user moves the handle, the top pane is given the height its
  content asks for (`top_need`) and the bottom pane takes the rest, except
  that the bottom pane is never squeezed below the height at which it stops
  being usable (`bottom_floor`) while the window has room for both. When the
  window does not have room, the top pane scrolls -- that is what its own
  scroll area is for.
* Once the user has moved the handle, the top pane keeps the height they
  gave it across every later resize, and the bottom pane takes the rest.
  Their choice is a height, not a proportion: maximising the window then
  adds the new room to the Activity panel instead of stretching both.

The sizes are recomputed inside `resizeEvent`, so a native maximise, a
restore, and an edge drag all go through the one path; the result depends
only on the height the splitter has now, never on how it got there.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter, QWidget


def split_heights(
    available: int,
    *,
    top_need: int,
    top_min: int,
    bottom_min: int,
    bottom_floor: int,
    top_user: Optional[int] = None,
) -> Tuple[int, int]:
    """Divide `available` pixels between the two panes.

    :param available: The height the two panes share, the handle excluded.
    :param top_need: The height at which the top pane's content shows
        without scrolling.
    :param top_min: The top pane's minimum height.
    :param bottom_min: The bottom pane's minimum height.
    :param bottom_floor: The height below which the bottom pane stops being
        usable. Honoured while there is room; a preference, not a minimum.
    :param top_user: The height the user gave the top pane by dragging the
        handle, or None if they have not.

    Returns `(top, bottom)`. The pair always sums to `available` when the
    minimums fit; when they do not, the top pane gets its minimum and the
    bottom pane whatever is left, and the splitter's own minimum handling
    takes it from there.
    """
    if top_user is not None:
        wanted, reserve = top_user, bottom_min
    else:
        wanted, reserve = top_need, max(bottom_min, bottom_floor)

    top = min(wanted, available - reserve)
    top = max(top, top_min)
    # Never push the bottom pane under its minimum while there is room to
    # avoid it -- a floor may be asked for by content, a minimum may not be
    # crossed by anything.
    top = min(top, max(top_min, available - bottom_min))
    return top, available - top


class ContentSplitter(QSplitter):
    """A two-pane vertical splitter whose default split follows the top
    pane's content (see the module docstring)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        # A section collapsed to 0 height would look like it vanished --
        # each side keeps a usable minimum instead.
        self.setChildrenCollapsible(False)
        self._top_need: Optional[Callable[[], int]] = None
        self._bottom_floor: Optional[Callable[[], int]] = None
        #: The top pane's height as the user last dragged it; None until they
        #: do. From then on the content no longer decides the split.
        self._user_top_height: Optional[int] = None
        # `splitterMoved` is emitted for the user's drags only; `setSizes`
        # does not emit it, so the sizes this class applies are never
        # mistaken for a choice the user made.
        self.splitterMoved.connect(self._on_user_moved)

    def set_content_sizing(
        self, top_need: Callable[[], int], bottom_floor: Callable[[], int]
    ) -> None:
        """Install the two measurements the split is computed from. Both are
        callables, read at every resize, so they always describe the content
        as it is now."""
        self._top_need = top_need
        self._bottom_floor = bottom_floor
        self.relayout()

    def user_adjusted(self) -> bool:
        """Whether the user has placed the handle themselves."""
        return self._user_top_height is not None

    def relayout(self) -> None:
        """Re-divide the current height. Call when the top pane's content
        changed size; a resize calls it by itself."""
        self._apply(self.contentsRect().height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's own casing
        # The geometry is already the new one here, so the split is computed
        # from the height the window actually has -- whether it arrived in
        # one step (maximise, restore) or in a hundred (an edge drag).
        self._apply(self.contentsRect().height())
        super().resizeEvent(event)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _on_user_moved(self, _pos: int, _index: int) -> None:
        self._user_top_height = self.sizes()[0]

    @staticmethod
    def _minimum_height(widget: QWidget) -> int:
        # The same floor Qt's own layout enforces: the larger of the explicit
        # minimum and the widget's minimum size hint.
        return max(widget.minimumHeight(), widget.minimumSizeHint().height())

    def handle_height(self) -> int:
        """The height the one handle between the two panes really takes.

        Not `handleWidth()`: a style sheet that gives the handle a border
        makes it taller than the width that was set (10px set, 12px drawn
        in the dashboard), and a split computed against the smaller figure
        came out one pixel short on each pane once Qt scaled it to fit.
        """
        if self.count() < 2:
            return self.handleWidth()
        return self.handle(1).sizeHint().height()

    def target_sizes(self, available_with_handle: int) -> Optional[List[int]]:
        """The sizes this splitter would apply at the given total height, or
        None while it has fewer than two panes or no measurements yet."""
        if self.count() < 2 or self._top_need is None or self._bottom_floor is None:
            return None
        available = available_with_handle - self.handle_height()
        if available <= 0:
            return None
        top, bottom = split_heights(
            available,
            top_need=self._top_need(),
            top_min=self._minimum_height(self.widget(0)),
            bottom_min=self._minimum_height(self.widget(1)),
            bottom_floor=self._bottom_floor(),
            top_user=self._user_top_height,
        )
        return [top, bottom]

    def _apply(self, available_with_handle: int) -> None:
        sizes = self.target_sizes(available_with_handle)
        if sizes is None or sizes == self.sizes():
            return
        self.setSizes(sizes)
