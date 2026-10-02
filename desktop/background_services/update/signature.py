"""
Verifying that a downloaded installer is signed, and by whom, before it runs.

The SHA-256 the backend supplies proves the file is *the one the backend
named*. It says nothing about who built it, and the backend is the one party
that could name the wrong file. A code signature is the independent half:
Windows checks it against the machine's own trust store, so it holds even if the
release channel is compromised end to end.

Windows
-------
`WinVerifyTrust` with the generic-verify policy — the same call Explorer and
SmartScreen use — through `ctypes`, so there is no new dependency and nothing
to install. It runs on the task pool (see `UpdateService._download`), never on
the GUI thread, because revocation checking can wait on the network.

Every declaration here carries explicit `argtypes`/`restype`. That is not
decoration: DO_NOT_DO.md records a Win32 call through `ctypes` whose handle was
silently truncated to 32 bits for want of them, and it returned NULL on every
64-bit machine without raising anything.

After a successful verification the signer's certificate display name is read
back (`CertGetNameStringW`) so it can be matched against
`policy.WINDOWS_SIGNER_PINS`.

macOS
-----
A DMG's contents cannot be judged before it is mounted, and mounting is the
helper's job (`installer._write_macos_helper`). There the new bundle is checked
with `codesign --verify --deep --strict` and `spctl --assess` — which is what
Gatekeeper itself applies — and a bundle that fails stays unmounted-and-unused
with the old one still in place. This module therefore only *describes* the
macOS requirement; it cannot discharge it.

What this module will not do
----------------------------
It never treats "unsigned" as acceptable in production, and it never reports
success for a signature it could not evaluate. An unreadable result is a
refusal.
"""
from __future__ import annotations

import ctypes
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.logging_setup import get_logger

from . import policy
from .downloader import DownloadError

log = get_logger("updates.signature")

# HRESULTs worth naming in a log. Anything else is reported as hex.
_STATUS_NAMES = {
    0x00000000: "valid",
    0x800B0100: "the file is not signed",
    0x800B0004: "the signature's subject is not trusted",
    0x800B0109: "the certificate chains to a root that is not trusted",
    0x800B0101: "the certificate has expired",
    0x800B0110: "the certificate is not valid for code signing",
    0x80096010: "the file's contents do not match its signature (tampered or corrupt)",
    0x80096004: "the signature is invalid",
    0x800B010C: "the certificate has been revoked",
    0x80092013: "revocation could not be checked (offline)",
    0x800B010E: "revocation could not be checked",
    0x80092026: "blocked by a security setting",
}

#: Statuses meaning "I could not check revocation", as opposed to "it is
#: revoked". Retried once without the revocation check, because failing every
#: update on a machine behind a proxy that blocks OCSP would make the updater
#: unusable exactly where IT control is tightest. The signature itself, the
#: chain and the pinned publisher are still enforced on the retry.
_REVOCATION_UNAVAILABLE = (0x80092013, 0x800B010E)


@dataclass(frozen=True)
class SignatureResult:
    """The outcome of one verification. Never constructed by guessing."""

    trusted: bool
    status: int
    #: Certificate display name of the signer, when a trusted signature was read.
    signer: Optional[str] = None
    #: SHA-256 of the signing certificate, for the log (not a pin).
    cert_sha256: Optional[str] = None
    revocation_checked: bool = True

    @property
    def reason(self) -> str:
        return _STATUS_NAMES.get(self.status & 0xFFFFFFFF, f"status 0x{self.status & 0xFFFFFFFF:08X}")


# ── Win32 plumbing (only defined where it can be used) ───────────────────────

