"""Transactional email: the provider abstraction, the templates and the outbox.

Import from here rather than from the submodules. The split inside the package
is an implementation detail, and keeping the surface narrow is what stops the
rest of the application growing a second opinion about how email is sent.

    from app.services.email import (
        EmailOutboxService, queue_feedback_notification, queue_welcome_email,
    )

The layering is one-directional: `workflows` decides *whether* to queue,
`outbox` decides *when* to deliver, `messages` + `templates` decide what the
message looks like, and `provider` is the only thing that speaks SMTP. Nothing
lower reaches back up.
"""
from app.services.email.outbox import (
    EmailOutboxService, deliver_in_background, retry_delay_seconds,
)
from app.services.email.provider import (
    EmailAddressError, EmailDeliveryError, EmailError, EmailNotConfiguredError,
    OutgoingEmail, describe_configuration, get_email_provider, unconfigured_reason,
)
from app.services.email.recipients import (
    describe_feedback_recipients, resolve_feedback_recipients,
)
from app.services.email.workflows import (
    feedback_dedupe_key, queue_feedback_notification, queue_welcome_email,
    welcome_dedupe_key,
)

__all__ = [
    "EmailAddressError",
    "EmailDeliveryError",
    "EmailError",
    "EmailNotConfiguredError",
    "EmailOutboxService",
    "OutgoingEmail",
    "deliver_in_background",
    "describe_configuration",
    "describe_feedback_recipients",
    "feedback_dedupe_key",
    "get_email_provider",
    "queue_feedback_notification",
    "queue_welcome_email",
    "resolve_feedback_recipients",
    "retry_delay_seconds",
    "unconfigured_reason",
    "welcome_dedupe_key",
]
