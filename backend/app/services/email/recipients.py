"""Who an automated email goes to, resolved from configuration only.

No address in this system is written in source. Admin and HR are people, the
people change, and a deployment should be able to redirect its notifications
without a code change and a release. So the recipients come from environment
configuration, are validated the same way any other email field is validated,
and are de-duplicated — a deployment that puts the same address in both
FEEDBACK_ADMIN_EMAIL and FEEDBACK_HR_EMAIL sends one message, not two.

A misconfigured address is dropped and logged rather than sent to. The
alternative — letting it through to the mail server — turns one typo into a
delivery failure that retries for six hours and buries the addresses that were
correct.
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.core.config import settings
from app.services.email.provider import EmailAddressError, normalise_address

logger = logging.getLogger("uvicorn.error")


def parse_address_list(raw: str, *, source: str) -> list[str]:
    """Addresses out of one comma- or semicolon-separated configuration value.

    Order is preserved and duplicates are removed, so the first spelling of an
    address wins and the resulting list is stable across restarts.
    """
    resolved: list[str] = []
    for candidate in (raw or "").replace(";", ",").split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            address = normalise_address(candidate, field_label=source)
        except EmailAddressError as exc:
            # The setting's name, never the value: a malformed value can be a
            # partially-typed real address, and logs are read by more people
            # than configuration is.
            logger.warning("EMAIL_RECIPIENT_INVALID: %s contains an unusable address (%s)", source, exc)
            continue
        if address not in resolved:
            resolved.append(address)
    return resolved


def _merge(*groups: Iterable[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for address in group:
            if address not in merged:
                merged.append(address)
    return merged


def resolve_feedback_recipients() -> list[str]:
    """Every address a feedback notification should reach.

    Admin first, then HR, then any additional addresses — the order the email's
    To header will carry. An empty list is a valid answer and means this
    deployment has nobody configured to notify; the caller's job is then to
    skip the notification, not to invent a recipient.
    """
    return _merge(
        parse_address_list(settings.FEEDBACK_ADMIN_EMAIL, source="FEEDBACK_ADMIN_EMAIL"),
        parse_address_list(settings.FEEDBACK_HR_EMAIL, source="FEEDBACK_HR_EMAIL"),
        parse_address_list(
            settings.FEEDBACK_NOTIFICATION_EMAILS, source="FEEDBACK_NOTIFICATION_EMAILS",
        ),
    )


def resolve_user_recipient(email: str) -> list[str]:
    """One user's address, or an empty list when it is not usable.

    Used by the welcome email. A user row always has an email — the provider
    refuses to authenticate without one — but this still validates rather than
    assuming, because the value has travelled through an external system.
    """
    try:
        return [normalise_address(email, field_label="User email")]
    except EmailAddressError:
        logger.warning("EMAIL_RECIPIENT_INVALID: user email is not a usable address")
        return []


def describe_feedback_recipients() -> dict:
    """Recipient configuration, safe to serve on /health.

    Counts and which settings are populated — never the addresses themselves.
    Someone checking a deployment needs to know whether anyone is configured,
    and that question is answerable without publishing a mailing list.
    """
    return {
        "configured": bool(resolve_feedback_recipients()),
        "recipient_count": len(resolve_feedback_recipients()),
        "admin_set": bool(parse_address_list(settings.FEEDBACK_ADMIN_EMAIL, source="FEEDBACK_ADMIN_EMAIL")),
        "hr_set": bool(parse_address_list(settings.FEEDBACK_HR_EMAIL, source="FEEDBACK_HR_EMAIL")),
    }