if sys.platform == "win32":
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
        ]

    class _WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pcwszFilePath", wintypes.LPCWSTR),
            ("hFile", ctypes.c_void_p),
            ("pgKnownSubject", ctypes.c_void_p),
        ]

    class _WINTRUST_DATA(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pPolicyCallbackData", ctypes.c_void_p),
            ("pSIPClientData", ctypes.c_void_p),
            ("dwUIChoice", wintypes.DWORD),
            ("fdwRevocationChecks", wintypes.DWORD),
            ("dwUnionChoice", wintypes.DWORD),
            ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)),   # the union; pointer-sized
            ("dwStateAction", wintypes.DWORD),
            ("hWVTStateData", ctypes.c_void_p),
            ("pwszURLReference", ctypes.c_void_p),
            ("dwProvFlags", wintypes.DWORD),
            ("dwUIContext", wintypes.DWORD),
            ("pSignatureSettings", ctypes.c_void_p),
        ]

    class _CRYPT_PROVIDER_CERT(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pCert", ctypes.c_void_p)]

    class _CERT_CONTEXT(ctypes.Structure):
        _fields_ = [
            ("dwCertEncodingType", wintypes.DWORD),
            ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
            ("cbCertEncoded", wintypes.DWORD),
            ("pCertInfo", ctypes.c_void_p),
            ("hCertStore", ctypes.c_void_p),
        ]

    _WINTRUST_ACTION_GENERIC_VERIFY_V2 = _GUID(
        0x00AAC56B, 0xCD44, 0x11D0,
        (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
    )

    _WTD_UI_NONE = 2
    _WTD_REVOKE_NONE = 0
    _WTD_REVOKE_WHOLECHAIN = 1
    _WTD_CHOICE_FILE = 1
    _WTD_STATEACTION_VERIFY = 1
    _WTD_STATEACTION_CLOSE = 2
    _WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT = 0x80
    _CERT_NAME_SIMPLE_DISPLAY_TYPE = 4

    _wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

    _wintrust.WinVerifyTrust.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.c_void_p,
    ]
    _wintrust.WinVerifyTrust.restype = ctypes.c_long
    _wintrust.WTHelperProvDataFromStateData.argtypes = [ctypes.c_void_p]
    _wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
    _wintrust.WTHelperGetProvSignerFromChain.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    _wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.c_void_p
    _wintrust.WTHelperGetProvCertFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _wintrust.WTHelperGetProvCertFromChain.restype = ctypes.POINTER(_CRYPT_PROVIDER_CERT)
    _crypt32.CertGetNameStringW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.LPWSTR, wintypes.DWORD,
    ]
    _crypt32.CertGetNameStringW.restype = wintypes.DWORD

    def _read_signer(state_handle) -> tuple[Optional[str], Optional[str]]:
        """(display name, certificate SHA-256) of the leaf signer, or (None, None)."""
        prov_data = _wintrust.WTHelperProvDataFromStateData(state_handle)
        if not prov_data:
            return None, None
        signer = _wintrust.WTHelperGetProvSignerFromChain(prov_data, 0, False, 0)
        if not signer:
            return None, None
        cert = _wintrust.WTHelperGetProvCertFromChain(signer, 0)
        if not cert or not cert.contents.pCert:
            return None, None
        context = ctypes.cast(cert.contents.pCert, ctypes.POINTER(_CERT_CONTEXT)).contents

        buffer = ctypes.create_unicode_buffer(512)
        length = _crypt32.CertGetNameStringW(
            cert.contents.pCert, _CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, None, buffer, 512
        )
        name = buffer.value if length > 1 else None

        digest = None
        if context.cbCertEncoded and context.pbCertEncoded:
            raw = ctypes.string_at(context.pbCertEncoded, context.cbCertEncoded)
            digest = hashlib.sha256(raw).hexdigest()
        return name, digest

    def verify_authenticode(path: Path, *, check_revocation: bool = True) -> SignatureResult:
        """Run WinVerifyTrust over `path`. Read-only; never raises for a bad file.

        Returns an untrusted result for any failure, including a path that does
        not exist: "I could not tell" is a refusal, never a pass.
        """
        file_info = _WINTRUST_FILE_INFO(
            ctypes.sizeof(_WINTRUST_FILE_INFO), str(path), None, None
        )
        data = _WINTRUST_DATA()
        data.cbStruct = ctypes.sizeof(_WINTRUST_DATA)
        data.dwUIChoice = _WTD_UI_NONE
        data.fdwRevocationChecks = (
            _WTD_REVOKE_WHOLECHAIN if check_revocation else _WTD_REVOKE_NONE
        )
        data.dwUnionChoice = _WTD_CHOICE_FILE
        data.pFile = ctypes.pointer(file_info)
        data.dwStateAction = _WTD_STATEACTION_VERIFY
        data.dwProvFlags = _WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT if check_revocation else 0

        action = _WINTRUST_ACTION_GENERIC_VERIFY_V2
        status = _wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
        signer = cert_hash = None
        try:
            if status == 0:
                signer, cert_hash = _read_signer(data.hWVTStateData)
        finally:
            data.dwStateAction = _WTD_STATEACTION_CLOSE
            _wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))

        return SignatureResult(
            trusted=(status == 0), status=int(status), signer=signer,
            cert_sha256=cert_hash, revocation_checked=check_revocation,
        )

else:  # pragma: no cover - exercised on macOS/Linux runners only

    def verify_authenticode(path: Path, *, check_revocation: bool = True) -> SignatureResult:
        return SignatureResult(trusted=False, status=0x800B0100)


# ── The policy applied to a result ───────────────────────────────────────────


def evaluate(result: SignatureResult) -> Optional[str]:
    """Why `result` does not satisfy this build's signing policy, or None.

    Split from `verify_installer` so the rule is testable without a signed file.
    """
    if not result.trusted:
        return f"signature check failed: {result.reason}"
    pins = [pin.lower() for pin in policy.WINDOWS_SIGNER_PINS]
    if pins and (result.signer or "").lower() not in pins:
        return (
            f"signed by '{result.signer}', which is not an approved publisher"
        )
    return None


def verify_installer(path: Path) -> None:
    """Refuse to let an installer through unless it is validly signed.

    :raises DownloadError: with a message fit to show a user, when the signature
        is missing, invalid, untrusted, or from a publisher that is not pinned.
        The caller discards the file.

    A no-op on macOS (see the module docstring) and when this build is allowed
    to run unsigned installers — which only a non-production build can be.
    """
    if sys.platform != "win32":
        return

    result = verify_authenticode(path, check_revocation=True)
    if not result.trusted and (result.status & 0xFFFFFFFF) in _REVOCATION_UNAVAILABLE:
        log.warning(
            "revocation could not be checked for the update installer (%s); "
            "re-checking the signature and chain without it", result.reason,
        )
        result = verify_authenticode(path, check_revocation=False)

    problem = evaluate(result)
    if problem is None:
        log.info(
            "installer signature verified: signer=%r cert_sha256=%s revocation_checked=%s",
            result.signer, result.cert_sha256, result.revocation_checked,
        )
        return

    if policy.unsigned_allowed():
        # Only reachable in a non-production build with the explicit test hook.
        log.error(
            "UPDATE_SIGNATURE_BYPASSED: %s -- continuing only because this is a "
            "non-production build with MONITRA_UPDATE_ALLOW_UNSIGNED set", problem,
        )
        return

    log.error("UPDATE_SIGNATURE_REFUSED: %s", problem)
    raise DownloadError(
        "The update's digital signature could not be verified, so it was not "
        "installed. Your current version of Monitra is unaffected. You can "
        "download the new version manually instead.",
        detail=problem,
    )
