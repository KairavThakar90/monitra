"""
uia — Read a browser's address bar through Windows UI Automation.

Why this module exists
----------------------
Before it, no browser adapter had *any* real URL source. `ChromeAdapter`
carried a `_extract_via_uia()` that unconditionally returned `None`, so the
only signal left was the window title, and a title like "ChatGPT - SMS"
contains no URL at all. The pipeline then invented one: the title parser
returned no domain, `normalize_domain_and_url` fell back to the sentinel
`"unknown-domain"`, and the summary builder rendered it as the link
`https://unknown-domain`. That is fabricated data of exactly the kind
CLAUDE.md §2 forbids.

This reads the actual address bar instead. UI Automation is the mechanism
Chromium and Gecko already expose to screen readers, so it needs no browser
extension, no automation/debugging port, no injected code and no elevation.

Implementation notes
--------------------
The COM plumbing is raw ctypes vtable dispatch rather than `comtypes`, to
keep `requirements.txt` (PySide6, httpx, python-dotenv, pynput, tzdata) as
it is -- adding a COM binding to every packaged build for one address-bar
read is not a trade worth making.

COM apartments are per-thread, so the `IUIAutomation` instance is created
lazily per thread and cached in a thread-local. In practice only the
`url_usage` service thread ever calls in here, which is the point: nothing
on the GUI thread blocks on a UIA round trip.

Measured cost on the development machine: ~8 ms for Chrome, ~29 ms for
Edge, and ~85 ms for a Firefox window that exposes no address bar (the
whole-subtree search has to fail before returning). Callers are expected to
avoid paying even that on every sample -- see `ChromiumAdapter`'s
title-keyed memoisation.
"""
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import POINTER, byref, c_int, c_longlong, c_ushort, c_void_p, c_wchar_p
from typing import Optional
from urllib.parse import urlparse

from core.logging_setup import get_logger

log = get_logger("tracking.browsers.uia")

#: True when this module can do anything at all.
SUPPORTED = sys.platform == "win32"

# ── COM / UI Automation constants ────────────────────────────────────────────

_CLSID_CUIAutomation = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
_IID_IUIAutomation = "{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}"

_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

_VT_I4 = 3
_VT_BSTR = 8

_UIA_NamePropertyId = 30005
_UIA_ControlTypePropertyId = 30003
_UIA_ValueValuePropertyId = 30045
_UIA_ClassNamePropertyId = 30012
_UIA_EditControlTypeId = 50004
_TreeScope_Descendants = 4

# `SysAllocString` returns a pointer. Without an explicit restype ctypes
# assumes `int`, which truncates it to 32 bits on x64 and the first use of the
# resulting "pointer" is an access violation that takes the whole process with
# it — not an exception this module could catch and report.
#
# Resolved only where it exists. `ctypes.windll` is a Windows-only attribute;
# evaluating it at import time made importing this module -- and therefore
# `tracking.browsers`, `UrlUsageService` and `core.runtime` above it -- raise
# AttributeError on macOS and Linux, so the packaged macOS application could
# not start and the Linux test run could not even collect. Every caller below
# is already gated on SUPPORTED, so off Windows this handle is simply absent.
if SUPPORTED:
    _oleaut32 = ctypes.windll.oleaut32
    _oleaut32.SysAllocString.restype = c_void_p
    _oleaut32.SysAllocString.argtypes = [c_wchar_p]
    _oleaut32.SysFreeString.argtypes = [c_void_p]
else:  # pragma: no cover - exercised by the import-portability test
    _oleaut32 = None

# IUnknown occupies vtable slots 0-2; every index below is an offset into the
# full vtable of the named interface, in declaration order.
_VT_IUNKNOWN_RELEASE = 2
_VT_IUIAUTOMATION_ELEMENT_FROM_HANDLE = 6
_VT_IUIAUTOMATION_CREATE_PROPERTY_CONDITION = 23
_VT_IUIAUTOMATIONELEMENT_FIND_FIRST = 5
_VT_IUIAUTOMATIONELEMENT_GET_CURRENT_PROPERTY_VALUE = 10


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_ulong),
        ("data2", ctypes.c_ushort),
        ("data3", ctypes.c_ushort),
        ("data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str) -> None:
        super().__init__()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(self))


