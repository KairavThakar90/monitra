"""
icon_manager — Async favicon and application icon loader & cacher.

Provides thread-safe, non-blocking icon resolution for desktop Activity views.
Supports Win32 HICON extraction directly from running top-level windows (HWND).
"""
from __future__ import annotations

import os
import re
import sys
import threading
import urllib.request
import concurrent.futures
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Signal, QUrl, QFileInfo
from PySide6.QtGui import QPixmap, QImage, QDesktopServices
from PySide6.QtWidgets import QFileIconProvider


def _hicon_to_image(hicon: int) -> Optional[QImage]:
    """Converts a Windows HICON handle to a QImage via Win32 GDI.

    A QImage, deliberately: this runs on a worker thread, and a QImage is
    the one image type Qt allows there. The QPixmap is made from it on the
    GUI thread (`IconManager._on_resolved`).
    """
    if not hicon or sys.platform != "win32":
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        hdc_screen = user32.GetDC(0)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, 32, 32)
        hbmp_old = gdi32.SelectObject(hdc_mem, hbmp)

        # Draw icon onto GDI bitmap
        user32.DrawIconEx(hdc_mem, 0, 0, hicon, 32, 32, 0, 0, 3)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = 32
        bmi.biHeight = -32  # Top-down bitmap
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        buf = ctypes.create_string_buffer(32 * 32 * 4)
        gdi32.GetDIBits(hdc_mem, hbmp, 0, 32, buf, ctypes.byref(bmi), 0)

        gdi32.SelectObject(hdc_mem, hbmp_old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)

        # `.copy()`: the QImage above wraps `buf`, which dies with this frame.
        img = QImage(buf.raw, 32, 32, QImage.Format.Format_ARGB32).copy()
        if not img.isNull() and img.width() > 1:
            return img
    except Exception:
        pass
    return None


def _get_window_hicon(hwnd: int) -> int:
    """Queries top-level window HWND for WM_GETICON or GCLP_HICON."""
    if not hwnd or sys.platform != "win32":
        return 0
    try:
        import ctypes
        user32 = ctypes.windll.user32
        WM_GETICON = 0x007F
        res = ctypes.c_size_t()

        for icon_type in (1, 2, 0):  # BIG, SMALL2, SMALL
            if user32.SendMessageTimeoutW(hwnd, WM_GETICON, icon_type, 0, 0x0002, 100, ctypes.byref(res)) and res.value:
                return res.value

        hicon = user32.GetClassLongPtrW(hwnd, -14) or user32.GetClassLongPtrW(hwnd, -34)
        if hicon:
            return hicon
    except Exception:
        pass
    return 0


