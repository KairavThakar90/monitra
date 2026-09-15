from tracking.browsers.base import BaseBrowserAdapter


class ChromeAdapter(BaseBrowserAdapter):
    """Chromium-family browsers that are not Edge.

    All of these expose the omnibox as a UI Automation edit control, so
    `BaseBrowserAdapter` reads the real URL from it; the title handling
    below only matters as the fallback.
    """

    browser_name = "Google Chrome"

    SUPPORTED_PROCESSES = frozenset({
        # Windows: the .exe's own base name (process name / class name).
        "chrome", "chrome.exe",
        "brave", "brave.exe",
        "vivaldi", "vivaldi.exe",
        "opera", "opera.exe",
        "opera_gx", "opera_gx.exe",
        # macOS: NSWorkspace's localizedName() for each app -- a display
        # name, not an executable name, so it looks nothing like the
        # Windows entries above.
        "google chrome", "brave browser", "opera", "vivaldi",
    })

    TITLE_SUFFIXES = (
        " - Google Chrome", " - Chrome", " - Brave",
        " - Vivaldi", " - Opera", " - New Tab",
    )

    # Chrome itself writes *no* marker into the window title -- a window opened
    # with --incognito reports the plain 'Wikipedia - Google Chrome'. These
    # cover the Chromium forks that do decorate the title, and cost nothing
    # where the browser does not; the private verdict itself never comes from
    # here, it comes from the BrowserRootView reading in `private_mode`.
    PRIVATE_TITLE_DECORATIONS = (
        " - Incognito", " (Incognito)",
        " - Private", " (Private)",
        " - Private Window", " (Private Window)",
    )
