"""
text_safety — showing text that somebody else wrote, as the text it is.

Qt decides for itself whether a string is rich text: a `QLabel` left on `AutoText` and every tooltip
look at what they are given, and anything that starts like HTML is *rendered* -- a task named
`<b>x</b>` shows in bold, one named `<img src=//host/x>` makes the label fetch an image. Names
and messages in this application come from other people (a project or task a colleague created, a
member's name, a server error, a release title), so none of them may be handed to a rich-text path as
they are. The same applies to what the server says went wrong: a gateway's HTML error page must not
become a notification.

Two helpers, and nothing else, so there is one way to do it:

* `plain_tooltip(text)` -- the string to pass to `setToolTip` so the tooltip shows `text` literally.
* `set_plain(label, text)` -- `setTextFormat(PlainText)` then `setText`, in that order, for a label
  whose whole content is somebody else's text.

(`html.escape` is the right tool where a label genuinely needs rich text around a message -- the task
list's "Retry" link -- and is used there directly.)
"""
from __future__ import annotations

import html
from typing import Optional

from core.validation import validate_search_term

from PySide6.QtCore import Qt
from PySide6.QtGui import Qt as _GuiQt   # `mightBeRichText` lives in the QtGui namespace


def plain_tooltip(text: Optional[str]) -> str:
    """`text` as a tooltip that cannot be interpreted as HTML.

    Qt treats a tooltip as rich text when `Qt.mightBeRichText` says it looks like it -- the very
    test it applies -- so text it would not misread is returned untouched (every ordinary name, and
    anything that compares a tooltip with a name still holds). Only text that *would* be read as markup is
    escaped and wrapped in a `<span>`, which is rich text that reads exactly as the original. An empty
    value stays empty, so a caller that clears a tooltip still can.
    """
    if not text:
        return ""
    text = str(text)
    if not _GuiQt.mightBeRichText(text):
        return text
    return f"<span style='white-space: pre-wrap'>{html.escape(text, quote=False)}</span>"


def set_plain(label, text: Optional[str]) -> None:
    """Show `text` literally on a `QLabel`: the format is fixed before the text is set, so there is no
    moment at which it could be read as rich text."""
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText("" if text is None else str(text))


def clean_search_text(text: Optional[str]) -> str:
    """What a local search box filters on: the shared SEARCH rule's value, or "" if it refuses the text
    (markup, control characters, too long) -- the list is then simply not narrowed. Used by the filters that only
    compare a term with names already on screen (task list, project list, pickers); a term that goes to the server is
    validated by the caller that sends it."""
    result = validate_search_term(text or "")
    return (result.value or "") if result.ok else ""
