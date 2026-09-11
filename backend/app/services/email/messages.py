"""The two emails this system sends, built from a stored payload.

Each builder takes the JSON context that was frozen onto the outbox row and
returns a finished `OutgoingEmail`. Nothing here touches the database, reads a
request, or decides *whether* to send — it renders, and it is therefore
testable on its own, which is what the template tests do.

Rendering from the stored payload rather than from live objects is what makes a
retry honest: the message a recipient eventually gets describes the event as it
was when it happened, even if the user has since been renamed or the feedback
row has changed status.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from markupsafe import Markup

from app.core.config import settings
from app.core.time_format import IST, to_ist
from app.services.email import assets
from app.services.email.provider import OutgoingEmail, assert_header_safe
from app.services.email.templates import (
    brand_html, detail_rows, paragraphs, render_page,
)

#: The longest a Subject header may be here. The column is String(255) and a
#: header that long is already being truncated by most clients; this keeps the
#: two consistent rather than letting a long name overflow the column.
SUBJECT_MAX_LENGTH = 255

#: Human labels for the six wire categories the desktop client submits. Kept
#: here rather than derived from the enum name so the email reads the way the
#: dialog's dropdown does — "Report a Problem", not "report_a_problem".
CATEGORY_LABELS = {
    "suggestion": "Suggestion",
    "report_a_problem": "Report a Problem",
    "general_feedback": "General Feedback",
    "need_help": "Need Help",
    "account_login_issue": "Account / Login Issue",
    "other": "Other",
}

#: What the welcome email says Monitra does. Four, because the feature list is
#: an orientation and not a manual.
WELCOME_FEATURES = (
    (
        "Time tracking",
        "Start a timer against the work you are doing and Monitra keeps the record for you.",
    ),
    (
        "Projects and tasks",
        "See what is assigned to you, and log your hours against the right piece of work.",
    ),
    (
        "Activity insight",
        "Monitra captures how active a tracked session was, so your time reflects real work.",
    ),
    (
        "Productivity visibility",
        "Clear daily and weekly summaries — for you, and for the people you report to.",
    ),
)


def category_label(category: str) -> str:
    """A category's display name, falling back to the wire value.

    An unknown value is shown as-is rather than replaced with "Other": if a
    seventh category is ever added, the notification should say which one it
    was, not quietly mislabel it.
    """
    return CATEGORY_LABELS.get(category, (category or "Feedback").replace("_", " ").title())


def clean_subject(value: str) -> str:
    """A Subject header that cannot carry a second header, bounded in length.

    Whitespace is collapsed *before* the header-safety check, not after, and
    the order is load-bearing. A Subject is a single line by definition, so a
    newline arriving inside an interpolated display name is whitespace to be
    folded — refusing it instead (which is what checking first did) made the
    notification for a legitimately saved piece of feedback permanently
    unrenderable, and it would have burned its six attempts and parked as
    failed. That is not the same thing as scrubbing invalid input and carrying
    on: the stored payload keeps exactly what the user typed, only this
    single-line rendering of it is folded, and anything that is genuinely a
    control character rather than whitespace is still refused below.
    """
    subject = " ".join((value or "").split())
    subject = assert_header_safe(subject, field_label="Subject")
    if len(subject) > SUBJECT_MAX_LENGTH:
        subject = subject[: SUBJECT_MAX_LENGTH - 1].rstrip() + "…"
    return subject


def _parse_timestamp(value: Any) -> datetime:
    """A stored ISO-8601 timestamp as an aware UTC datetime.

    The payload holds a string because it is JSON. A value that cannot be
    parsed falls back to "now" rather than raising: a notification must not
    become undeliverable because of how its timestamp was written.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _display_times(value: Any) -> tuple[str, str, str]:
    """(date, time, combined) for a timestamp, in the organisation's timezone.

    Asia/Kolkata via `core.time_format.IST` — the same zone every other
    user-facing time in this system is displayed in, so a feedback email and
    the dashboard agree about when something happened. The zone is named in the
    output, because a reader in another timezone otherwise has no way to tell.
    """
    local = to_ist(_parse_timestamp(value)) or datetime.now(IST)
    day = local.strftime("%d %B %Y")
    clock = f"{local.strftime('%I:%M %p').lstrip('0')} IST"
    return day, clock, f"{day}, {clock}"


