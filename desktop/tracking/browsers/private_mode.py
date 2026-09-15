"""
private_mode — Is this browser window a private/incognito one?

Why this is not a word search
-----------------------------
The obvious implementation is "look for 'Incognito' somewhere". It does not
work, and both halves of why were confirmed against real browser windows on a
development machine rather than assumed:

* **Chrome's window title does not contain the word at all.** A Chrome window
  opened with ``--incognito`` reports the plain title
  ``'Wikipedia - Google Chrome'``. Anything keyed on the window title would
  classify every Chrome incognito window as normal — silently, and for the one
  browser the feature is most often asked about.

* **Perfectly ordinary windows are full of the word.** A normal Chrome window
  showing a page *about* incognito mode has "Incognito" throughout its
  accessibility tree, and a page whose ``<title>`` ends in "(Incognito)"
  produces the window title ``'Quarterly report (Incognito) - Google Chrome'``.
  Anything that searched the window's text, or matched a parenthetical
  anywhere in it, would report a private session that never happened — and
  would do so on the machine of whoever is reading the documentation for this
  feature.

What is actually read
---------------------
For Chromium browsers, exactly one element: the window's ``BrowserRootView``,
whose accessible name is the title Chromium composes for screen readers. The
browser derives that name from its own off-the-record state and appends a
**trailing parenthetical** to it — and to nothing else:

    normal      BrowserRootView.Name = 'Example Domain - Google Chrome'
    incognito   BrowserRootView.Name = 'Wikipedia - Google Chrome (Incognito)'
    InPrivate   BrowserRootView.Name = 'Search - Bing - Microsoft Edge (InPrivate)'

So the signal is positional and structural, not lexical: a parenthetical in
*final* position on *that one element*. The page-title trap above fails this
test twice over — the parenthetical is mid-string, and the browser name
follows it.

`_TRAILING_PARENTHETICAL` is matched against the accessible name only, never
against page content, and the window title is used as a control: if the plain
title already ends with the same parenthetical, it came from the page (or from
a title-less app window) and not from the browser, so it is rejected. That is
what keeps a progressive web app called "Notes (Draft)" from being reported as
private browsing.

Firefox
-------
Gecko exposes no ``BrowserRootView`` — it is not Chromium — but it does put its
own marker in the window title, which Chromium does not:

    normal    'Example Domain — Mozilla Firefox'
    private   'DuckDuckGo … — Mozilla Firefox Private Browsing'

The marker is matched as a whole suffix (``Mozilla Firefox Private Browsing``),
not as the bare words "Private Browsing", so a page *titled* "Private Browsing"
in a normal window is not mistaken for one.

Failing safe
------------
Three states, not two. `PrivateState.UNKNOWN` is returned whenever the question
could not be answered — no UI Automation on this platform, a browser with no
readable marker, a failed read — and it is stored as SQL NULL rather than as
false. Recording "not private" for a window nobody could inspect would be
asserting a finding that was never made, which is the same defect as the
``unknown-domain`` URL this package already removed once.
"""
from __future__ import annotations

import re
import sys
import time
from typing import Dict, Optional, Tuple

from core.logging_setup import get_logger
from tracking.browsers.uia import read_element_name_by_class

log = get_logger("tracking.browsers.private")

#: The Chromium view whose accessible name carries the marker. A C++ class
#: name, identical in every locale and every supported Chromium browser —
#: Chrome, Edge, Brave, Vivaldi and Opera all build the same view.
CHROMIUM_ROOT_VIEW_CLASS = "BrowserRootView"

#: A parenthetical in final position, which is the only position the browser
#: ever writes its own marker in. Bounded in length so a page title that
#: happens to end in a long bracketed phrase cannot match, and restricted to a
#: single level of nesting.
_TRAILING_PARENTHETICAL = re.compile(r"\s\(([^()]{1,40})\)\s*$")

#: Markers seen in the wild, lowercased. Used **only** to label a detection in
#: logs and to recognise a marker as familiar — never as the test itself. The
#: structural rule above is what decides; this list exists so that an operator
#: reading a log sees `marker=incognito` rather than a bare parenthetical, and
#: so an unfamiliar marker (a locale this list does not cover) is logged once
#: as such rather than passing unnoticed.
KNOWN_CHROMIUM_MARKERS = frozenset({
    "incognito",          # Chrome, Brave (en)
    "inprivate",          # Edge (en)
    "in private",
    "private",            # Vivaldi, Opera (en)
    "private browsing",
    "guest",              # Chromium guest profile: also off-the-record
})

#: Firefox writes its private marker into the window title. Matched as a whole
#: suffix so the words alone, appearing in a page title, cannot trigger it.
FIREFOX_PRIVATE_TITLE_SUFFIXES = (
    "mozilla firefox private browsing",
    "firefox private browsing",
    "(private browsing)",
    "— private browsing",
    "- private browsing",
)


class PrivateState:
    """Whether a browser window is a private/incognito one.

    Deliberately three-valued. `UNKNOWN` is a real answer — "this platform or
    this browser did not tell us" — and is stored as NULL, distinct from a
    `NORMAL` finding that something actually looked and found nothing.
    """

    PRIVATE = "private"
    NORMAL = "normal"
    UNKNOWN = "unknown"

    @staticmethod
    def to_bool(state: str) -> Optional[bool]:
        if state == PrivateState.PRIVATE:
            return True
        if state == PrivateState.NORMAL:
            return False
        return None


