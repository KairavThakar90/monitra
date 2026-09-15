import os
import re
import time
from abc import ABC
from typing import Optional, Tuple

from tracking.browsers.uia import read_address_bar


class UrlSource:
    """Where a captured URL actually came from.

    Recorded so nothing downstream has to guess how trustworthy a URL is,
    and so "we could not read it" is a state the pipeline can carry rather
    than a hole someone fills with a placeholder.
    """

    #: Read from the browser's real address bar via UI Automation.
    ADDRESS_BAR = "address_bar"
    #: Inferred from a URL/domain that the window title itself contained.
    WINDOW_TITLE = "window_title"
    #: The browser is supported but no URL could be read right now.
    UNAVAILABLE = "unavailable"


class BaseBrowserAdapter(ABC):
    """
    Base class for browser URL and title extraction adapters.

    Subclasses supply the process names they own and the title decorations
    that browser appends; the extraction strategy itself is shared, so all
    browsers agree on precedence:

        1. the real address bar (UI Automation), then
        2. a URL or domain that the window title happens to contain, then
        3. nothing -- reported honestly as `UrlSource.UNAVAILABLE`.
    """

    browser_name: str = "Generic Browser"
    #: Process names (Windows) and localizedName() values (macOS) this owns.
    SUPPORTED_PROCESSES: frozenset = frozenset()
    #: Trailing decorations this browser appends to the page title.
    TITLE_SUFFIXES: Tuple[str, ...] = ()
    #: Titles that carry no page identity at all.
    EMPTY_TITLES: frozenset = frozenset({"new tab"})

    #: Decorations a *private* window adds to the title, stripped before the
    #: page title is stored. Without these a private window's page title keeps
    #: the browser's own marker in it ("Search - Bing - [InPrivate]"), which
    #: would both look wrong in a report and split a page's segment from its
    #: normal-window self for no reason. Stripped in addition to
    #: `TITLE_SUFFIXES`, not instead of them.
    PRIVATE_TITLE_DECORATIONS: Tuple[str, ...] = ()

    #: Whether this adapter's browser is Gecko rather than Chromium. Private
    #: state is read from a Chromium view tree that Firefox does not have, so
    #: the detector needs to know which mechanism applies. Declared on the
    #: adapter that knows, rather than re-sniffed from the process name at the
    #: point of use.
    IS_GECKO: bool = False

    #: Matches the " and 3 more pages" that Chromium appends when a window
    #: has several tabs, optionally followed by the profile name Edge adds
    #: after it ("Hubstaff - Dashboard and 12 more pages - Personal"). Both
    #: are window decoration, not part of the page's own title.
    _MORE_PAGES = re.compile(
        r"\s+and \d+ more pages?(?:\s+-\s+[^-]{1,40})?$", re.IGNORECASE
    )

    #: How long an address-bar read is reused for an unchanged window. A UIA
    #: round trip costs milliseconds, and re-entering COM on every
    #: two-second sample for hours is a cost worth not paying. The read is
    #: still refreshed periodically rather than pinned: a single-page app can
    #: change its URL without changing the window title, and a browser that
    #: momentarily exposed nothing must not be written off for the rest of
    #: the session.
    URL_CACHE_TTL_SECONDS = 10.0

    def __init__(self) -> None:
        self._cache_key: Optional[Tuple[int, str]] = None
        self._cache_url: Optional[str] = None
        self._cache_at: float = 0.0

    def is_supported_app(self, app_name: str) -> bool:
        if not app_name:
            return False
        clean_name = os.path.basename(app_name).lower()
        if clean_name.endswith(".exe"):
            clean_name = clean_name[:-4]
        return clean_name in self.SUPPORTED_PROCESSES

    # ── Title handling ────────────────────────────────────────────────────────

    def _clean_title(self, window_title: str) -> Optional[str]:
        """Strip the browser's own decorations off the page title.

        Private-window decorations go first: Edge writes its marker *before*
        the browser name ("… - [InPrivate] - Microsoft Edge"), so a suffix
        pass that ran first would find no match and leave the marker embedded
        in the stored page title.
        """
        if not window_title:
            return None
        title = window_title.strip()
        stripped_private = False
        for decoration in self.PRIVATE_TITLE_DECORATIONS:
            if decoration in title:
                title = title.replace(decoration, "")
                stripped_private = True
        if stripped_private:
            # Removing a decoration from the middle of the title leaves the
            # separators that surrounded it ("A -  - B"). Collapsed only on
            # this path, so an ordinary title that genuinely contains a dash
            # is never rewritten.
            title = re.sub(r"\s*-\s*-\s*", " - ", title)
            title = re.sub(r"\s{2,}", " ", title).strip().strip("-").strip()
        for suffix in self.TITLE_SUFFIXES:
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()
                break
        title = self._MORE_PAGES.sub("", title).strip()
        if not title or title.lower() in self.EMPTY_TITLES:
            return None
        return title

    def _url_from_title(self, title: Optional[str]) -> Optional[str]:
        """A URL only if the title literally contains one."""
        if not title:
            return None
        explicit = re.search(r"(https?://[^\s]+)", title)
        if explicit:
            return explicit.group(1)
        bare_domain = re.search(r"([a-zA-Z0-9-]+\.[a-zA-Z]{2,})(?:/[^\s]*)?", title)
        if bare_domain:
            return f"https://{bare_domain.group(0)}"
        return None

    # ── Extraction ────────────────────────────────────────────────────────────

    def _read_address_bar(self, hwnd: int, window_title: str) -> Optional[str]:
        key = (hwnd, window_title or "")
        now = time.monotonic()
        if key == self._cache_key and now - self._cache_at < self.URL_CACHE_TTL_SECONDS:
            return self._cache_url
        url = read_address_bar(hwnd)
        self._cache_key = key
        self._cache_url = url
        self._cache_at = now
        return url

    def extract_url_info(
        self,
        hwnd: int,
        window_title: str,
    ) -> Tuple[str, Optional[str], Optional[str], str]:
        """
        Extract (browser_name, url, page_title, url_source) for a window.

        `url` is None exactly when `url_source` is `UrlSource.UNAVAILABLE`;
        callers must record no URL in that case rather than substituting a
        placeholder.
        """
        page_title = self._clean_title(window_title)

        url = self._read_address_bar(hwnd, window_title)
        if url:
            return self.browser_name, url, page_title or window_title, UrlSource.ADDRESS_BAR

        url = self._url_from_title(page_title)
        if url:
            return self.browser_name, url, page_title or window_title, UrlSource.WINDOW_TITLE

        return self.browser_name, None, page_title or window_title, UrlSource.UNAVAILABLE
