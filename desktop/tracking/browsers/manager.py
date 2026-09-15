import re
from typing import List, NamedTuple, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from tracking.app_identity import canonical_domain, resolve_application
from tracking.browsers.base import BaseBrowserAdapter, UrlSource
from tracking.browsers.chrome import ChromeAdapter
from tracking.browsers.edge import EdgeAdapter
from tracking.browsers.firefox import FirefoxAdapter
from tracking.browsers.private_mode import PrivateState, PrivateWindowDetector

KNOWN_SITE_DOMAINS = {
    "github": "github.com",
    "stackoverflow": "stackoverflow.com",
    "stack overflow": "stackoverflow.com",
    "google": "google.com",
    "youtube": "youtube.com",
    "figma": "figma.com",
    "gitlab": "gitlab.com",
    "linear": "linear.app",
    "notion": "notion.so",
    "jira": "atlassian.net",
    "confluence": "atlassian.net",
    "slack": "slack.com",
    "reddit": "reddit.com",
    "twitter": "twitter.com",
    "x": "x.com",
    "linkedin": "linkedin.com",
}

def normalize_domain_and_url(
    url_str: Optional[str],
    window_title: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Extracts lowercased domain and normalized URL from URL string or window title.

    Returns ``(None, None)`` when neither the URL nor the title identifies a
    site. This used to return the sentinel ``"unknown-domain"`` instead, which
    the summary builder then rendered as the clickable link
    ``https://unknown-domain`` -- a URL the user had never visited, shown as
    if it were real. There is no placeholder here any more: callers treat
    ``(None, None)`` as "nothing to record".
    """
    if url_str and url_str.strip():
        clean_url = url_str.strip()
        try:
            parsed = urlparse(clean_url)
            if not parsed.scheme or not parsed.netloc:
                parsed = urlparse(f"https://{clean_url}")
            
            # `www.` is a routing prefix, not a different site. Left on, the
            # same site arrived as two report rows -- www.bing.com and
            # bing.com -- each holding half the time and each small enough
            # to be pushed out of the top of the list.
            domain = canonical_domain(parsed.hostname) or ""
            path = parsed.path
            if len(path) > 1 and path.endswith('/'):
                path = path.rstrip('/')

            norm_url = urlunparse((
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                path,
                parsed.params,
                parsed.query,
                parsed.fragment
            ))
            if domain:
                return domain, norm_url
        except Exception:
            pass

    # Fallback to domain extraction from title if URL parsing produced no domain
    if window_title:
        title = window_title.strip().lower()
        # Look for explicit domain patterns in title e.g. "docs.python.org"
        domain_match = re.search(r'([a-zA-Z0-9-]+\.(?:com|org|net|io|dev|edu|gov|co|in|app|ai|info|me|ca|uk|us|de|fr))[^\w]*', title)
        if domain_match:
            dom = canonical_domain(domain_match.group(1))
            if dom:
                return dom, f"https://{dom}"

        # Look for known site keywords in title e.g. "GitHub" -> "github.com".
        # The match is on whole words: a bare substring test made the
        # single-letter key "x" match any title containing the letter, so a
        # Firefox window with no page open reported the user as browsing
        # x.com -- "mozilla firefox" contains an "x".
        for kw, dom in KNOWN_SITE_DOMAINS.items():
            if re.search(rf"\b{re.escape(kw)}\b", title):
                return dom, f"https://{dom}"

    # Nothing identified the site. Say so.
    return None, None


class BrowserObservation(NamedTuple):
    """One sample of what a browser window is currently showing.

    `domain` and `url` are None together, exactly when `url_source` is
    `UrlSource.UNAVAILABLE`. Consumers must not invent a stand-in for them.

    `is_private` is three-valued for the same reason: True and False are
    findings, None is "this platform or browser could not tell us". Private
    browsing is otherwise an ordinary observation — same fields, same
    pipeline, same storage — because it is ordinary browsing that happened in
    a different kind of window.
    """

    browser_name: str
    domain: Optional[str]
    url: Optional[str]
    page_title: Optional[str]
    url_source: str
    is_private: Optional[bool] = None
    #: The browser's own word for the private state ("incognito",
    #: "inprivate"), for logs. None when the window is not private.
    private_marker: Optional[str] = None

    @property
    def has_url(self) -> bool:
        return self.domain is not None


class BrowserManager:
    """Coordinates browser detection and URL extraction across registered adapters."""

    def __init__(
        self,
        adapters: Optional[List[BaseBrowserAdapter]] = None,
        private_detector: Optional[PrivateWindowDetector] = None,
    ) -> None:
        self.adapters: List[BaseBrowserAdapter] = adapters or [
            ChromeAdapter(),
            EdgeAdapter(),
            FirefoxAdapter(),
        ]
        #: One detector for every browser: private state is read the same way
        #: whichever Chromium build is in front, and a second instance would
        #: just be a second cache of the same verdicts.
        self.private_detector = private_detector or PrivateWindowDetector()

    def get_adapter(self, app_name: str) -> Optional[BaseBrowserAdapter]:
        if not app_name:
            return None
        for adapter in self.adapters:
            if adapter.is_supported_app(app_name):
                return adapter
        return None

    def is_browser_app(self, app_name: str) -> bool:
        return self.get_adapter(app_name) is not None

    def extract_browser_info(
        self,
        app_name: str,
        window_title: str,
        hwnd: int = 0
    ) -> Optional[BrowserObservation]:
        """
        Extracts browser usage info if app_name is a supported browser.

        Returns None when the application is not a browser at all. For a
        browser it always returns an observation, even when no URL could be
        read -- `domain` and `url` are then None and `url_source` is
        `UrlSource.UNAVAILABLE`, so the caller can distinguish "not a
        browser" from "a browser whose URL we honestly cannot see".
        """
        adapter = self.get_adapter(app_name)
        if not adapter:
            return None

        _adapter_name, raw_url, page_title, url_source = adapter.extract_url_info(
            hwnd, window_title
        )

        # Read before the URL is normalised, so the private state travels with
        # the observation whether or not a URL could be read. A private window
        # whose address bar is unreadable still has to be recorded as browser
        # usage -- private browsing must never be the reason activity vanishes.
        private_state, private_marker = self.private_detector.detect(
            hwnd, window_title, is_firefox=adapter.IS_GECKO
        )
        # The adapter knows how to *read* a Chromium omnibox; it does not
        # know which Chromium browser this is. `ChromeAdapter` serves Chrome,
        # Brave, Vivaldi and Opera, and reported all four as "Google Chrome"
        # -- so a Brave user's browsing was filed under a browser they do not
        # have, and disagreed with the application-usage row for the very
        # same session, which said "brave". The identity comes from the one
        # catalogue instead, so both tables name the same product.
        identity = resolve_application(process_name=app_name)
        browser_name = identity.name or _adapter_name
        domain, normalized_url = normalize_domain_and_url(raw_url, page_title or window_title)

        if domain is None:
            # Neither the address bar nor the title identified a site.
            url_source = UrlSource.UNAVAILABLE
        elif url_source == UrlSource.UNAVAILABLE:
            # The adapter read no address bar, but a site keyword in the title
            # identified the domain. That is a weaker signal, and is labelled
            # as such rather than being passed off as a real address-bar read.
            url_source = UrlSource.WINDOW_TITLE

        return BrowserObservation(
            browser_name=browser_name,
            domain=domain,
            url=normalized_url,
            page_title=page_title or window_title,
            url_source=url_source,
            is_private=PrivateState.to_bool(private_state),
            private_marker=private_marker,
        )


_instance: Optional[BrowserManager] = None

def get_browser_manager() -> BrowserManager:
    global _instance
    if _instance is None:
        _instance = BrowserManager()
    return _instance