class _VARIANT(ctypes.Structure):
    """Minimal VARIANT: the 8-byte header plus the 16-byte x64 union."""

    _fields_ = [
        ("vt", c_ushort),
        ("_r1", c_ushort),
        ("_r2", c_ushort),
        ("_r3", c_ushort),
        ("val", c_longlong),
        ("_val_high", c_longlong),
    ]


_local = threading.local()


def _vtable_call(interface: c_void_p, index: int, restype, *argtypes):
    vtable = ctypes.cast(interface, POINTER(POINTER(c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return prototype(vtable[index])


def _release(interface: Optional[c_void_p]) -> None:
    if interface:
        _vtable_call(interface, _VT_IUNKNOWN_RELEASE, ctypes.c_ulong)(interface)


def _automation() -> Optional[c_void_p]:
    """The calling thread's IUIAutomation instance, created on first use.

    A thread that has already failed to obtain one is remembered as such, so
    a machine where UI Automation is unavailable costs one failed attempt
    per thread rather than one per sample.
    """
    if getattr(_local, "failed", False):
        return None
    instance = getattr(_local, "automation", None)
    if instance is not None:
        return instance

    try:
        hresult = ctypes.windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        if hresult < 0 and hresult != _RPC_E_CHANGED_MODE:
            raise OSError(f"CoInitializeEx failed (0x{hresult & 0xFFFFFFFF:08x})")

        instance = c_void_p()
        ctypes.oledll.ole32.CoCreateInstance(
            byref(_GUID(_CLSID_CUIAutomation)),
            None,
            _CLSCTX_INPROC_SERVER,
            byref(_GUID(_IID_IUIAutomation)),
            byref(instance),
        )
    except Exception:  # noqa: BLE001
        # No UI Automation on this machine/session. URL capture reports
        # itself unavailable rather than guessing; it never fabricates.
        log.exception("UI Automation unavailable on this thread; URL capture disabled here")
        _local.failed = True
        return None

    _local.automation = instance
    return instance


def _is_plausible_url(candidate: str) -> bool:
    """Reject anything that is not actually a location.

    Chromium's omnibox is a plain edit control: while the user types it holds
    a half-finished search phrase, and this must not be recorded as a visited
    URL. Requiring a parseable host with a dot (or an explicit localhost)
    and no whitespace is enough to tell the two apart.
    """
    if not candidate or any(character.isspace() for character in candidate):
        return False
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    host = (parsed.hostname or "").strip()
    if not host:
        return False
    return "." in host or host == "localhost"


def _read_string_property(element: c_void_p, property_id: int) -> Optional[str]:
    """One string property off an element, or None. Never raises."""
    value = _VARIANT()
    try:
        if _vtable_call(
            element,
            _VT_IUIAUTOMATIONELEMENT_GET_CURRENT_PROPERTY_VALUE,
            ctypes.HRESULT,
            c_int,
            POINTER(_VARIANT),
        )(element, property_id, byref(value)) < 0:
            return None
        if value.vt != _VT_BSTR or not value.val:
            return None
        return ctypes.cast(value.val, c_wchar_p).value
    except Exception:  # noqa: BLE001
        return None
    finally:
        ctypes.windll.oleaut32.VariantClear(byref(value))


def read_element_name_by_class(hwnd: int, class_name: str) -> Optional[str]:
    """
    The accessible name of the first descendant of `hwnd` with `class_name`.

    Used to read a Chromium window's `BrowserRootView`, whose name is the
    *accessible* window title. That differs from the plain window title in one
    way that matters: the browser appends a parenthetical marker to it for an
    off-the-record window — "Wikipedia - Google Chrome (Incognito)" — and does
    not for an ordinary one.

    The lookup is by UI Automation class name, which is the browser's own C++
    view class (`BrowserRootView`, `EdgeAvatarToolbarButton`, ...). Those are
    identifiers, not user-facing text: they are identical in every locale and
    have been stable across Chromium releases for years, which is what makes
    this a structural reading of the window rather than a text search of it.

    :return: the name, or None when UI Automation is unavailable, the window
        has no such element (any non-Chromium browser), or the read failed.
        None means "could not determine", never "no".
    """
    if not SUPPORTED or not hwnd or not class_name:
        return None

    automation = _automation()
    if automation is None:
        return None

    element = c_void_p()
    condition = c_void_p()
    found = c_void_p()
    class_bstr = None
    try:
        if _vtable_call(
            automation,
            _VT_IUIAUTOMATION_ELEMENT_FROM_HANDLE,
            ctypes.HRESULT,
            c_void_p,
            POINTER(c_void_p),
        )(automation, hwnd, byref(element)) < 0 or not element:
            return None

        class_bstr = _oleaut32.SysAllocString(class_name)
        if not class_bstr:
            return None
        wanted = _VARIANT()
        wanted.vt = _VT_BSTR
        wanted.val = class_bstr
        if _vtable_call(
            automation,
            _VT_IUIAUTOMATION_CREATE_PROPERTY_CONDITION,
            ctypes.HRESULT,
            c_int,
            _VARIANT,
            POINTER(c_void_p),
        )(automation, _UIA_ClassNamePropertyId, wanted, byref(condition)) < 0:
            return None

        if _vtable_call(
            element,
            _VT_IUIAUTOMATIONELEMENT_FIND_FIRST,
            ctypes.HRESULT,
            c_int,
            c_void_p,
            POINTER(c_void_p),
        )(element, _TreeScope_Descendants, condition, byref(found)) < 0 or not found:
            return None

        return _read_string_property(found, _UIA_NamePropertyId)
    except Exception:  # noqa: BLE001
        log.debug("class-name element read failed for hwnd %s (%s)",
                  hwnd, class_name, exc_info=True)
        return None
    finally:
        _release(found)
        _release(condition)
        _release(element)
        if class_bstr:
            _oleaut32.SysFreeString(class_bstr)


def read_address_bar(hwnd: int) -> Optional[str]:
    """
    Return the URL currently shown in the address bar of `hwnd`, or None.

    None means "no URL could be read" -- an unsupported browser, a window
    with no address bar, a Firefox build with accessibility not enabled, or
    an omnibox holding a partly typed search. Callers must treat it as
    URL_UNAVAILABLE and record no URL, never a placeholder.
    """
    if not SUPPORTED or not hwnd:
        return None

    automation = _automation()
    if automation is None:
        return None

    element = c_void_p()
    condition = c_void_p()
    found = c_void_p()
    try:
        if _vtable_call(
            automation,
            _VT_IUIAUTOMATION_ELEMENT_FROM_HANDLE,
            ctypes.HRESULT,
            c_void_p,
            POINTER(c_void_p),
        )(automation, hwnd, byref(element)) < 0 or not element:
            return None

        control_type = _VARIANT()
        control_type.vt = _VT_I4
        control_type.val = _UIA_EditControlTypeId
        if _vtable_call(
            automation,
            _VT_IUIAUTOMATION_CREATE_PROPERTY_CONDITION,
            ctypes.HRESULT,
            c_int,
            _VARIANT,
            POINTER(c_void_p),
        )(automation, _UIA_ControlTypePropertyId, control_type, byref(condition)) < 0:
            return None

        if _vtable_call(
            element,
            _VT_IUIAUTOMATIONELEMENT_FIND_FIRST,
            ctypes.HRESULT,
            c_int,
            c_void_p,
            POINTER(c_void_p),
        )(element, _TreeScope_Descendants, condition, byref(found)) < 0 or not found:
            return None

        value = _VARIANT()
        if _vtable_call(
            found,
            _VT_IUIAUTOMATIONELEMENT_GET_CURRENT_PROPERTY_VALUE,
            ctypes.HRESULT,
            c_int,
            POINTER(_VARIANT),
        )(found, _UIA_ValueValuePropertyId, byref(value)) < 0:
            return None

        try:
            if value.vt != _VT_BSTR or not value.val:
                return None
            raw = ctypes.cast(value.val, c_wchar_p).value or ""
        finally:
            ctypes.windll.oleaut32.VariantClear(byref(value))

        raw = raw.strip()
        if not _is_plausible_url(raw):
            return None
        # Chromium hides the scheme for plain https pages, so the address bar
        # reads "chatgpt.com/c/..." rather than "https://chatgpt.com/c/...".
        # Restoring the implied scheme is not invention: the browser omitted
        # it precisely because it is https.
        return raw if "://" in raw else f"https://{raw}"
    except Exception:  # noqa: BLE001
        log.debug("address-bar read failed for hwnd %s", hwnd, exc_info=True)
        return None
    finally:
        _release(found)
        _release(condition)
        _release(element)