def _normalize(name: str) -> str:
    """Lowercase, alphanumerics only.

    Tracked application names are executable basenames ("sublime_text",
    "notepad++"), while installed shortcuts and bundles are display names
    ("Sublime Text.lnk", "Notepad++.app"). Normalizing both sides is what
    lets one match the other without a hand-maintained entry per app.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _running_process_path(app_name: str) -> Optional[str]:
    """Full path of a running process whose executable matches `app_name`.

    Windows only, via Toolhelp32 + QueryFullProcessImageNameW (ctypes -- the
    same API tracking/active_window.py already uses, no new dependency).
    This is the highest-value lookup for the Activity list: every app in it
    was, by definition, in the foreground recently, so most are still
    running and their real icon is one snapshot away.
    """
    if sys.platform != "win32" or not app_name:
        return None

    target = _normalize(app_name)
    if not target:
        return None

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        TH32CS_SNAPPROCESS = 0x00000002
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == INVALID_HANDLE_VALUE:
            return None

        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        try:
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return None
            while True:
                exe_name = entry.szExeFile
                stem = exe_name[:-4] if exe_name.lower().endswith(".exe") else exe_name
                if _normalize(stem) == target:
                    handle = kernel32.OpenProcess(
                        PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID
                    )
                    if handle:
                        try:
                            buf = ctypes.create_unicode_buffer(1024)
                            size = ctypes.c_ulong(1024)
                            if kernel32.QueryFullProcessImageNameW(
                                handle, 0, buf, ctypes.byref(size)
                            ) and os.path.exists(buf.value):
                                return buf.value
                        finally:
                            kernel32.CloseHandle(handle)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        pass
    return None


def _registry_app_path(app_name: str) -> Optional[str]:
    """Installed location from the Windows "App Paths" registry key.

    Covers applications that are installed but not currently running -- the
    Activity list still shows them for the rest of the day after they are
    closed.
    """
    if sys.platform != "win32" or not app_name:
        return None
    try:
        import winreg
    except ImportError:
        return None

    exe = app_name if app_name.lower().endswith(".exe") else f"{app_name}.exe"
    sub_key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, sub_key) as key:
                value, _ = winreg.QueryValueEx(key, None)
                path = (value or "").strip('"')
                if path and os.path.exists(path):
                    return path
        except OSError:
            continue
    return None


#: Lazily built once per process: normalized shortcut name -> .lnk path.
#: Walking the Start Menu is cheap but not free, and it only ever happens
#: on the icon worker's threads, never on the GUI thread.
_start_menu_index: Optional[Dict[str, str]] = None
_start_menu_lock = threading.Lock()


def _start_menu_shortcut(app_name: str) -> Optional[str]:
    """A Start Menu .lnk whose name matches `app_name`.

    QFileIconProvider reads a shortcut's icon directly, so this resolves the
    long tail of installed applications that have neither an App Paths entry
    nor a running process.
    """
    if sys.platform != "win32" or not app_name:
        return None

    global _start_menu_index
    with _start_menu_lock:
        if _start_menu_index is None:
            index: Dict[str, str] = {}
            roots: List[str] = [
                os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
                os.path.expandvars(r"%AppData%\Microsoft\Windows\Start Menu\Programs"),
            ]
            for root in roots:
                if not os.path.isdir(root):
                    continue
                try:
                    for dirpath, _dirs, files in os.walk(root):
                        for filename in files:
                            if not filename.lower().endswith(".lnk"):
                                continue
                            key = _normalize(os.path.splitext(filename)[0])
                            if key:
                                index.setdefault(key, os.path.join(dirpath, filename))
                except OSError:
                    continue
            _start_menu_index = index
        return _start_menu_index.get(_normalize(app_name))


def _macos_app_bundle(app_name: str) -> Optional[str]:
    """A matching .app bundle in the standard application directories."""
    if sys.platform != "darwin" or not app_name:
        return None
    target = _normalize(app_name)
    for root in ("/Applications", "/System/Applications",
                 os.path.expanduser("~/Applications")):
        if not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                if entry.endswith(".app") and _normalize(entry[:-4]) == target:
                    return os.path.join(root, entry)
        except OSError:
            continue
    return None


class IconManager(QObject):
    """Singleton icon manager for loading and caching website favicons and application icons."""

    favicon_ready = Signal(str, QPixmap)  # domain, pixmap
    app_icon_ready = Signal(str, QPixmap)  # app_name, pixmap

    #: The hand-off from a worker thread to the GUI thread: (kind, key,
    #: payload). Only thread-safe things cross it -- a QImage, a file path,
    #: or None. The QPixmap is built, cached and announced in `_on_resolved`,
    #: on the GUI thread, which is the only thread Qt allows a QPixmap on.
    #: The workers used to build the QPixmap themselves and emit it across
    #: threads; that is undefined behaviour in Qt, and it surfaced as
    #: intermittent access violations on the GUI thread while the pixmaps
    #: were being painted or freed.
    _resolved = Signal(str, str, object)

    _instance: Optional[IconManager] = None

    def __init__(self) -> None:
        super().__init__()
        self._favicon_cache: Dict[str, Optional[QPixmap]] = {}
        self._app_icon_cache: Dict[str, Optional[QPixmap]] = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self._file_icon_provider = QFileIconProvider()
        # Emitted from the pool, so Qt queues it onto this object's thread.
        self._resolved.connect(self._on_resolved)

        # Known application alias map
        self._app_alias_map = {
            "code": "vscode",
            "code.exe": "vscode",
            "visual studio code": "vscode",

            "msedge": "edge",
            "msedge.exe": "edge",
            "microsoft edge": "edge",

            "chrome": "chrome",
            "chrome.exe": "chrome",
            "google chrome": "chrome",

            "firefox": "firefox",
            "firefox.exe": "firefox",
            "mozilla firefox": "firefox",

            "python": "python",
            "python.exe": "python",

            "notepad++": "notepadplusplus",
            "notepad++.exe": "notepadplusplus",

            "snippingtool": "snippingtool",
            "snippingtool.exe": "snippingtool",
            "snipping tool": "snippingtool",

            "teams": "teams",
            "teams.exe": "teams",
            "ms-teams": "teams",
            "microsoft teams": "teams",

            "chatgpt": "chatgpt",
            "chatgpt.exe": "chatgpt",

            "antigravity": "antigravity",
            "monitra": "monitra",
            "hubstaff": "hubstaff",
            "searchhost": "searchhost",
        }

        # Known installation paths, Windows and macOS side by side per app --
        # QFileIconProvider (used below) reads a native icon from whichever
        # of these actually exists, .exe or .app bundle alike, so no
        # platform branching is needed here: a path for the "other" OS
        # simply never exists and os.path.exists() skips it, exactly like
        # it already did before macOS paths were added. Some Windows-only
        # utilities (Notepad++, Snipping Tool) have no macOS equivalent and
        # are intentionally left Windows-only rather than pointed at
        # something unrelated.
        self._common_app_paths = {
            "chrome": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                "/Applications/Google Chrome.app",
            ],
            "edge": [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                "/Applications/Microsoft Edge.app",
            ],
            "firefox": [
                r"C:\Program Files\Mozilla Firefox\firefox.exe",
                "/Applications/Firefox.app",
            ],
            "vscode": [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
                r"C:\Program Files\Microsoft VS Code\Code.exe",
                "/Applications/Visual Studio Code.app",
            ],
            "notepadplusplus": [
                r"C:\Program Files\Notepad++\notepad++.exe",
            ],
            "snippingtool": [
                r"C:\Windows\System32\SnippingTool.exe",
            ],
            "teams": [
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Teams\current\Teams.exe"),
                "/Applications/Microsoft Teams.app",
            ],
            "chatgpt": [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\ChatGPT\ChatGPT.exe"),
                "/Applications/ChatGPT.app",
            ],
        }

    @classmethod
    def instance(cls) -> IconManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Favicon Loader ────────────────────────────────────────────────────────

    def get_favicon(self, domain: str, title: str = "") -> Optional[QPixmap]:
        if not domain or domain == "unknown-domain":
            # Attempt domain extraction from title if unknown
            if title:
                t_lower = title.lower()
                if "edge" in t_lower:
                    domain = "microsoft.com"
                elif "chrome" in t_lower:
                    domain = "google.com"
                elif "firefox" in t_lower:
                    domain = "mozilla.org"

        if not domain or domain == "unknown-domain":
            return None

        clean_dom = domain.lower().strip()
        if clean_dom in self._favicon_cache:
            return self._favicon_cache[clean_dom]

        self._executor.submit(self._fetch_favicon_worker, clean_dom)
        return None

    def _fetch_favicon_worker(self, domain: str) -> None:
        url = f"https://www.google.com/s2/favicons?domain={domain}&sz=64"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = resp.read()
                if data:
                    image = QImage()
                    if image.loadFromData(data) and not image.isNull():
                        self._resolved.emit("favicon", domain, image)
                        return
        except Exception:
            pass

        self._resolved.emit("favicon", domain, None)

    # ── App Icon Loader ───────────────────────────────────────────────────────

    @staticmethod
    def app_icon_key(app_name: str, exe_path: Optional[str] = None) -> str:
        """The key an app icon is cached and announced under.

        Public because `app_icon_ready` carries this key, not the app name:
        a row given an exe_path was keyed by that path while it compared the
        signal against its own name, so the icon it asked for never arrived
        and the letter badge stayed. Callers match on this.
        """
        return (exe_path or app_name or "").lower().strip()

    def get_app_icon(self, app_name: str, exe_path: Optional[str] = None, hwnd: Optional[int] = None) -> Optional[QPixmap]:
        if not app_name and not exe_path and not hwnd:
            return None

        cache_key = self.app_icon_key(app_name, exe_path)
        if cache_key in self._app_icon_cache:
            return self._app_icon_cache[cache_key]

        self._executor.submit(self._extract_app_icon_worker, cache_key, app_name, exe_path, hwnd)
        return None

    def _extract_app_icon_worker(self, cache_key: str, app_name: str, exe_path: Optional[str], hwnd: Optional[int]) -> None:
        """Find where an application's icon comes from. Runs on the pool.

        Everything slow lives here -- process, registry and Start Menu
        lookups, the GDI read of a window's icon -- and everything Qt-visual
        does not: the result handed to `_on_resolved` is Monitra's own mark
        (by name), a QImage, or a file path for QFileIconProvider to read on
        the GUI thread.
        """
        norm_name = app_name.lower().strip()
        if norm_name in ("monitra", "python", "python.exe", "main.py"):
            self._resolved.emit("app_own", cache_key, None)
            return

        # 1. Try Win32 HICON from active window handle (HWND)
        if hwnd:
            hicon = _get_window_hicon(hwnd)
            if hicon:
                image = _hicon_to_image(hicon)
                if image is not None and not image.isNull():
                    self._resolved.emit("app_image", cache_key, image)
                    return

        # 2. Try direct exe_path extraction via QFileIconProvider
        target_path = exe_path if (exe_path and os.path.exists(exe_path)) else None

        # 3. Check known paths via alias map
        if not target_path:
            norm_name = app_name.lower().strip()
            alias = self._app_alias_map.get(norm_name, norm_name)
            candidate_paths = self._common_app_paths.get(alias, [])
            for p in candidate_paths:
                if os.path.exists(p):
                    target_path = p
                    break

        # 4. Generic resolution -- the hand-maintained map above only ever
        #    covered a handful of applications, so everything else fell
        #    through to a coloured initials badge. These three cover any
        #    application without naming it: one that is still running, one
        #    that is installed, and one that has a Start Menu shortcut (plus
        #    the .app bundle equivalent on macOS).
        if not target_path:
            for resolve in (
                _running_process_path,
                _registry_app_path,
                _start_menu_shortcut,
                _macos_app_bundle,
            ):
                candidate = resolve(app_name)
                if candidate:
                    target_path = candidate
                    break

        self._resolved.emit("app_path", cache_key, target_path)

    def _on_resolved(self, kind: str, key: str, payload: object) -> None:
        """Build, cache and announce the pixmap. GUI thread only."""
        pixmap: Optional[QPixmap] = None
        minimum_width = 1
        try:
            if kind == "favicon":
                if isinstance(payload, QImage) and not payload.isNull():
                    pixmap = QPixmap.fromImage(payload)
            elif kind == "app_own":
                from background_services.public_api import create_app_icon
                pixmap = create_app_icon().pixmap(48, 48)
            elif kind == "app_image":
                if isinstance(payload, QImage) and not payload.isNull():
                    pixmap = QPixmap.fromImage(payload)
            elif kind == "app_path":
                if payload:
                    icon = self._file_icon_provider.icon(QFileInfo(str(payload)))
                    if not icon.isNull():
                        pixmap = icon.pixmap(48, 48)
                        minimum_width = 8
        except Exception:
            pixmap = None

        usable = (
            pixmap is not None and not pixmap.isNull()
            and pixmap.width() > minimum_width and pixmap.height() > 1
        )
        if kind == "favicon":
            self._favicon_cache[key] = pixmap if usable else None
            if usable:
                self.favicon_ready.emit(key, pixmap)
            return
        self._app_icon_cache[key] = pixmap if usable else None
        if usable:
            self.app_icon_ready.emit(key, pixmap)


def get_icon_manager() -> IconManager:
    return IconManager.instance()


def safe_open_url(url_str: str) -> bool:
    if not url_str or not isinstance(url_str, str):
        return False

    clean_url = url_str.strip()
    if not clean_url.startswith(("http://", "https://")):
        clean_url = f"https://{clean_url}"

    qurl = QUrl(clean_url)
    if qurl.isValid() and qurl.scheme().lower() in ("http", "https"):
        return QDesktopServices.openUrl(qurl)

    return False
