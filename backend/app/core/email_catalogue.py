"""Every automated email Monitra sends, as one registry.

This is the single description of "which emails exist" that Email Management
(Settings -> Email Management) is built on: the page shows these rows, the CC
policy (`app/services/email_cc.py`) is keyed by `key`, and the schema validates
against it. A key is the outbox's `notification_type` -- the value that already
identifies an email on the way out -- so there is no second naming scheme to keep
in step with the first.

How an email gets in here
-------------------------
It is **registered where its builder is**, by `app/services/email/messages.py`:
one `_register(...)` call beside each builder carries the key, the builder and
the words the page shows. That one call is the whole of "making an email
manageable" -- there is no second list to append to, no number to pick (it is
the registration order), no seed row and no frontend change: the page reads
`GET /api/v1/email-cc`, which reads this registry.

An email that is never queued (the two that carry a bearer secret, a client's
invitation and sign-in link) is registered with no builder and `cc_allowed=False`.
Copying anyone on them would hand that person the recipient's access, so they are
never copied, whatever is configured (`OutgoingEmail.copy_exempt`), and the page
shows them as locked rather than hiding them.

`check_registry()` runs at application start-up (`app/main.py`) and in the tests
and refuses a registry that is inconsistent -- a queued email without its
metadata, a copyable email that is never queued, a configuration flag that does
not exist -- so a producer cannot ship half-registered and be silently
unmanageable.

Reading the registry
--------------------
`email_triggers()`, `trigger_keys()` and `cc_allowed_keys()` load the message
module on first use, so the registry is complete whichever module asks first.
"""
from dataclasses import dataclass
from importlib import import_module
from typing import Callable, Dict, Optional, Tuple

#: The module whose import registers every email. Named here, imported lazily:
#: `core` must not depend on `services` at import time, and a reader that asks
#: before the message module has been imported would otherwise see nothing.
_PRODUCER_MODULE = "app.services.email.messages"


@dataclass(frozen=True)
class EmailTrigger:
    #: The outbox `notification_type`. Stable: it is stored in the CC policy.
    key: str
    #: 1-based position in the list an administrator is shown: the order of registration.
    number: int
    name: str
    #: Who the email is addressed to.
    goes_to: str
    #: What makes it go out.
    trigger: str
    cc_allowed: bool = True
    #: Why an email is not copied, or anything else worth saying beside it.
    note: Optional[str] = None
    #: The `Settings` flag that switches the whole email off, when there is one.
    enabled_setting: Optional[str] = None
    #: Whether the email goes through the outbox (has a builder) or is sent at once.
    queued: bool = True


class EmailRegistryError(RuntimeError):
    """The registry is inconsistent; the application must not start on it."""


#: Why an email is not copied: the one note the two private-link emails share.
NO_CC_NOTE = "Carries a private link, so it is never copied to anyone else."

#: Most addresses the policy may hold. Every one is copied on every email it is
#: switched on for, so this is a guard against a runaway list, not a design limit.
MAX_CC_ADDRESSES = 50

_registry: Dict[str, EmailTrigger] = {}
_loaded = False


def register_email(
    key: str, name: str, goes_to: str, trigger: str, *,
    cc_allowed: bool = True, note: Optional[str] = None,
    enabled_setting: Optional[str] = None, queued: bool = True,
) -> EmailTrigger:
    """Record one email. Called once per email, by the module that builds it.

    Refuses a key registered twice (two producers claiming one name would make
    the policy ambiguous) and an email that may be copied but is never queued
    (the outbox is the only place the policy is applied, so such a row would
    promise a copy that never happens).
    """
    if not isinstance(key, str) or not key or key != key.strip():
        raise EmailRegistryError(f"An email key must be a non-blank string, not {key!r}.")
    if key in _registry:
        raise EmailRegistryError(f"The email {key!r} is registered twice.")
    if cc_allowed and not queued:
        raise EmailRegistryError(
            f"The email {key!r} may be copied but is not queued; only the outbox applies the CC policy."
        )
    entry = EmailTrigger(
        key, len(_registry) + 1, name, goes_to, trigger,
        cc_allowed=cc_allowed, note=note, enabled_setting=enabled_setting, queued=queued,
    )
    _registry[key] = entry
    return entry


def _ensure_loaded() -> None:
    global _loaded
    if not _loaded:
        _loaded = True
        import_module(_PRODUCER_MODULE)


def email_triggers() -> Tuple[EmailTrigger, ...]:
    """Every email, in the order the page shows them."""
    _ensure_loaded()
    return tuple(_registry.values())


def trigger_keys() -> Tuple[str, ...]:
    return tuple(t.key for t in email_triggers())


def cc_allowed_keys() -> Tuple[str, ...]:
    """The emails that may be copied."""
    return tuple(t.key for t in email_triggers() if t.cc_allowed)


def check_registry(builders: Dict[str, Callable], settings_object) -> Tuple[EmailTrigger, ...]:
    """Refuse an inconsistent registry. Run at start-up and by the tests.

    `builders` is the outbox's builder table: every queued email must be in it
    and everything in it must be registered, so a builder cannot exist that the
    page cannot show and the policy cannot reach. Every `enabled_setting` must be
    a real `Settings` flag, or the page would raise on the day it is shown.
    """
    triggers = email_triggers()
    if not triggers:
        raise EmailRegistryError("No email is registered.")
    queued = {t.key for t in triggers if t.queued}
    unregistered = sorted(set(builders) - queued)
    if unregistered:
        raise EmailRegistryError(
            f"Outbox builder(s) without a registration (not manageable, never copied): {unregistered}."
        )
    unbuilt = sorted(queued - set(builders))
    if unbuilt:
        raise EmailRegistryError(f"Registered as queued but no outbox builder: {unbuilt}.")
    for t in triggers:
        if t.enabled_setting and not hasattr(settings_object, t.enabled_setting):
            raise EmailRegistryError(
                f"The email {t.key!r} names a configuration flag that does not exist: {t.enabled_setting}."
            )
    if [t.number for t in triggers] != list(range(1, len(triggers) + 1)):
        raise EmailRegistryError("Email numbers are not the registration order.")
    return triggers
