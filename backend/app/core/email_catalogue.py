"""Every automated email Monitra sends, as one list.

This is the single description of "which emails exist" that Email Management
(Settings -> Email Management) is built on: the page shows these rows, the CC
policy (`app/services/email_cc.py`) is keyed by `key`, and the schema validates
against it. A key is the outbox's `notification_type` -- the value that already
identifies an email on the way out -- so there is no second naming scheme to keep
in step with the first.

Adding an email means adding its row here, with the same key its builder is
registered under (`app/services/email/messages.BUILDERS`). A test compares the
two, so an email cannot ship without being listed, and a row here cannot name an
email that does not exist.

`cc_allowed` is False for the two emails that carry a bearer secret -- a client's
invitation and a client's sign-in link. Copying anyone on them would hand that
person the recipient's access, so they are never copied, whatever is configured
(`OutgoingEmail.copy_exempt`), and the page shows them as locked rather than hiding them.
"""
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class EmailTrigger:
    #: The outbox `notification_type`. Stable: it is stored in the CC policy.
    key: str
    #: 1-based position in the list an administrator is shown.
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


_NO_CC = "Carries a private link, so it is never copied to anyone else."

EMAIL_TRIGGERS: Tuple[EmailTrigger, ...] = (
    EmailTrigger(
        "welcome", 1, "Welcome", "New user",
        "Once, when the account is first created",
        enabled_setting="WELCOME_EMAIL_ENABLED",
    ),
    EmailTrigger(
        "feedback", 2, "Feedback received", "Admin/HR addresses from config",
        "A user submits feedback in the desktop app",
    ),
    EmailTrigger(
        "feedback_status", 3, "Feedback status (Working / Resolved)", "The person who submitted it",
        "Admin changes the feedback status",
    ),
    EmailTrigger(
        "release", 4, "New version available", "Every active user",
        "A desktop release is published",
        note="Off by default (RELEASE_EMAIL_ENABLED).",
        enabled_setting="RELEASE_EMAIL_ENABLED",
    ),
    EmailTrigger(
        "weekly_report", 5, "Weekly productivity report", "Each user",
        "Mondays, 9:00 AM IST",
    ),
    EmailTrigger(
        "monthly_report", 6, "Monthly productivity report", "Each user",
        "1st of the month, 9:00 AM IST",
    ),
    EmailTrigger(
        "monthly_project_summary", 7, "Monthly project summary",
        "Admins, project owners and leaders (each sees their own scope)",
        "1st of the month, 9:00 AM IST",
    ),
    EmailTrigger(
        "manual_time_request", 8, "Manual time request, for review",
        "Admins, HR, managers, and the leaders of the related project",
        "An employee submits a manual time request",
    ),
    EmailTrigger(
        "manual_time_receipt", 9, "Manual time request, receipt", "The requester",
        "Same moment as the review email",
    ),
    EmailTrigger(
        "manual_time_decision", 10, "Manual time decision (Approved / Rejected)", "The requester",
        "A reviewer decides the request",
    ),
    EmailTrigger(
        "project_budget_alert", 11, "Project hours alert",
        "Admins, owners and leaders of that project",
        "A fixed-hours project reaches 50%, 20% or 10% remaining, or runs out. Checked every 5 minutes",
    ),
    EmailTrigger(
        "client_invitation", 12, "Client invitation", "The client",
        "Admin invites a client",
        cc_allowed=False, note=_NO_CC,
    ),
    EmailTrigger(
        "client_login_link", 13, "Client sign-in link", "The client",
        "Client requests a link",
        cc_allowed=False, note=_NO_CC,
    ),
    EmailTrigger(
        "screenshot_notice", 14, "Screenshot notice, with the picture attached",
        "The employee the screenshot belongs to",
        "An admin, HR or leader sends a notice about it",
    ),
    EmailTrigger(
        "member_access", 15, "Access update (Excluded / Allowed, for Login or Add Task)",
        "The affected member",
        "An admin moves a Login or Add Task switch",
        enabled_setting="ACCESS_CHANGE_EMAIL_ENABLED",
    ),
)

TRIGGER_KEYS: Tuple[str, ...] = tuple(t.key for t in EMAIL_TRIGGERS)
#: The emails that may be copied.
CC_ALLOWED_KEYS: Tuple[str, ...] = tuple(t.key for t in EMAIL_TRIGGERS if t.cc_allowed)

#: Most addresses the policy may hold. Every one is copied on every email it is
#: switched on for, so this is a guard against a runaway list, not a design limit.
MAX_CC_ADDRESSES = 50
