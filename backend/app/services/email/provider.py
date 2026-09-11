"""How a message actually leaves this system.

Everything above this module — the outbox, the two workflows, the templates —
speaks in terms of `OutgoingEmail` and `EmailProvider.send`. Nothing else in
the application imports `smtplib`, knows a port number, or has an opinion about
STARTTLS. Swapping SMTP for a provider API is adding a class here and a branch
in `get_email_provider`; no caller changes.

Two failure kinds are distinguished, and the distinction is what makes retrying
safe:

* `EmailNotConfiguredError` — this deployment cannot send at all. Not a
  delivery failure: the outbox skips the sweep entirely rather than burning an
  attempt, because retrying a missing SMTP_HOST five more times only turns a
  configuration mistake into a permanently failed notification.
* `EmailDeliveryError` — an attempt was made and it failed. Worth retrying,
  and it consumes an attempt.

Header injection is refused here rather than sanitised away. A recipient
address or a subject carrying a newline is not a value to be repaired — it is
someone trying to append headers to a message, and the honest answer is to
refuse the message.
"""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Optional, Sequence

from app.core.config import settings

logger = logging.getLogger("uvicorn.error")

#: Anything that can start a new header line. CR and LF are the attack; the
#: rest are control characters that have no business in an address or subject.
_HEADER_UNSAFE = re.compile(r"[\r\n\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Enough to reject a value that is plainly not an address before it reaches a
#: mail server. Deliberately the same shape as the validation catalogue's
#: EMAIL_PATTERN — see `app.core.validation.rules` — so a recipient accepted
#: here is one the rest of the system would also accept.
_ADDRESS = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class EmailError(Exception):
    """Base class for everything this module raises."""


class EmailNotConfiguredError(EmailError):
    """This deployment has no usable mail transport. Not a delivery failure."""


class EmailDeliveryError(EmailError):
    """An attempt was made and the message did not go out. Retryable."""


class EmailAddressError(EmailError):
    """A recipient, sender or subject that must not be put into a message."""


@dataclass(frozen=True)
class InlineImage:
    """An image attached to the message and referenced as ``cid:<cid>``.

    Used when no public asset URL is configured, which is the default. The
    bytes travel with the message, so the logos render in a client that blocks
    remote images — and there is no host to keep alive for mail sent months ago.
    """

    cid: str
    filename: str
    content: bytes
    subtype: str = "png"


@dataclass(frozen=True)
class OutgoingEmail:
    """One message, fully rendered and ready to hand to a transport."""

    to: Sequence[str]
    subject: str
    html: str
    text: str
    reply_to: Optional[str] = None
    inline_images: Sequence[InlineImage] = field(default_factory=tuple)


def assert_header_safe(value: str, *, field_label: str) -> str:
    """A header value with no way to inject a second header, or an error."""
    if not isinstance(value, str):
        raise EmailAddressError(f"{field_label} must be text.")
    if _HEADER_UNSAFE.search(value):
        raise EmailAddressError(f"{field_label} must not contain line breaks.")
    return value.strip()


def normalise_address(value: str, *, field_label: str = "Email address") -> str:
    """One recipient address, lower-cased, or an error.

    Rejects rather than repairs: an address that does not look like an address
    is a configuration mistake, and quietly dropping the malformed part of it
    would deliver someone's feedback to whatever remained.
    """
    address = assert_header_safe(value or "", field_label=field_label).lower()
    if not address:
        raise EmailAddressError(f"{field_label} must not be empty.")
    if not _ADDRESS.match(address):
        raise EmailAddressError(f"{field_label} is not a valid email address.")
    return address


def redact_error(error: BaseException, *, limit: int = 480) -> str:
    """A provider error reduced to something safe to store and log.

    An SMTP server's rejection can quote the envelope, and an exception raised
    while authenticating can carry the credential that failed. Anything that
    looks like a secret is removed before the text is written anywhere.
    """
    text = f"{type(error).__name__}: {error}"
    for secret in (settings.SMTP_PASSWORD, settings.SMTP_USERNAME, settings.EMAIL_DISPATCH_TOKEN):
        if secret and len(secret) > 3:
            text = text.replace(secret, "[redacted]")
    text = _HEADER_UNSAFE.sub(" ", text)
    return text[:limit]


class EmailProvider:
    """The transport contract. One method, and a name for logs."""

    name = "base"

    def send(self, message: OutgoingEmail) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def build_mime_message(message: OutgoingEmail) -> EmailMessage:
    """Turn an `OutgoingEmail` into a multipart/alternative MIME message.

    Plain text first and HTML second, which is the order that makes a client
    without HTML show the text part. Inline images are attached to the HTML
    part as ``multipart/related``, so a client that cannot resolve a Content-ID
    still has the readable text version rather than an empty frame.
    """
    from_address = normalise_address(
        settings.EMAIL_FROM_ADDRESS, field_label="EMAIL_FROM_ADDRESS"
    )
    from_name = assert_header_safe(
        settings.EMAIL_FROM_NAME or "Monitra", field_label="EMAIL_FROM_NAME"
    )
    recipients = [normalise_address(address, field_label="Recipient") for address in message.to]
    if not recipients:
        raise EmailAddressError("A message needs at least one recipient.")

    mime = EmailMessage()
    mime["From"] = formataddr((from_name, from_address))
    mime["To"] = ", ".join(recipients)
    mime["Subject"] = assert_header_safe(message.subject, field_label="Subject")
    mime["Message-ID"] = make_msgid(domain=from_address.rsplit("@", 1)[-1])
    # Tells well-behaved autoresponders not to reply to an automated message,
    # which is what stops a vacation responder bouncing back into the mailbox
    # the feedback notifications land in.
    mime["Auto-Submitted"] = "auto-generated"
    if message.reply_to:
        mime["Reply-To"] = normalise_address(message.reply_to, field_label="EMAIL_REPLY_TO")

    mime.set_content(message.text)
    mime.add_alternative(message.html, subtype="html")

    if message.inline_images:
        html_part = mime.get_payload()[-1]
        for image in message.inline_images:
            html_part.add_related(
                image.content,
                maintype="image",
                subtype=image.subtype,
                cid=f"<{image.cid}>",
                filename=image.filename,
            )
    return mime


class SmtpEmailProvider(EmailProvider):
    """Delivery over SMTP, which is what every transactional provider also speaks.

    TLS is not optional in any configuration that reaches a real mail server:
    either the socket is wrapped from the start (SMTP_USE_SSL, port 465) or the
    session is upgraded with STARTTLS before a username is offered. There is no
    code path here that sends a password over a plaintext connection.
    """

    name = "smtp"

    def send(self, message: OutgoingEmail) -> None:
        if not settings.SMTP_HOST:
            raise EmailNotConfiguredError("SMTP_HOST is not set.")
        if not settings.EMAIL_FROM_ADDRESS:
            raise EmailNotConfiguredError("EMAIL_FROM_ADDRESS is not set.")

        mime = build_mime_message(message)
        timeout = settings.SMTP_TIMEOUT_SECONDS
        host, port = settings.SMTP_HOST, settings.SMTP_PORT

        try:
            if settings.SMTP_USE_SSL:
                context = ssl.create_default_context()
                client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
            else:
                client = smtplib.SMTP(host, port, timeout=timeout)
            with client:
                client.ehlo()
                if settings.SMTP_USE_TLS and not settings.SMTP_USE_SSL:
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                if settings.SMTP_USERNAME:
                    if not (settings.SMTP_USE_TLS or settings.SMTP_USE_SSL):
                        # Refusing beats "helpfully" authenticating in the clear.
                        raise EmailNotConfiguredError(
                            "SMTP_USERNAME is set but TLS is disabled; refusing to "
                            "send credentials over an unencrypted connection."
                        )
                    client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                client.send_message(mime)
        except (EmailNotConfiguredError, EmailAddressError):
            raise
        except smtplib.SMTPException as exc:
            raise EmailDeliveryError(redact_error(exc)) from exc
        except OSError as exc:
            # Connection refused, DNS failure, timeout — all transient as far as
            # this system is concerned, and all worth another attempt later.
            raise EmailDeliveryError(redact_error(exc)) from exc


class ConsoleEmailProvider(EmailProvider):
    """Development transport: records that a message would have gone out.

    Logs the envelope only. The body is deliberately not logged — a feedback
    notification carries what an employee wrote, and a development log is not
    the place for it.
    """

    name = "console"

    def send(self, message: OutgoingEmail) -> None:
        build_mime_message(message)  # Same validation a real send would apply.
        logger.info(
            "EMAIL_CONSOLE_SEND: to=%s subject=%r html_bytes=%d images=%d",
            list(message.to), message.subject, len(message.html), len(message.inline_images),
        )


class DisabledEmailProvider(EmailProvider):
    """Queue, but never deliver. Every send raises, so nothing is marked sent."""

    name = "disabled"

    def send(self, message: OutgoingEmail) -> None:
        raise EmailNotConfiguredError("EMAIL_PROVIDER is 'disabled'.")


def get_email_provider() -> EmailProvider:
    """The transport this deployment is configured for.

    An unrecognised EMAIL_PROVIDER is treated as disabled rather than guessed
    at: a typo must not silently fall back to a transport nobody chose.
    """
    provider = (settings.EMAIL_PROVIDER or "").strip().lower()
    if provider == "smtp":
        return SmtpEmailProvider()
    if provider == "console":
        return ConsoleEmailProvider()
    if provider in ("", "disabled", "none", "off"):
        return DisabledEmailProvider()
    logger.warning(
        "EMAIL_PROVIDER_UNKNOWN: %r is not a supported provider; email is disabled",
        provider,
    )
    return DisabledEmailProvider()


def unconfigured_reason() -> Optional[str]:
    """Why this deployment cannot send, or None when it can.

    Non-sensitive by construction: it names the setting that is missing, never
    the value of one that is present.
    """
    provider = (settings.EMAIL_PROVIDER or "").strip().lower()
    if provider in ("", "disabled", "none", "off"):
        return "EMAIL_PROVIDER is not set to a delivering transport"
    if not settings.EMAIL_FROM_ADDRESS:
        return "EMAIL_FROM_ADDRESS is not set"
    if provider == "smtp" and not settings.SMTP_HOST:
        return "SMTP_HOST is not set"
    if provider not in ("smtp", "console"):
        return f"EMAIL_PROVIDER {provider!r} is not supported"
    return None


def describe_configuration() -> dict:
    """What this deployment's email setup is, safe to log and to serve on /health."""
    return {
        "configured": unconfigured_reason() is None,
        "provider": (settings.EMAIL_PROVIDER or "").strip().lower() or "disabled",
        "from_address_set": bool(settings.EMAIL_FROM_ADDRESS),
        "smtp_host_set": bool(settings.SMTP_HOST),
        "smtp_authenticated": bool(settings.SMTP_USERNAME),
        "asset_mode": "url" if settings.EMAIL_ASSET_BASE_URL else "cid",
    }