def _frame_context(subject: str, preheader: str, footer_note: str) -> dict[str, Any]:
    """The values `base.html` needs, shared by both emails."""
    monitra = assets.monitra_logo()
    store_transform = assets.store_transform_logo()

    support = (settings.MONITRA_SUPPORT_EMAIL or "").strip()
    support_block = Markup("")
    if support:
        support_block = Markup(
            '<p style="margin:0;font-family:Helvetica,Arial,sans-serif;font-size:13px;'
            'line-height:20px;color:#6B7280;">Need a hand? Write to '
            '<a href="mailto:{email}" style="color:#2563EB;text-decoration:none;">{email}</a>.</p>'
        ).format(email=support)

    return {
        "subject": subject,
        "preheader": preheader,
        "footer_note": footer_note,
        "support_block": support_block,
        "year": datetime.now(IST).year,
        "store_transform_brand": brand_html(
            store_transform, fallback_text="Store Transform",
            fallback_color="#DC4B32", align="left",
        ),
        "monitra_brand": brand_html(
            monitra, fallback_text="Monitra", fallback_color="#2563EB", align="right",
        ),
        # Only logos that are actually delivered by Content-ID have an
        # attachment; in hosted-URL mode `inline` is None and nothing is
        # attached to the message.
        "_inline_images": tuple(
            logo.inline for logo in (store_transform, monitra) if logo and logo.inline
        ),
    }


# ----------------------------------------------------------------------
# Workflow 1 — welcome
# ----------------------------------------------------------------------

def welcome_subject() -> str:
    return clean_subject("Welcome to Monitra — Your Workforce Productivity Companion")


def _greeting(name: Optional[str]) -> str:
    """"Hi Priya," when a name is known, and a plain greeting when it is not.

    A first name only: the provider's `name` is a full display name, and
    "Hi Priya" reads like a person wrote it where "Hi Priya Raman" does not.
    """
    first = (name or "").strip().split(" ")[0] if (name or "").strip() else ""
    return f"Hi {first}," if first else "Hello,"


def build_welcome_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    """The one-time welcome, rendered for delivery."""
    subject = welcome_subject()
    frame = _frame_context(
        subject=subject,
        preheader="Your Monitra workspace is ready — here is what you can do with it.",
        footer_note=(
            "You are receiving this because a Monitra account was created for you. "
            "It is sent once, when the account is set up."
        ),
    )

    feature_rows = Markup("").join(
        Markup(
            '<tr>'
            '<td style="padding:10px 0;border-bottom:1px solid #E8ECF3;">'
            '<p style="margin:0 0 3px 0;font-family:Helvetica,Arial,sans-serif;font-size:15px;'
            'font-weight:700;color:#0F172A;">{title}</p>'
            '<p style="margin:0;font-family:Helvetica,Arial,sans-serif;font-size:14px;'
            'line-height:22px;color:#5B6576;">{body}</p>'
            '</td></tr>'
        ).format(title=title, body=body)
        for title, body in WELCOME_FEATURES
    )

    app_url = (settings.MONITRA_APP_URL or "").strip()
    cta_block = Markup("")
    if app_url.startswith("https://"):
        cta_block = Markup(
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" class="st-cta">'
            '<tr><td align="center" style="background-color:#2563EB;border-radius:8px;">'
            '<a href="{url}" style="display:inline-block;padding:13px 30px;'
            'font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:700;'
            'color:#FFFFFF;text-decoration:none;">Open Monitra</a>'
            '</td></tr></table>'
        ).format(url=app_url)

    html = render_page(
        "welcome.html",
        {**frame, "greeting": _greeting(payload.get("name")),
         "feature_rows": feature_rows, "cta_block": cta_block},
    )

    text_lines = [
        "Welcome to Monitra",
        "",
        f"{_greeting(payload.get('name'))} Your Monitra workspace is ready.",
        "",
        "Monitra is the staff management system your team uses to track working time,",
        "keep projects and tasks moving, and see how the working day actually went.",
        "",
        "What you can do with it:",
    ]
    text_lines += [f"  - {title}: {body}" for title, body in WELCOME_FEATURES]
    if app_url.startswith("https://"):
        text_lines += ["", f"Open Monitra: {app_url}"]
    if (settings.MONITRA_SUPPORT_EMAIL or "").strip():
        text_lines += ["", f"Need a hand? Write to {settings.MONITRA_SUPPORT_EMAIL.strip()}."]
    text_lines += ["", "Monitra — Staff Management System", "Store Transform"]

    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(text_lines),
        reply_to=(settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=frame["_inline_images"],
    )


