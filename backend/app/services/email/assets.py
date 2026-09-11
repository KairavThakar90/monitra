"""The logos the email templates draw, and how a mail client gets at them.

A mail client cannot open a file path, and a link to `localhost` resolves on
the *recipient's* machine, not on the server that sent the message. So there
are exactly two workable strategies, and this module implements both behind one
interface:

* **Content-ID (the default).** The image bytes travel inside the message and
  the HTML refers to them as ``cid:monitra-logo``. Nothing has to be publicly
  hosted, the logos render in a client that blocks remote images, and mail read
  years later still looks right.
* **Hosted URL.** Set ``EMAIL_ASSET_BASE_URL`` to a public https:// prefix —
  this backend serves the same files at ``/email-assets/`` — and the templates
  reference ``<base>/monitra-logo.png`` instead. Smaller messages, at the cost
  of depending on a host and on the recipient allowing remote images.

An asset that is not on disk is simply absent: `logo()` returns ``None`` and
the template falls back to that brand's name as styled text. It does **not**
substitute a different image or draw an approximation of a logo — a wordmark
rendered in text is honest about being text, and a stand-in logo is not.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.services.email.provider import InlineImage

logger = logging.getLogger("uvicorn.error")

#: Where the email artwork lives. Deliberately inside the package rather than
#: alongside the frontend's copy: the backend is what sends the mail, and a
#: deployment that does not build the frontend must still be able to render it.
ASSET_DIR = Path(__file__).resolve().parents[2] / "assets" / "email"

#: The Monitra product logo. Sourced from `frontend/public/logo.png`, trimmed
#: and resized for email (560px wide, drawn at 280px so it stays sharp on a
#: high-density display), and flattened onto white because transparent PNGs
#: render unpredictably against the varying backgrounds mail clients apply.
MONITRA_LOGO = "monitra-logo.png"

#: The Store Transform company logo. Optional on disk — see the module
#: docstring. Drop the official artwork at
#: `backend/app/assets/email/store-transform-logo.png`, prepared the same way,
#: and both templates pick it up with no code change.
STORE_TRANSFORM_LOGO = "store-transform-logo.png"

#: Largest asset that may be embedded in a message. A logo is a few tens of
#: kilobytes; anything past this is a mistake, and mailing it to every user
#: would be an expensive one.
MAX_ASSET_BYTES = 200 * 1024


@dataclass(frozen=True)
class EmailLogo:
    """One logo, resolved for whichever image strategy is configured."""

    #: What goes in the ``src`` attribute: ``cid:...`` or an https:// URL.
    src: str
    alt: str
    #: Displayed width in CSS pixels. Always half the asset's pixel width, so
    #: the image is effectively 2x for high-density displays.
    width: int
    #: The attachment, when this logo is delivered by Content-ID. ``None`` in
    #: hosted-URL mode, where there is nothing to attach.
    inline: Optional[InlineImage] = None


@lru_cache(maxsize=8)
def _read_asset(filename: str) -> Optional[bytes]:
    """An asset's bytes, or None when it is not installed.

    Cached: these files never change while a process is running, and a
    serverless invocation should not re-read them per message.
    """
    path = ASSET_DIR / filename
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        logger.warning(
            "EMAIL_ASSET_MISSING: %s is not installed at %s; the template will "
            "render that brand as text instead of an image.", filename, ASSET_DIR,
        )
        return None
    except OSError:
        logger.warning("EMAIL_ASSET_UNREADABLE: %s", filename, exc_info=True)
        return None

    if len(data) > MAX_ASSET_BYTES:
        logger.warning(
            "EMAIL_ASSET_TOO_LARGE: %s is %d bytes, over the %d byte limit; "
            "it will not be embedded.", filename, len(data), MAX_ASSET_BYTES,
        )
        return None
    return data


def asset_exists(filename: str) -> bool:
    """Whether an asset is installed and usable."""
    return _read_asset(filename) is not None


def logo(filename: str, *, alt: str, width: int) -> Optional[EmailLogo]:
    """One logo ready for a template, or None when the artwork is not installed."""
    data = _read_asset(filename)
    if data is None:
        return None

    base_url = (settings.EMAIL_ASSET_BASE_URL or "").strip().rstrip("/")
    if base_url:
        if base_url.startswith(("http://localhost", "http://127.0.0.1", "https://localhost")):
            # A loopback URL resolves on the reader's machine. Refusing it here
            # is what stops a development value reaching a production mailbox
            # as a broken image.
            logger.warning(
                "EMAIL_ASSET_BASE_URL points at localhost, which no recipient can "
                "resolve; falling back to embedded images."
            )
        else:
            return EmailLogo(src=f"{base_url}/{filename}", alt=alt, width=width)

    cid = filename.rsplit(".", 1)[0]
    return EmailLogo(
        src=f"cid:{cid}",
        alt=alt,
        width=width,
        inline=InlineImage(cid=cid, filename=filename, content=data, subtype="png"),
    )


def monitra_logo() -> Optional[EmailLogo]:
    return logo(MONITRA_LOGO, alt="Monitra", width=170)


def store_transform_logo() -> Optional[EmailLogo]:
    return logo(STORE_TRANSFORM_LOGO, alt="Store Transform", width=140)


def asset_path(filename: str) -> Optional[Path]:
    """The on-disk path of an installed asset, for the static route to serve.

    Resolved and checked against ASSET_DIR so a crafted filename cannot walk
    out of the asset directory and serve something else.
    """
    if not filename or "/" in filename or "\\" in filename:
        return None
    candidate = (ASSET_DIR / filename).resolve()
    try:
        candidate.relative_to(ASSET_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None