def _chromium_marker(accessible_name: str, window_title: str) -> Optional[str]:
    """
    The browser's own trailing marker, or None.

    Rejects a parenthetical that the plain window title already ends with:
    that one came from the page or from a title-less application window, not
    from the browser's off-the-record state.
    """
    match = _TRAILING_PARENTHETICAL.search(accessible_name or "")
    if not match:
        return None
    marker = match.group(1).strip()
    if not marker:
        return None
    if window_title and _TRAILING_PARENTHETICAL.search(window_title):
        title_match = _TRAILING_PARENTHETICAL.search(window_title)
        if title_match and title_match.group(1).strip().casefold() == marker.casefold():
            return None
    return marker


class PrivateWindowDetector:
    """
    Reads a browser window's private/incognito state.

    One instance is shared by the URL tracker. It caches per window handle
    because private state is a property of a *window*, fixed for that window's
    whole life: a window cannot be toggled into incognito, only opened as one.
    That makes the cache far safer than the address-bar cache next door, which
    has to expire because the page behind an unchanged title genuinely can
    change. The TTL is here only so a recycled handle cannot inherit a verdict
    from a window that has closed.
    """

    #: How long a verdict is reused for one window handle.
    CACHE_TTL_SECONDS = 30.0
    #: Bound on the cache, so a session that opens and closes many windows
    #: cannot grow it without limit.
    MAX_CACHE_ENTRIES = 256

    def __init__(self) -> None:
        self._cache: Dict[int, Tuple[float, str, Optional[str]]] = {}
        #: Markers already reported, so an unfamiliar locale costs one log
        #: line rather than one every two seconds for the whole session.
        self._reported_markers: set = set()
        self._reported_unsupported = False

    def detect(
        self, hwnd: int, window_title: str, is_firefox: bool = False
    ) -> Tuple[str, Optional[str]]:
        """
        Classify one browser window.

        :param is_firefox: Gecko exposes no Chromium view tree, so its marker
            is read from the window title instead. The caller knows which
            browser it is looking at; sniffing it again here would be a second
            source of truth for something already resolved.
        :return: `(PrivateState, marker)`. The marker is the browser's own
            word for the state, for logging, and is None when there is none.
        """
        if is_firefox:
            return self._detect_firefox(window_title)
        return self._detect_chromium(hwnd, window_title)

    def _detect_firefox(self, window_title: str) -> Tuple[str, Optional[str]]:
        title = (window_title or "").strip().casefold()
        if not title:
            return PrivateState.UNKNOWN, None
        for suffix in FIREFOX_PRIVATE_TITLE_SUFFIXES:
            if title.endswith(suffix):
                return PrivateState.PRIVATE, "private browsing"
        # Firefox always appends its own name to the title, so a title that
        # arrived at all is one Firefox composed, and the absence of the
        # private suffix is a real finding rather than a failed read.
        return PrivateState.NORMAL, None

    def _detect_chromium(self, hwnd: int, window_title: str) -> Tuple[str, Optional[str]]:
        if sys.platform != "win32":
            # No UI Automation: the question cannot be answered here. Saying
            # "not private" would be inventing the answer.
            if not self._reported_unsupported:
                self._reported_unsupported = True
                log.info(
                    "private-window detection is unavailable on %s; browser "
                    "activity is still tracked, with the private state recorded "
                    "as unknown rather than guessed",
                    sys.platform,
                )
            return PrivateState.UNKNOWN, None
        if not hwnd:
            return PrivateState.UNKNOWN, None

        now = time.monotonic()
        cached = self._cache.get(hwnd)
        if cached and now - cached[0] < self.CACHE_TTL_SECONDS:
            return cached[1], cached[2]

        accessible_name = read_element_name_by_class(hwnd, CHROMIUM_ROOT_VIEW_CLASS)
        if accessible_name is None:
            # No BrowserRootView: not a Chromium window, or UI Automation is
            # unavailable on this machine. Either way nothing was observed.
            state, marker = PrivateState.UNKNOWN, None
        else:
            marker = _chromium_marker(accessible_name, window_title or "")
            if marker is None:
                state = PrivateState.NORMAL
            else:
                state = PrivateState.PRIVATE
                self._note_marker(marker)

        self._remember(hwnd, now, state, marker)
        return state, marker

    def _note_marker(self, marker: str) -> None:
        key = marker.casefold()
        if key in self._reported_markers:
            return
        self._reported_markers.add(key)
        if key in KNOWN_CHROMIUM_MARKERS:
            log.info("PRIVATE_BROWSER_DETECTED marker=%s", key)
        else:
            # Structurally a private window — the browser appended a marker of
            # its own — but not one this build has a name for, most likely a
            # locale the catalogue does not list. Worth a line so it can be
            # added, not worth doubting the detection over.
            log.info(
                "PRIVATE_BROWSER_DETECTED marker=%s (not in the known-marker "
                "catalogue; treated as private on the structural signal)", key,
            )

    def _remember(
        self, hwnd: int, now: float, state: str, marker: Optional[str]
    ) -> None:
        if len(self._cache) >= self.MAX_CACHE_ENTRIES:
            # Drop whatever is oldest. A verdict is cheap to recompute and the
            # cache exists to avoid a UIA round trip per two-second sample,
            # not to be authoritative.
            oldest = min(self._cache, key=lambda key: self._cache[key][0])
            self._cache.pop(oldest, None)
        self._cache[hwnd] = (now, state, marker)

    def forget(self, hwnd: int) -> None:
        """Drop a window's cached verdict. Used by tests and on session reset."""
        self._cache.pop(hwnd, None)

    def reset(self) -> None:
        self._cache.clear()
        self._reported_markers.clear()