# ----------------------------------------------------------------------
# Workflow 2 — feedback notification
# ----------------------------------------------------------------------

def feedback_subject(payload: dict[str, Any]) -> str:
    """"Monitra Feedback Received — Suggestion — Priya Raman".

    The submitter's name is part of the subject because these land in a shared
    mailbox, where "who" is the first thing a reader needs. It is passed
    through the same header-safety check as everything else, so a name
    containing a newline cannot append a header.
    """
    who = str(payload.get("user_name") or payload.get("username") or "").strip()
    label = category_label(str(payload.get("category") or ""))
    parts = ["Monitra Feedback Received", label] + ([who] if who else [])
    return clean_subject(" — ".join(parts))


def build_feedback_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    """The Admin/HR notification for one feedback submission."""
    subject = feedback_subject(payload)
    label = category_label(str(payload.get("category") or ""))
    who = str(payload.get("user_name") or payload.get("username") or "a team member").strip()
    day, clock, submitted = _display_times(payload.get("submitted_at"))
    feedback_id = payload.get("feedback_id")
    reference = f"FB-{feedback_id}" if feedback_id is not None else None
    message = str(payload.get("message") or "")

    frame = _frame_context(
        subject=subject,
        preheader=f"{label} from {who} — submitted {submitted}.",
        footer_note=(
            "This is an automated notification from Monitra. It was sent because "
            "feedback was submitted from the Monitra desktop application."
        ),
    )

    user_rows = detail_rows([
        ("Name", payload.get("user_name")),
        ("Username", payload.get("username")),
        ("Email", payload.get("user_email")),
        ("Role", payload.get("user_role")),
        ("User ID", payload.get("user_id")),
    ])
    submission_rows = detail_rows([
        ("Feedback ID", reference),
        ("Category", label),
        ("Submitted on", day),
        ("Submitted at", clock),
        ("Source", payload.get("source") or "Monitra Desktop"),
    ])

    html = render_page(
        "feedback.html",
        {
            **frame,
            "category_label": label,
            "summary": (
                f"{who} submitted feedback from the Monitra desktop application "
                f"on {submitted}. The full message is below."
            ),
            "user_rows": user_rows,
            "submission_rows": submission_rows,
            "message_html": paragraphs(message),
        },
    )

    text_lines = [
        "MONITRA FEEDBACK RECEIVED",
        "",
        f"{who} submitted feedback from the Monitra desktop application.",
        "",
        "Submitted by",
        f"  Name       {payload.get('user_name') or '-'}",
        f"  Username   {payload.get('username') or '-'}",
        f"  Email      {payload.get('user_email') or '-'}",
        f"  Role       {payload.get('user_role') or '-'}",
        f"  User ID    {payload.get('user_id') if payload.get('user_id') is not None else '-'}",
        "",
        "Submission",
        f"  Feedback   {reference or '-'}",
        f"  Category   {label}",
        f"  Submitted  {submitted}",
        f"  Source     {payload.get('source') or 'Monitra Desktop'}",
        "",
        "Message",
        "-" * 48,
        message,
        "-" * 48,
        "",
        "Automated notification — Monitra, Store Transform.",
    ]

    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(text_lines),
        reply_to=(settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=frame["_inline_images"],
    )


#: Maps a notification type to the builder that renders it. The outbox is
#: type-agnostic: adding a third email is a builder, an entry here and a
#: `dedupe_key`, with nothing in the delivery machinery changing.
BUILDERS = {
    "welcome": build_welcome_email,
    "feedback": build_feedback_email,
}
