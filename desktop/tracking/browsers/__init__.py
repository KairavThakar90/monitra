from tracking.browsers.base import BaseBrowserAdapter, UrlSource
from tracking.browsers.chrome import ChromeAdapter
from tracking.browsers.edge import EdgeAdapter
from tracking.browsers.firefox import FirefoxAdapter
from tracking.browsers.manager import (
    BrowserManager,
    BrowserObservation,
    get_browser_manager,
)
from tracking.browsers.private_mode import PrivateState, PrivateWindowDetector

__all__ = [
    "BaseBrowserAdapter",
    "UrlSource",
    "ChromeAdapter",
    "EdgeAdapter",
    "FirefoxAdapter",
    "BrowserManager",
    "BrowserObservation",
    "PrivateState",
    "PrivateWindowDetector",
    "get_browser_manager",
]
