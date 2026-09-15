from tracking.browsers.base import BaseBrowserAdapter


class FirefoxAdapter(BaseBrowserAdapter):
    """Firefox.

    Gecko only exposes its URL bar to UI Automation when accessibility
    services are active, so the address-bar read genuinely does fail on some
    installations. That case is reported as `UrlSource.UNAVAILABLE` and the
    browser's own usage is still recorded -- it is never papered over with a
    guessed URL.
    """

    browser_name = "Mozilla Firefox"

    #: Gecko, not Chromium: there is no `BrowserRootView` to read, so private
    #: state comes from the window title, which Firefox does mark.
    IS_GECKO = True

    SUPPORTED_PROCESSES = frozenset({
        "firefox", "firefox.exe",
        "mozilla firefox",  # macOS: NSWorkspace's localizedName()
    })

    # The private-window suffix comes first: a private window's title ends
    # '… — Mozilla Firefox Private Browsing', so the plain ' — Mozilla Firefox'
    # suffix does not match it and the marker would survive into the stored
    # page title. Confirmed against a real private window.
    TITLE_SUFFIXES = (
        " — Mozilla Firefox Private Browsing",
        " - Mozilla Firefox Private Browsing",
        " — Private Browsing",
        " - Private Browsing",
        " — Mozilla Firefox", " - Mozilla Firefox", " - Firefox",
    )

    EMPTY_TITLES = frozenset({
        "new tab", "mozilla firefox", "private browsing",
    })
