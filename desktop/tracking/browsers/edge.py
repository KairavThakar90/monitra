from tracking.browsers.base import BaseBrowserAdapter


class EdgeAdapter(BaseBrowserAdapter):
    browser_name = "Microsoft Edge"

    SUPPORTED_PROCESSES = frozenset({
        "msedge", "msedge.exe", "edge", "edge.exe",  # Windows
        "microsoft edge",  # macOS: NSWorkspace's localizedName()
    })

    # Edge writes a zero-width space into its own name in the window title
    # ("Microsoft​ Edge"), so the suffix list has to carry both forms
    # or the browser name survives into the page title.
    TITLE_SUFFIXES = (
        " - Microsoft​ Edge", " - Microsoft Edge", " - Edge",
    )

    # Edge puts its private marker in the *middle* of the window title, before
    # the browser name: 'Search - Microsoft Bing - [InPrivate] - Microsoft Edge'.
    # Confirmed against a real InPrivate window, not assumed.
    PRIVATE_TITLE_DECORATIONS = (" - [InPrivate]", "[InPrivate]", "[InPrivate] ")

    EMPTY_TITLES = frozenset({"new tab", "start"})
