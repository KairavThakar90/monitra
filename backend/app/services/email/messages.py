"""The emails this system sends, built from a stored payload.

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

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from markupsafe import Markup

from app.core.config import settings
from app.core.time_format import IST, to_ist
from app.models.email_notification import (
    TYPE_MEMBER_ACCESS, TYPE_MANUAL_TIME_DECISION, TYPE_MANUAL_TIME_RECEIPT, TYPE_MANUAL_TIME_REQUEST,
    TYPE_MONTHLY_PROJECT_SUMMARY, TYPE_MONTHLY_REPORT, TYPE_PROJECT_BUDGET_ALERT,
    TYPE_SCREENSHOT_NOTICE, TYPE_WEEKLY_REPORT,
)
from app.services.email import assets
from app.services.email.provider import (
    EmailAddressError, EmailDeliveryError, InlineImage, OutgoingEmail, assert_header_safe,
    normalise_address,
)
from app.services.email.templates import (
    brand_html, detail_rows, paragraphs, render_page,
)

logger = logging.getLogger(__name__)

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

#: How much of a feedback message the notification shows before it is cut off
#: and the reader is sent to the dashboard for the rest. The notification is a
#: prompt to go and read, not the archive — and a mailbox is a poor place to
#: keep somebody's full text anyway.
MESSAGE_PREVIEW_LENGTH = 50

#: Where the "Read full feedback" button points, appended to MONITRA_APP_URL.
#: `/admin/feedback` is the dashboard's Admin/HR feedback list — the same route
#: `FeedbackAdminRoute` guards — which is exactly who this notification goes to.
FEEDBACK_DASHBOARD_PATH = "/admin/feedback"

#: Where the release announcement's "Download the update" button points. The
#: web download page, not a direct artifact URL: the page picks the right build
#: for the visitor's platform, and one version is several artifacts.
DOWNLOAD_PAGE_PATH = "/download"

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

def welcome_cta_url() -> Optional[str]:
    """Where "Download Monitra" sends a newly welcomed user, or None.

    The public download page. Only built when MONITRA_APP_URL holds a real
    https:// URL — an http:// or localhost value resolves on the reader's
    machine, not this server, so there is no button rather than a broken one.
    """
    base = (settings.MONITRA_APP_URL or "").strip().rstrip("/")
    if not base.startswith("https://"):
        return None
    return f"{base}{DOWNLOAD_PAGE_PATH}"


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

    # The download page, not the app root. A welcome goes to somebody who has
    # just been given an account and does not have the desktop client yet, so
    # the next thing they need is the installer — and `/download` is public
    # precisely so a first-time user is not asked to sign in before they can
    # get the thing they sign in with. The app root would bounce them to
    # /login, which is a worse first step and, for a brand-new account, a
    # confusing one.
    app_url = welcome_cta_url()
    cta_block = Markup("")
    if app_url:
        cta_block = Markup(
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" class="st-cta">'
            '<tr><td align="center" style="background-color:#2563EB;border-radius:8px;">'
            '<a href="{url}" style="display:inline-block;padding:13px 30px;'
            'font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:700;'
            'color:#FFFFFF;text-decoration:none;">Download Monitra</a>'
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
    if app_url:
        text_lines += ["", f"Download Monitra: {app_url}"]
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
    message = str(payload.get("message") or "")

    frame = _frame_context(
        subject=subject,
        preheader=f"{label} from {who} — submitted {submitted}.",
        footer_note=(
            "This is an automated notification from Monitra. It was sent because "
            "feedback was submitted from the Monitra desktop application."
        ),
    )

    # Six fields, one table, in this order. Everything else the payload carries
    # -- username, role, user id, feedback id, source -- is deliberately not
    # shown: it is either duplicated elsewhere in the message or it is an
    # internal identifier that means nothing to the person reading. The payload
    # still holds all of it, so nothing has to be re-derived to bring a field
    # back, and `reference` remains the identifier used in logs.
    rows = detail_rows([
        ("Name", payload.get("user_name")),
        ("Email", payload.get("user_email")),
        ("Category", label),
        ("Submitted time", clock),
        ("Submission date", day),
    ])

    preview, truncated = preview_message(message)
    # The ellipsis is appended as markup, after the preview text has been
    # escaped, so it is a typographic mark and not something the user could
    # have typed to fake one.
    message_html = paragraphs(preview)
    if truncated:
        message_html = message_html + Markup(
            '<span style="color:#9AA3AF;"> …</span>'
        )

    html = render_page(
        "feedback.html",
        {
            **frame,
            "detail_rows": rows,
            "message_html": message_html,
            "cta_block": _feedback_cta(truncated),
        },
    )

    # The same six fields, in the same order. The two alternatives of one
    # message must not disagree about what was submitted.
    text_lines = [
        "MONITRA FEEDBACK RECEIVED",
        "",
        "A new feedback submission has been received from the Monitra desktop application.",
        "",
        f"  Name             {payload.get('user_name') or '-'}",
        f"  Email            {payload.get('user_email') or '-'}",
        f"  Category         {label}",
        f"  Submitted time   {clock}",
        f"  Submission date  {day}",
        "",
        "Message",
        "-" * 48,
        f"{preview}…" if truncated else preview,
        "-" * 48,
    ]
    if (dashboard_url := feedback_dashboard_url()):
        text_lines += [
            "",
            f"{'Read the full feedback' if truncated else 'Open in Monitra'}: {dashboard_url}",
        ]
    text_lines += ["", "Automated notification — Monitra, Store Transform."]

    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(text_lines),
        from_name=_sender_display_name(who),
        reply_to=_reply_to(payload),
        inline_images=frame["_inline_images"],
    )


def feedback_dashboard_url() -> Optional[str]:
    """The dashboard's feedback page, or None when no web address is configured.

    Built from MONITRA_APP_URL, and only when that holds a real https:// URL.
    A button in an email that goes to `http://localhost:5173` — the development
    default — resolves on the *reader's* machine, which is either nothing at all
    or, worse, something else of theirs. No URL means no button.
    """
    base = (settings.MONITRA_APP_URL or "").strip().rstrip("/")
    if not base.startswith("https://"):
        return None
    return f"{base}{FEEDBACK_DASHBOARD_PATH}"


def _feedback_cta(truncated: bool) -> Markup:
    """The "Read full feedback" button, when there is somewhere to send people.

    Rendered whenever a dashboard URL is configured, not only when the message
    was cut: a reader who wants to reply, mark it handled or see the rest of
    that person's history wants the link either way. The label changes so it
    does not promise "the full message" when the full message is already above.
    """
    url = feedback_dashboard_url()
    if url is None:
        return Markup("")
    label = "Read full feedback" if truncated else "Open in Monitra"
    return Markup(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'class="st-cta" style="margin:24px 0 0 0;">'
        '<tr><td align="center" style="background-color:#2563EB;border-radius:8px;">'
        '<a href="{url}" style="display:inline-block;padding:12px 26px;'
        'font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:700;'
        'color:#FFFFFF;text-decoration:none;">{label}</a>'
        '</td></tr></table>'
    ).format(url=url, label=label)


def preview_message(message: str, limit: int = MESSAGE_PREVIEW_LENGTH) -> tuple[str, bool]:
    """A message shortened for the notification. Returns (text, was_truncated).

    Whitespace is collapsed first, so a message whose first fifty characters are
    mostly newlines does not produce a preview that looks empty. The cut falls
    back to the last word boundary when there is one reasonably close, because
    "The timer resets when I resume fr…" reads worse than stopping a word
    earlier — but a fifty-character word is still cut at fifty rather than
    disappearing.

    Nothing here changes what is stored: the outbox payload keeps the full text
    the user wrote, and the dashboard shows all of it. This shortens one
    rendering of it.
    """
    collapsed = " ".join((message or "").split())
    if len(collapsed) <= limit:
        return collapsed, False

    cut = collapsed[:limit].rstrip()
    boundary = cut.rfind(" ")
    if boundary >= limit * 0.6:
        cut = cut[:boundary].rstrip()
    return cut, True


def _sender_display_name(who: str) -> str:
    """"Smit Prajapati via Monitra" — who the notification is *about*.

    This is the display name only. The address stays the one mailbox this
    deployment is authorised to send from; see `build_mime_message` for why
    putting the submitter's address in `From` is not an option. "via Monitra"
    is kept because the mail genuinely is from Monitra on that person's behalf,
    and a reader who cannot tell the difference is a reader who has been misled
    about where a message came from.
    """
    name = " ".join((who or "").split())
    if not name or name == "a team member":
        return settings.EMAIL_FROM_NAME or "Monitra"
    return f"{name} via Monitra"


def _reply_to(payload: dict[str, Any]) -> Optional[str]:
    """The submitter's address, so Reply goes to the person who wrote in.

    This is the part that actually answers "I want to reach the user who sent
    it": hitting Reply on the notification opens a message to them, not to the
    mailbox the notification was sent from. Falls back to EMAIL_REPLY_TO when
    the submitter has no usable address — a Reply-To that does not parse is
    worse than none, because a client will silently refuse to send.
    """
    candidate = str(payload.get("user_email") or "").strip()
    if candidate:
        try:
            return normalise_address(candidate, field_label="Submitter email")
        except EmailAddressError:
            pass
    return (settings.EMAIL_REPLY_TO or "").strip() or None


# ----------------------------------------------------------------------
# Workflow 3 — feedback status update, sent to the person who submitted it
# ----------------------------------------------------------------------

#: How each workflow state is presented to the person who submitted the
#: feedback. One entry per state the Admin buttons can produce, and the whole
#: visible difference between the two emails lives here: the headline, the
#: sentence under it, the accent colour of the status chip and the line the
#: subject carries.
#:
#: `in_progress` is titled "Working on it" because that is the word on the
#: button the administrator pressed and the word the employee will hear from
#: them — an email announcing "In Progress" for the same act reads like a
#: different system talking about a different thing.
FEEDBACK_STATUS_PRESENTATION: dict[str, dict[str, str]] = {
    "in_progress": {
        "label": "Working on it",
        "subject": "Monitra Feedback Update — We're Working on It",
        "preheader": "Your feedback has been reviewed and our team is working on it.",
        "heading": "We're working on your feedback",
        "lead": (
            "Thank you for taking the time to share your feedback with us. "
            "It has been reviewed, and our team is now working on it."
        ),
        "body": (
            "Your feedback is genuinely valuable — it is how we find the things "
            "worth fixing and the improvements worth making. We appreciate your "
            "patience while we work on this, and there is nothing further you "
            "need to do. If we need any more detail, we will get in touch."
        ),
        "accent": "#B45309",
        "chip_bg": "#FFFBEB",
        "chip_border": "#FDE68A",
    },
    "resolved": {
        "label": "Resolved",
        "subject": "Monitra Feedback Update — Resolved",
        "preheader": "The feedback you reported has now been resolved.",
        "heading": "Your feedback has been resolved",
        "lead": (
            "We're pleased to let you know that the feedback you reported has "
            "now been resolved."
        ),
        "body": (
            "Thank you for reporting it, and for helping us identify where "
            "Monitra could be better. Contributions like yours are what make "
            "the product better for everyone on the team. If you notice "
            "anything else, we would like to hear about it."
        ),
        "accent": "#047857",
        "chip_bg": "#ECFDF5",
        "chip_border": "#A7F3D0",
    },
}


def feedback_status_presentation(status: str) -> dict[str, str]:
    """How one status is worded and coloured.

    An unknown status raises rather than falling back to a neutral wording.
    This renders from a payload written by `queue_feedback_status_notification`,
    which can only queue a status the schema already restricted to these two —
    so an unknown value here means the two have drifted apart, and a
    reassuring-but-wrong email is a worse outcome than a notification that
    retries and is logged.
    """
    presentation = FEEDBACK_STATUS_PRESENTATION.get(status)
    if presentation is None:
        raise KeyError(f"No feedback status email is defined for {status!r}.")
    return presentation


def feedback_status_subject(payload: dict[str, Any]) -> str:
    """"Monitra Feedback Update — We're Working on It".

    Deliberately carries no name, no category and no fragment of the message.
    This one goes to a single person, who already knows who they are and what
    they wrote; a subject line naming their complaint is also the line that
    shows up on a lock screen in front of whoever is standing there.
    """
    return clean_subject(feedback_status_presentation(str(payload.get("status") or ""))["subject"])


def _status_chip(presentation: dict[str, str]) -> Markup:
    """The coloured "Working on it" / "Resolved" marker."""
    return Markup(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:0 0 22px 0;"><tr>'
        '<td style="padding:7px 14px;background-color:{bg};border:1px solid {border};'
        'border-radius:999px;font-family:Helvetica,Arial,sans-serif;font-size:12px;'
        'font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:{accent};">'
        '{label}</td></tr></table>'
    ).format(
        bg=presentation["chip_bg"],
        border=presentation["chip_border"],
        accent=presentation["accent"],
        label=presentation["label"],
    )


def build_feedback_status_email(
    payload: dict[str, Any], recipients: list[str]
) -> OutgoingEmail:
    """The Working / Resolved update, addressed to the submitter."""
    status = str(payload.get("status") or "")
    presentation = feedback_status_presentation(status)
    subject = feedback_status_subject(payload)
    label = category_label(str(payload.get("category") or ""))
    day, _clock, _submitted = _display_times(payload.get("submitted_at"))

    frame = _frame_context(
        subject=subject,
        preheader=presentation["preheader"],
        footer_note=(
            "You are receiving this because you submitted feedback from the "
            "Monitra desktop application. It is sent once per status update."
        ),
    )

    # Three fields: what they sent, where it now stands, and when they sent it
    # — enough for someone with several submissions open to tell which one this
    # is about. No feedback id, no internal reference, no administrator's name:
    # an identifier means nothing to the reader, and the other two are ours
    # rather than theirs.
    rows = detail_rows([
        ("Category", label),
        ("Status", presentation["label"]),
        ("Submitted", day),
    ])

    html = render_page(
        "feedback_status.html",
        {
            **frame,
            "greeting": _greeting(payload.get("name")),
            "status_chip": _status_chip(presentation),
            "heading": presentation["heading"],
            "lead": presentation["lead"],
            "body": presentation["body"],
            "detail_rows": rows,
        },
    )

    # The same words and the same three fields. A reader on a plain-text client
    # must not get a different account of what happened.
    text_lines = [
        presentation["heading"].upper(),
        "",
        _greeting(payload.get("name")),
        "",
        presentation["lead"],
        "",
        presentation["body"],
        "",
        f"  Category   {label}",
        f"  Status     {presentation['label']}",
        f"  Submitted  {day}",
    ]
    if (support := (settings.MONITRA_SUPPORT_EMAIL or "").strip()):
        text_lines += ["", f"Need a hand? Write to {support}."]
    text_lines += [
        "",
        "Thank you for helping us improve Monitra.",
        "",
        "Monitra — Staff Management System",
        "Store Transform",
    ]

    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(text_lines),
        reply_to=(settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=frame["_inline_images"],
    )


# ----------------------------------------------------------------------
# Workflow 4 — new desktop version available
# ----------------------------------------------------------------------

def release_subject(payload: dict[str, Any]) -> str:
    version = str(payload.get("version") or "").strip()
    subject = (
        f"Monitra {version} is available — what's new" if version
        else "A new version of Monitra is available"
    )
    # A rehearsal (RELEASE_EMAIL_TEST_RECIPIENTS) says so in the subject. The
    # subject is rebuilt from the payload at delivery, so the marker has to
    # live here and not only on the queued row.
    if payload.get("test"):
        subject = f"[TEST] {subject}"
    return clean_subject(subject)


def download_page_url() -> Optional[str]:
    """The web download page, or None when no web address is configured."""
    base = (settings.MONITRA_APP_URL or "").strip().rstrip("/")
    if not base.startswith("https://"):
        return None
    return f"{base}{DOWNLOAD_PAGE_PATH}"


def render_release_notes(notes: str) -> Markup:
    """Whatever the release author wrote, as readable HTML.

    A light structural read of plain text, and nothing more:

    * a line ending in ``:`` with nothing after it is a heading, which is how
      "New features:" and "Bug fixes:" become sections;
    * a line starting ``-``, ``*`` or ``•`` is a bullet;
    * anything else is a paragraph.

    It does **not** invent sections, classify anything as a feature or a fix,
    or summarise. What the release names is what the email says: a notification
    claiming a bug was fixed when the notes never said so is fabricated
    release information, and people make upgrade decisions on it.

    Every line is escaped before any tag is added, so release notes are text
    even though an administrator wrote them.
    """
    lines = [line.rstrip() for line in (notes or "").splitlines()]
    blocks: list[Markup] = []
    bullets: list[Markup] = []

    def flush() -> None:
        if bullets:
            blocks.append(
                Markup('<ul style="margin:0 0 16px 0;padding-left:20px;">{}</ul>')
                .format(Markup("").join(bullets))
            )
            bullets.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.endswith(":") and len(stripped) <= 60:
            flush()
            blocks.append(
                Markup(
                    '<p style="margin:0 0 8px 0;font-family:Helvetica,Arial,sans-serif;'
                    'font-size:12px;font-weight:700;letter-spacing:0.08em;'
                    'text-transform:uppercase;color:#9AA3AF;">{}</p>'
                ).format(stripped.rstrip(":"))
            )
            continue
        if stripped[0] in "-*•":
            bullets.append(
                Markup(
                    '<li style="margin:0 0 7px 0;font-family:Helvetica,Arial,sans-serif;'
                    'font-size:15px;line-height:24px;color:#374151;">{}</li>'
                ).format(stripped[1:].strip())
            )
            continue
        flush()
        blocks.append(
            Markup(
                '<p style="margin:0 0 14px 0;font-family:Helvetica,Arial,sans-serif;'
                'font-size:15px;line-height:25px;color:#374151;">{}</p>'
            ).format(stripped)
        )

    flush()
    return Markup("").join(blocks)


def build_release_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    """The "a new version is available" announcement, for one user."""
    version = str(payload.get("version") or "").strip()
    subject = release_subject(payload)
    notes = str(payload.get("release_notes") or "").strip()
    notes_url = str(payload.get("release_notes_url") or "").strip()

    frame = _frame_context(
        subject=subject,
        preheader=(
            f"Monitra {version} is ready to install." if version
            else "A new version of Monitra is ready to install."
        ),
        footer_note=(
            "You are receiving this because you use Monitra. It is sent once "
            "per release."
        ),
    )

    download_url = download_page_url()
    cta_block = Markup("")
    if download_url:
        cta_block = Markup(
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            'class="st-cta" style="margin:4px 0 0 0;">'
            '<tr><td align="center" style="background-color:#2563EB;border-radius:8px;">'
            '<a href="{url}" style="display:inline-block;padding:13px 30px;'
            'font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:700;'
            'color:#FFFFFF;text-decoration:none;">Download the update</a>'
            '</td></tr></table>'
        ).format(url=download_url)

    notes_link = Markup("")
    if notes_url.startswith("https://"):
        notes_link = Markup(
            '<p style="margin:16px 0 0 0;font-family:Helvetica,Arial,sans-serif;'
            'font-size:14px;line-height:22px;">'
            '<a href="{url}" style="color:#2563EB;text-decoration:none;">'
            'Read the full release notes</a></p>'
        ).format(url=notes_url)

    # No notes is an honest empty state, not an invented changelog. The email
    # still tells the user a new version exists and where to get it.
    notes_html = render_release_notes(notes) if notes else Markup(
        '<p style="margin:0 0 14px 0;font-family:Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:25px;color:#6B7280;">'
        'Release notes for this version have not been published yet.</p>'
    )

    html = render_page(
        "release.html",
        {
            **frame,
            "version_heading": f"Monitra {version}" if version else "A new version of Monitra",
            "notes_html": notes_html,
            "notes_link": notes_link,
            "cta_block": cta_block,
        },
    )

    text_lines = [
        f"MONITRA {version} IS AVAILABLE" if version else "A NEW VERSION OF MONITRA IS AVAILABLE",
        "",
        "What's changed",
        "-" * 48,
        notes or "Release notes for this version have not been published yet.",
        "-" * 48,
    ]
    if download_url:
        text_lines += ["", f"Download the update: {download_url}"]
    if notes_url.startswith("https://"):
        text_lines += [f"Full release notes: {notes_url}"]
    text_lines += ["", "Monitra — Staff Management System", "Store Transform"]

    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(text_lines),
        reply_to=(settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=frame["_inline_images"],
        # A rehearsal reaches its own test list only -- see RELEASE_EMAIL_TEST_RECIPIENTS.
        copy_exempt=bool(payload.get("test")),
    )


# ----------------------------------------------------------------------
# Workflow 6 — client invitation
# ----------------------------------------------------------------------

def client_invitation_subject() -> str:
    return clean_subject("You're Invited to Monitra")


def _client_invitation_urls(token: str) -> tuple[str, str]:
    """The direct backend GET endpoints the two buttons point at.

    Deliberately built from `API_BASE_URL`, not `MONITRA_APP_URL`: these links
    are meant to be opened once, unauthenticated, and hit this service
    directly -- the backend performs the approve/reject and redirects into the
    web client itself. See `app/api/clients.py`.
    """
    base = (settings.API_BASE_URL or "").strip().rstrip("/")
    if not base:
        # Without a base these render as host-less links ("http:///clients/…"
        # once a mail client absolutises them), which is exactly how a
        # production invitation shipped with broken buttons on 2026-09-29.
        # The email still sends -- the token is valid and support can hand
        # the client a working link -- but the misconfiguration is shouted.
        logger.error(
            "CLIENT_INVITATION_LINKS_UNCONFIGURED: API_BASE_URL is not set; "
            "this invitation's Approve/Reject buttons will be broken relative links"
        )
    return f"{base}/clients/invitations/{token}/approve", f"{base}/clients/invitations/{token}/reject"


def build_client_invitation_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    """The invitation, with its Approve/Reject buttons, for one client."""
    subject = client_invitation_subject()
    project_names = [str(name) for name in (payload.get("project_names") or [])]

    frame = _frame_context(
        subject=subject,
        preheader="You have been invited to view your project information in Monitra.",
        footer_note=(
            "You are receiving this because an administrator invited you to a "
            "Monitra client account. It is sent once per invitation."
        ),
    )

    project_rows = (
        Markup("<ul style=\"margin:0;padding-left:18px;\">{}</ul>").format(
            Markup("").join(
                Markup(
                    '<li style="font-family:Helvetica,Arial,sans-serif;font-size:14px;'
                    'line-height:24px;color:#374151;">{}</li>'
                ).format(name)
                for name in project_names
            )
        )
        if project_names
        else Markup(
            '<p style="margin:0;font-family:Helvetica,Arial,sans-serif;font-size:14px;'
            'color:#9AA3AF;">No projects have been shared yet.</p>'
        )
    )

    approve_url, reject_url = _client_invitation_urls(str(payload.get("token") or ""))

    html = render_page(
        "client_invitation.html",
        {**frame, "project_rows": project_rows, "approve_url": approve_url, "reject_url": reject_url},
    )

    text_lines = [
        "YOU'RE INVITED TO MONITRA",
        "",
        "You have been invited to access your project information in Monitra.",
        "",
        "Projects shared with you:",
    ]
    text_lines += [f"  - {name}" for name in project_names] if project_names else ["  (none yet)"]
    text_lines += [
        "",
        "Your login credential is your email address. There is no password to set.",
        "",
        f"Approve invitation: {approve_url}",
        f"Not needed / reject: {reject_url}",
        "",
        "Monitra — Staff Management System",
        "Store Transform",
    ]

    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(text_lines),
        reply_to=(settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=frame["_inline_images"],
        # Carries Approve/Reject links: copying anyone would let them act as the client.
        copy_exempt=True,
    )


# ----------------------------------------------------------------------
# Workflow 7 — client passwordless login link
# ----------------------------------------------------------------------

def client_login_link_subject() -> str:
    return clean_subject("Your Monitra sign-in link")


def _client_login_url(handoff_token: str) -> str:
    """Where the button sends the client -- the web client's `?token=`
    handoff consumption, the same mechanism the desktop-to-web handoff and
    the invitation Approve link both use."""
    base = (settings.MONITRA_APP_URL or "").strip().rstrip("/")
    return f"{base}/?token={handoff_token}"


def build_client_login_link_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    subject = client_login_link_subject()
    frame = _frame_context(
        subject=subject,
        preheader="Use this link to sign in to your Monitra client account.",
        footer_note="You are receiving this because you requested a sign-in link for Monitra.",
    )
    login_url = _client_login_url(str(payload.get("handoff_token") or ""))

    html = render_page("client_login_link.html", {**frame, "login_url": login_url})

    text_lines = [
        "YOUR MONITRA SIGN-IN LINK",
        "",
        "Use this link to sign in to your Monitra client account. It can only be "
        "used once and expires shortly.",
        "",
        login_url,
        "",
        "If you did not request this, you can safely ignore this email.",
        "",
        "Monitra — Staff Management System",
        "Store Transform",
    ]

    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(text_lines),
        reply_to=(settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=frame["_inline_images"],
        # A one-time sign-in link: anyone copied could use it before the client does.
        copy_exempt=True,
    )


#: Maps a notification type to the builder that renders it. The outbox is
#: type-agnostic: adding a third email is a builder, an entry here and a
#: `dedupe_key`, with nothing in the delivery machinery changing.
#:
#: `build_client_invitation_email` and `build_client_login_link_email` are
#: deliberately **not** registered here. Both render a bearer secret into the
#: message (an invitation token, a login handoff token), and the outbox's
#: payload is a durable, plaintext row in `email_notifications` -- see the
#: "never a token, a password or a session identifier" rule on that model.
#: `workflows.queue_client_invitation_email`/`queue_client_login_link_email`
#: call these builders directly and send immediately instead of going through
#: `EmailOutboxService.enqueue`, so the secret is never persisted.
BUILDERS = {
    "welcome": build_welcome_email,
    "feedback": build_feedback_email,
    "feedback_status": build_feedback_status_email,
    "release": build_release_email,
}


# ----------------------------------------------------------------------
# Workflow 5 — weekly productivity report
# ----------------------------------------------------------------------

#: The two Reports screens, and which permission opens which. Both route
#: guards *redirect* rather than refuse, and an administrator sent to the
#: member route is bounced to `/dashboard` — losing the date range on the way
#: — so the button has to be pointed at the right one from the start.
WEEKLY_REPORT_MEMBER_PATH = "/member/reports/projects"
WEEKLY_REPORT_ADMIN_PATH = "/dashboard/reports/projects"


def format_duration(total_seconds: Any) -> str:
    """A tracked duration as "38h 42m".

    Whole minutes, because a weekly total reported to the second invites a
    reader to reconcile it against a dashboard that rounds differently. The
    exact figure is in the database and on the Reports page; this is the
    summary's rendering of it.

    Not `format_hms`: "38:42:00" is the right shape for a timesheet row and
    the wrong one for a sentence. Both describe the same stored seconds.
    """
    try:
        seconds = max(0, int(total_seconds or 0))
    except (TypeError, ValueError):
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    return f"{hours}h {remainder // 60}m"


def format_activity(value: Any) -> str:
    """An activity percentage as "78%", or "No activity" when none was sampled.

    ``None`` means *not measured* — a week of approved manual entries carries
    no activity samples at all — and it is reported as such rather than as 0%,
    which would read as "you were idle all week" about somebody who worked.
    """
    if value is None:
        return "No activity"
    try:
        return f"{round(float(value))}%"
    except (TypeError, ValueError):
        return "No activity"


def weekly_report_subject(payload: dict[str, Any]) -> str:
    """"Your Monitra Weekly Report — 08 Sep–14 Sep".

    Carries the period because a reader with four of these in a folder needs
    to tell them apart, and deliberately carries no figure: an hours total on
    a lock screen is somebody's performance shown to whoever is standing there.
    """
    period = str(payload.get("period_short") or "").strip()
    return clean_subject(
        f"Your Monitra Weekly Report — {period}" if period
        else "Your Monitra Weekly Report"
    )


def weekly_report_dashboard_url(payload: dict[str, Any]) -> Optional[str]:
    """Where "View Detailed Report" sends this reader, or None.

    An existing route with existing query parameters — the Reports page reads
    ``?start=``/``?end=`` already, which is how the dashboard hands its picked
    span to it — so the button opens the very week the email describes rather
    than the page's default last-seven-days. No route is invented here.

    Built only when MONITRA_APP_URL holds a real https:// URL, for the reason
    every other call to action in this module is: `http://localhost:5173`
    resolves on the *reader's* machine, and a button that goes nowhere is
    worse than no button.
    """
    return _report_dashboard_url(payload, start_key="week_start", end_key="week_end")


def _report_dashboard_url(payload: dict[str, Any], *, start_key: str, end_key: str) -> Optional[str]:
    """The Reports page filtered to the period under `start_key`/`end_key`,
    or None when MONITRA_APP_URL is not a real https:// URL."""
    base = (settings.MONITRA_APP_URL or "").strip().rstrip("/")
    if not base.startswith("https://"):
        return None

    path = (
        WEEKLY_REPORT_ADMIN_PATH if payload.get("can_view_all_time")
        else WEEKLY_REPORT_MEMBER_PATH
    )
    start = str(payload.get(start_key) or "").strip()
    end = str(payload.get(end_key) or "").strip()
    if not (start and end):
        return f"{base}{path}"
    return f"{base}{path}?{urlencode({'start': start, 'end': end})}"


def _rows_table(rows: Markup) -> Markup:
    """The one table the weekly email is. Empty markup for an empty body."""
    if not rows:
        return Markup("")
    return Markup(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="margin:0;border:1px solid #EDF0F5;border-radius:10px;">{rows}</table>'
    ).format(rows=rows)


def _weekly_summary_table(payload: dict[str, Any]) -> Markup:
    """Every figure the weekly email reports, as a single label/value table.

    One table rather than a card grid and four sections: a weekly summary is
    read down a column in ten seconds, and splitting eight numbers across four
    headed blocks made the message longer without making any of it clearer.

    Rows are omitted rather than zero-filled when the underlying data cannot
    support them — `detail_rows` drops a `None` value entirely, so an absent
    measurement leaves no row behind instead of printing a figure that would
    read as one.

    Projects are reported as a count and not as a list. The detail belongs to
    the dashboard the button goes to; repeating a slice of it here made the
    summary longer without answering anything the count does not.
    """
    idle_seconds = payload.get("idle_seconds")
    highest = payload.get("highest_activity_day") or {}
    lowest = payload.get("lowest_activity_day") or {}

    return _rows_table(detail_rows([
        ("Total tracked time", format_duration(payload.get("total_seconds"))),
        # Active and idle appear only when idle time was actually recorded. A
        # flat "Idle time 0h 0m" cannot distinguish "you were never idle" from
        # "no idle period was ever captured on your machine", and the second is
        # not something to state as a measurement.
        ("Active time", format_duration(payload.get("active_seconds")) if idle_seconds else None),
        ("Idle time", format_duration(idle_seconds) if idle_seconds else None),
        ("Average activity", format_activity(payload.get("average_activity"))),
        ("Highest activity day", highest.get("name")),
        ("Lowest activity day", lowest.get("name")),
        ("Projects", str(int(payload.get("project_count") or 0))),
    ]))


def _weekly_cta(payload: dict[str, Any]) -> Markup:
    url = weekly_report_dashboard_url(payload)
    if url is None:
        return Markup("")
    return Markup(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'class="st-cta" style="margin:30px 0 0 0;">'
        '<tr><td align="center" style="background-color:#2563EB;border-radius:8px;">'
        '<a href="{url}" style="display:inline-block;padding:13px 30px;'
        'font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:700;'
        'color:#FFFFFF;text-decoration:none;">View Detailed Report</a>'
        '</td></tr></table>'
    ).format(url=url)


def _weekly_text_lines(payload: dict[str, Any], has_activity: bool) -> list[str]:
    """The plain-text alternative. The same figures, in the same order.

    A reader on a text-only client must not be given a different account of
    their week from the one the HTML shows.
    """
    period = str(payload.get("period_label") or "")
    lines = [
        "YOUR MONITRA WEEKLY PRODUCTIVITY REPORT",
        "",
        _greeting(payload.get("name")),
        "",
        f"Week of {period}" if period else "",
        "",
    ]

    if not has_activity:
        lines += [
            f"You had no tracked activity during {period}." if period
            else "You had no tracked activity last week.",
            "",
        ]

    # The same rows as the HTML table, in the same order. A reader on a
    # text-only client must not get a different account of their week.
    lines += ["-" * 48, f"  Total tracked time    {format_duration(payload.get('total_seconds'))}"]
    if payload.get("idle_seconds"):
        lines += [
            f"  Active time           {format_duration(payload.get('active_seconds'))}",
            f"  Idle time             {format_duration(payload.get('idle_seconds'))}",
        ]
    lines.append(f"  Average activity      {format_activity(payload.get('average_activity'))}")
    for label, key in (
        ("Highest activity day", "highest_activity_day"),
        ("Lowest activity day", "lowest_activity_day"),
    ):
        if (name := (payload.get(key) or {}).get("name")):
            lines.append(f"  {label}{' ' * (22 - len(label))}{name}")
    lines.append(f"  Projects              {int(payload.get('project_count') or 0)}")
    lines.append("-" * 48)

    if not has_activity:
        lines += [
            "",
            "Open Monitra and start a timer to begin tracking your work this week.",
        ]

    if (url := weekly_report_dashboard_url(payload)):
        lines += ["", f"View your detailed report: {url}"]
    if (support := (settings.MONITRA_SUPPORT_EMAIL or "").strip()):
        lines += ["", f"Need a hand? Write to {support}."]
    lines += [
        "",
        "Keep tracking. Keep improving.",
        "",
        "Monitra — Staff Management System",
        "Store Transform",
    ]
    return lines


def build_weekly_report_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    """One person's week, rendered for delivery.

    Everything shown was aggregated for the `user_id` on this row and frozen
    into `payload` at queue time. Nothing is looked up here, so this builder
    cannot reach another employee's data even in principle — there is no
    database session in scope to reach it with.
    """
    subject = weekly_report_subject(payload)
    period = str(payload.get("period_label") or "")
    has_activity = bool(payload.get("has_activity"))

    frame = _frame_context(
        subject=subject,
        preheader=(
            f"Your tracked time, activity and projects for {period}." if period
            else "Your Monitra weekly productivity summary."
        ),
        footer_note=(
            "You are receiving this because you have a Monitra account. It is "
            "sent once a week and covers only the previous completed week."
        ),
    )

    if has_activity:
        lead = Markup("Here is a quick look at your work activity from the previous week.")
        closing_note = Markup(
            "Your complete activity, time and project details are available "
            "in the Monitra dashboard."
        )
    else:
        # A week with nothing in it is a report, not an error, and it is not
        # dressed up as one either: the table below still renders, carrying
        # real zeroes rather than a hidden section or an invented figure.
        lead = Markup("You had no tracked activity during this period.")
        closing_note = Markup(
            "Open Monitra and start a timer to begin tracking your work this week."
        )

    html = render_page(
        "weekly_report.html",
        {
            **frame,
            "greeting": _greeting(payload.get("name")),
            "period_label": period,
            "lead": lead,
            "summary_table": _weekly_summary_table(payload),
            "cta_block": _weekly_cta(payload),
            "closing_note": closing_note,
        },
    )

    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(_weekly_text_lines(payload, has_activity)),
        reply_to=(settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=frame["_inline_images"],
    )


#: Registered after the fact rather than inside the literal above, because the
#: builder is defined below it. The dict is still the one registry the outbox
#: dispatches through — `TYPE_WEEKLY_REPORT`'s wire value, matched exactly.
BUILDERS[TYPE_WEEKLY_REPORT] = build_weekly_report_email


# ----------------------------------------------------------------------
# Workflow 8 — monthly productivity report
# ----------------------------------------------------------------------

def monthly_report_subject(payload: dict[str, Any]) -> str:
    """"Your Monitra Monthly Report — Aug 2026". No figure, for the same
    lock-screen reason as the weekly subject."""
    period = str(payload.get("period_short") or "").strip()
    return clean_subject(
        f"Your Monitra Monthly Report — {period}" if period
        else "Your Monitra Monthly Report"
    )


def monthly_report_dashboard_url(payload: dict[str, Any]) -> Optional[str]:
    """"View Detailed Report" for the month: the same Reports route the weekly
    button opens, filtered to the month's first and last day."""
    return _report_dashboard_url(payload, start_key="month_start", end_key="month_end")


def _most_productive_day_text(payload: dict[str, Any]) -> str:
    day = payload.get("most_productive_day") or {}
    label = str(day.get("label") or "").strip()
    if not label:
        # Nothing was tracked, so there is no such day. Said plainly rather
        # than left out: this is one of the six rows the report always shows.
        return "No tracked days"
    return f"{label} ({format_duration(day.get('total_seconds'))})"


def _monthly_rows(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """The six figures the monthly email always shows, in order. Shared by the
    HTML table and the plain-text alternative so the two cannot disagree."""
    return [
        ("Total tracked time", format_duration(payload.get("total_seconds"))),
        ("Average activity", format_activity(payload.get("average_activity"))),
        ("Projects", str(int(payload.get("project_count") or 0))),
        ("Total working days", str(int(payload.get("working_days") or 0))),
        ("Average per working day", format_duration(payload.get("average_seconds_per_working_day"))),
        ("Most productive day", _most_productive_day_text(payload)),
    ]


def _monthly_summary_table(payload: dict[str, Any]) -> Markup:
    return _rows_table(detail_rows(_monthly_rows(payload)))


def _monthly_cta(payload: dict[str, Any]) -> Markup:
    url = monthly_report_dashboard_url(payload)
    if url is None:
        return Markup("")
    return Markup(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'class="st-cta" style="margin:30px 0 0 0;">'
        '<tr><td align="center" style="background-color:#2563EB;border-radius:8px;">'
        '<a href="{url}" style="display:inline-block;padding:13px 30px;'
        'font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:700;'
        'color:#FFFFFF;text-decoration:none;">View Detailed Report</a>'
        '</td></tr></table>'
    ).format(url=url)


def _monthly_text_lines(payload: dict[str, Any], has_activity: bool) -> list[str]:
    period = str(payload.get("period_label") or "")
    lines = [
        "YOUR MONITRA MONTHLY PRODUCTIVITY REPORT",
        "",
        _greeting(payload.get("name")),
        "",
        f"Month of {period}" if period else "",
        "",
    ]
    if not has_activity:
        lines += [
            f"You had no tracked activity during {period}." if period
            else "You had no tracked activity last month.",
            "",
        ]
    lines.append("-" * 48)
    for label, value in _monthly_rows(payload):
        lines.append(f"  {label}{' ' * max(1, 26 - len(label))}{value}")
    lines.append("-" * 48)
    if not has_activity:
        lines += ["", "Open Monitra and start a timer to begin tracking your work this month."]
    if (url := monthly_report_dashboard_url(payload)):
        lines += ["", f"View your detailed report: {url}"]
    if (support := (settings.MONITRA_SUPPORT_EMAIL or "").strip()):
        lines += ["", f"Need a hand? Write to {support}."]
    lines += ["", "Keep tracking. Keep improving.", "", "Monitra — Staff Management System", "Store Transform"]
    return lines


def build_monthly_report_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    """One person's month, rendered for delivery from its frozen payload."""
    subject = monthly_report_subject(payload)
    period = str(payload.get("period_label") or "")
    has_activity = bool(payload.get("has_activity"))

    frame = _frame_context(
        subject=subject,
        preheader=(
            f"Your tracked time, activity, projects and working days for {period}." if period
            else "Your Monitra monthly productivity summary."
        ),
        footer_note=(
            "You are receiving this because you have a Monitra account. It is "
            "sent once a month and covers only the previous completed month."
        ),
    )

    if has_activity:
        lead = Markup("Here is a quick look at your work activity from the previous month.")
        closing_note = Markup(
            "Your complete activity, time and project details are available "
            "in the Monitra dashboard."
        )
    else:
        lead = Markup("You had no tracked activity during this period.")
        closing_note = Markup(
            "Open Monitra and start a timer to begin tracking your work this month."
        )

    html = render_page(
        "monthly_report.html",
        {
            **frame,
            "greeting": _greeting(payload.get("name")),
            "period_label": period,
            "lead": lead,
            "summary_table": _monthly_summary_table(payload),
            "cta_block": _monthly_cta(payload),
            "closing_note": closing_note,
        },
    )

    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(_monthly_text_lines(payload, has_activity)),
        reply_to=(settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=frame["_inline_images"],
    )


BUILDERS[TYPE_MONTHLY_REPORT] = build_monthly_report_email


# ----------------------------------------------------------------------
# Workflow 9 & 10 — manual time request, receipt, and decision
# ----------------------------------------------------------------------

#: Where each audience reviews or follows a request. Existing routes: the
#: admin/leader Time Tracking screen (opened on its Manual Requests tab) and
#: the member's own Time Tracking screen.
MANUAL_TIME_REVIEW_PATH = "/admin/time-tracking?tab=requests"
MANUAL_TIME_MEMBER_PATH = "/member/time-tracking"

MANUAL_TIME_STATUS_PRESENTATION: dict[str, dict[str, str]] = {
    "pending": {"label": "Pending approval", "accent": "#B45309", "chip_bg": "#FFFBEB", "chip_border": "#FDE68A"},
    "approved": {"label": "Approved", "accent": "#047857", "chip_bg": "#ECFDF5", "chip_border": "#A7F3D0"},
    "rejected": {"label": "Rejected", "accent": "#B91C1C", "chip_bg": "#FEF2F2", "chip_border": "#FECACA"},
}


def _manual_time_status(status: str) -> dict[str, str]:
    presentation = MANUAL_TIME_STATUS_PRESENTATION.get(status)
    if presentation is None:
        raise KeyError(f"No manual time email is defined for status {status!r}.")
    return presentation


def _app_url(path: str) -> Optional[str]:
    base = (settings.MONITRA_APP_URL or "").strip().rstrip("/")
    if not base.startswith("https://"):
        return None
    return f"{base}{path}"


def manual_time_review_url() -> Optional[str]:
    return _app_url(MANUAL_TIME_REVIEW_PATH)


def manual_time_requester_url(payload: dict[str, Any]) -> Optional[str]:
    return _app_url(
        MANUAL_TIME_REVIEW_PATH if payload.get("requester_can_view_directory")
        else MANUAL_TIME_MEMBER_PATH
    )


def _manual_date(payload: dict[str, Any]) -> str:
    raw = str(payload.get("work_date") or "")
    try:
        return datetime.fromisoformat(raw).strftime("%d %B %Y")
    except ValueError:
        return raw


def _manual_time_range(payload: dict[str, Any]) -> Optional[str]:
    if not (payload.get("start_time") and payload.get("end_time")):
        return None
    _d, start, _ = _display_times(payload.get("start_time"))
    _d, end, _ = _display_times(payload.get("end_time"))
    return f"{start.replace(' IST', '')} – {end}"


def _manual_duration(payload: dict[str, Any]) -> str:
    return format_duration(payload.get("total_seconds"))


def _manual_reason(payload: dict[str, Any]) -> Optional[str]:
    """The reason the requester chose, in words; None when none was given.

    A request filed before the field existed, or by a client that does not
    ask for one, has no reason -- the row is then left out rather than filled
    with a placeholder. A value this build does not know (a newer client's)
    is shown as it was sent, not dropped.
    """
    from app.schemas.manual_time_entry import MANUAL_ENTRY_REASON_LABELS

    reason = payload.get("reason")
    if not reason:
        return None
    return MANUAL_ENTRY_REASON_LABELS.get(str(reason), str(reason))


def _manual_rows(payload: dict[str, Any], *, include_employee: bool, include_decision: bool) -> list[tuple[str, Any]]:
    """The request exactly as it was filed, one label/value pair per field."""
    rows: list[tuple[str, Any]] = []
    if include_employee:
        rows += [("Employee", payload.get("name")), ("Email", payload.get("email"))]
    rows += [
        ("Project", payload.get("project_name")),
        ("Task", payload.get("task_name")),
        ("Date", _manual_date(payload)),
        ("Time", _manual_time_range(payload)),
        ("Duration", _manual_duration(payload)),
        ("Reason", _manual_reason(payload)),
        ("Billable", "Yes" if payload.get("is_billable") else "No"),
        ("Submitted", _display_times(payload.get("submitted_at"))[2]),
    ]
    if include_decision:
        rows += [
            ("Status", _manual_time_status(str(payload.get("status") or ""))["label"]),
            ("Reviewed by", payload.get("reviewer_name")),
            ("Reviewed", _display_times(payload.get("decided_at"))[2] if payload.get("decided_at") else None),
        ]
    return rows


def _manual_rows_text(rows: list[tuple[str, Any]]) -> list[str]:
    return [
        f"  {label}{' ' * max(1, 14 - len(label))}{value}"
        for label, value in rows
        if value is not None and str(value).strip()
    ]


def _manual_note_block(payload: dict[str, Any]) -> Markup:
    """The requester's own description, verbatim and escaped, or nothing."""
    description = str(payload.get("description") or "")
    if not description.strip():
        return Markup("")
    return Markup(
        '<p style="margin:24px 0 8px 0;font-family:Helvetica,Arial,sans-serif;font-size:12px;'
        'font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#6B7280;">Description</p>'
        '<div style="padding:14px 16px;background-color:#F8FAFC;border:1px solid #E8ECF3;border-radius:10px;'
        'font-family:Helvetica,Arial,sans-serif;font-size:14px;line-height:22px;color:#1F2937;">{body}</div>'
    ).format(body=paragraphs(description))


def _manual_cta(url: Optional[str], label: str) -> Markup:
    if url is None:
        return Markup("")
    return Markup(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'class="st-cta" style="margin:28px 0 0 0;">'
        '<tr><td align="center" style="background-color:#2563EB;border-radius:8px;">'
        '<a href="{url}" style="display:inline-block;padding:13px 30px;'
        'font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:700;'
        'color:#FFFFFF;text-decoration:none;">{label}</a>'
        '</td></tr></table>'
    ).format(url=url, label=label)


def _manual_email(
    payload: dict[str, Any], recipients: list[str], *, subject: str, status: str,
    heading: str, greeting: str, lead: str, body: str, rows: list[tuple[str, Any]],
    cta_url: Optional[str], cta_label: str, footer_note: str, preheader: str,
) -> OutgoingEmail:
    presentation = _manual_time_status(status)
    frame = _frame_context(subject=subject, preheader=preheader, footer_note=footer_note)
    html = render_page(
        "manual_time_request.html",
        {
            **frame,
            "status_chip": _status_chip(presentation),
            "heading": heading,
            "greeting": greeting,
            "lead": lead,
            "body": body,
            "detail_rows": detail_rows(rows),
            "note_block": _manual_note_block(payload),
            "cta_block": _manual_cta(cta_url, cta_label),
        },
    )

    description = str(payload.get("description") or "")
    text = [heading.upper(), "", greeting, "", lead]
    if body:
        text += ["", body]
    text += ["", "-" * 48, *_manual_rows_text(rows), "-" * 48]
    if description.strip():
        text += ["", "Description", description]
    if cta_url:
        text += ["", f"{cta_label}: {cta_url}"]
    if (support := (settings.MONITRA_SUPPORT_EMAIL or "").strip()):
        text += ["", f"Need a hand? Write to {support}."]
    text += ["", "Monitra — Staff Management System", "Store Transform"]

    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(text),
        reply_to=(settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=frame["_inline_images"],
    )


def manual_time_request_subject(payload: dict[str, Any]) -> str:
    """"Manual Time Request — Priya Raman — 15 September 2026". Goes to an
    approver's inbox, where who and which day are what they sort by."""
    who = str(payload.get("name") or "").strip()
    parts = ["Manual Time Request"] + ([who] if who else []) + [_manual_date(payload)]
    return clean_subject(" — ".join(part for part in parts if part))


def build_manual_time_request_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    """To an admin or leader: a request is waiting for their review."""
    who = str(payload.get("name") or "A team member").strip()
    duration = _manual_duration(payload)
    day = _manual_date(payload)
    return _manual_email(
        payload, recipients,
        subject=manual_time_request_subject(payload),
        status="pending",
        heading="New manual time request",
        greeting=_greeting(payload.get("recipient_name")),
        lead=f"{who} has submitted a manual time request for {duration} on {day}, and it is waiting for your review.",
        body="The request is shown below exactly as it was submitted. Approve or reject it from the Manual Requests tab in Time Tracking.",
        rows=_manual_rows(payload, include_employee=True, include_decision=False),
        cta_url=manual_time_review_url(),
        cta_label="Review Request",
        footer_note=(
            "You are receiving this because you can approve manual time requests for "
            "this employee in Monitra. It is sent once for each new request."
        ),
        preheader=f"{who} requested {duration} on {day}. Waiting for your review.",
    )


def manual_time_receipt_subject(payload: dict[str, Any]) -> str:
    return clean_subject(f"Your Manual Time Request Was Submitted — {_manual_date(payload)}")


def build_manual_time_receipt_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    """To the requester: we received your request and sent it for review."""
    duration = _manual_duration(payload)
    day = _manual_date(payload)
    return _manual_email(
        payload, recipients,
        subject=manual_time_receipt_subject(payload),
        status="pending",
        heading="Your manual time request was submitted",
        greeting=_greeting(payload.get("name")),
        lead=f"Your request for {duration} on {day} has been sent to your admin, HR, manager and team leader for review.",
        body="You will receive another email as soon as it is approved or rejected. Until then it stays pending and is not counted in your tracked time.",
        rows=_manual_rows(payload, include_employee=False, include_decision=False),
        cta_url=manual_time_requester_url(payload),
        cta_label="View My Requests",
        footer_note=(
            "You are receiving this because you submitted a manual time request in Monitra. "
            "It is sent once for each request."
        ),
        preheader=f"Your request for {duration} on {day} is pending review.",
    )


def manual_time_decision_subject(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "")
    word = "Approved" if status == "approved" else "Rejected"
    return clean_subject(f"Your Manual Time Request Was {word} — {_manual_date(payload)}")


def build_manual_time_decision_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    """To the requester: the outcome of their request."""
    status = str(payload.get("status") or "")
    duration = _manual_duration(payload)
    day = _manual_date(payload)
    if status == "approved":
        heading = "Your manual time request was approved"
        lead = f"Good news — your request for {duration} on {day} has been approved."
        body = "This time has been added to your tracked time and now appears in your timesheet and reports."
        preheader = f"Approved: {duration} on {day} has been added to your tracked time."
    elif status == "rejected":
        heading = "Your manual time request was rejected"
        lead = f"Your request for {duration} on {day} was not approved."
        body = ("This time has not been added to your tracked time. If you think this is a mistake, "
                "please speak to your admin or team leader, or submit a corrected request.")
        preheader = f"Rejected: {duration} on {day} was not added to your tracked time."
    else:
        raise KeyError(f"No manual time decision email is defined for status {status!r}.")
    return _manual_email(
        payload, recipients,
        subject=manual_time_decision_subject(payload),
        status=status,
        heading=heading,
        greeting=_greeting(payload.get("name")),
        lead=lead,
        body=body,
        rows=_manual_rows(payload, include_employee=False, include_decision=True),
        cta_url=manual_time_requester_url(payload),
        cta_label="View My Requests",
        footer_note=(
            "You are receiving this because you submitted a manual time request in Monitra. "
            "It is sent once, when the request is decided."
        ),
        preheader=preheader,
    )


BUILDERS[TYPE_MANUAL_TIME_REQUEST] = build_manual_time_request_email
BUILDERS[TYPE_MANUAL_TIME_RECEIPT] = build_manual_time_receipt_email
BUILDERS[TYPE_MANUAL_TIME_DECISION] = build_manual_time_decision_email


# ----------------------------------------------------------------------
# Workflow 11 — monthly project summary
# ----------------------------------------------------------------------

_CELL = "padding:9px 8px;border-bottom:1px solid #EDF0F5;font-family:Helvetica,Arial,sans-serif;font-size:13px;line-height:18px;"
_HEAD = ("padding:8px 8px;border-bottom:1px solid #E2E8F0;background-color:#F8FAFC;"
         "font-family:Helvetica,Arial,sans-serif;font-size:10px;font-weight:700;letter-spacing:0.06em;"
         "text-transform:uppercase;color:#64748B;")


def monthly_project_summary_subject(payload: dict[str, Any]) -> str:
    """"Monitra Monthly Project Summary — Sep 2026". No figures in the subject:
    a lock screen is not the place for a company's hours."""
    period = str(payload.get("month_short") or "").strip()
    return clean_subject(
        f"Monitra Monthly Project Summary — {period}" if period else "Monitra Monthly Project Summary"
    )


def monthly_project_summary_url(payload: dict[str, Any]) -> Optional[str]:
    """The Reports page's Projects report, filtered to the reported month.

    An existing route with its existing ``?start=&end=`` parameters. Readers
    with ``time_entries:view_all`` open the organisation screen (which a
    leader's own scope already narrows); anyone else opens their own Reports
    screen, the only one their account can open.
    """
    return _report_dashboard_url(payload, start_key="month_start", end_key="month_end")


def _hours_text(seconds: Any) -> str:
    return format_duration(seconds)


def _remaining_markup(project: dict[str, Any]) -> Markup:
    remaining = project.get("remaining_seconds")
    if remaining is None:
        return Markup('<span style="color:#94A3B8;">—</span>')
    if remaining < 0:
        return Markup('<span style="color:#DC2626;font-weight:700;">Over by {v}</span>').format(v=_hours_text(-remaining))
    return Markup('<span style="color:#047857;font-weight:700;">{v} left</span>').format(v=_hours_text(remaining))


def _remaining_text(project: dict[str, Any]) -> str:
    remaining = project.get("remaining_seconds")
    if remaining is None:
        return "—"
    return f"Over by {_hours_text(-remaining)}" if remaining < 0 else f"{_hours_text(remaining)} left"


def _summary_cards(totals: dict[str, Any]) -> Markup:
    cards = [
        ("Projects", str(int(totals.get("projects_worked") or 0))),
        ("Total Hours Used", _hours_text(totals.get("total_seconds"))),
        ("Internal Hours", _hours_text(totals.get("internal_seconds"))),
        ("Billable Hours", _hours_text(totals.get("billable_seconds"))),
    ]
    cell = Markup(
        '<td width="50%" valign="top" style="padding:6px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background-color:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;">'
        '<tr><td style="padding:14px 16px;">'
        '<p style="margin:0;font-family:Helvetica,Arial,sans-serif;font-size:11px;font-weight:700;'
        'letter-spacing:0.06em;text-transform:uppercase;color:#64748B;">{label}</p>'
        '<p style="margin:4px 0 0 0;font-family:Helvetica,Arial,sans-serif;font-size:22px;'
        'font-weight:700;color:#0F172A;">{value}</p></td></tr></table></td>'
    )
    rows = Markup("")
    for index in range(0, len(cards), 2):
        pair = Markup("").join(cell.format(label=label, value=value) for label, value in cards[index:index + 2])
        rows += Markup("<tr>{pair}</tr>").format(pair=pair)
    return Markup(
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:0 -6px;">{rows}</table>'
    ).format(rows=rows)


def _highlight_rows(totals: dict[str, Any]) -> list[tuple[str, Any]]:
    """Highlights: the fixed budgets in play, the flexible hours used, and four
    at-a-glance facts. Project counts live in each table's heading, and each
    fixed project's used-to-date and remaining live in its own row."""
    rows: list[tuple[str, Any]] = []
    if totals.get("fixed_projects"):
        rows.append(("Fixed hours allocated", _hours_text(totals.get("fixed_allocated_seconds"))))
    if totals.get("flexible_projects"):
        rows.append(("Flexible hours used", _hours_text(totals.get("flexible_total_seconds"))))
    if totals.get("projects_worked"):
        highest = totals.get("highest_project") or {}
        highest_billable = totals.get("highest_billable_project") or {}
        rows += [
            ("Contributors", str(int(totals.get("contributors") or 0))),
            ("Average hours per project", _hours_text(totals.get("average_seconds_per_project"))),
            ("Most hours", f"{highest.get('name')} ({_hours_text(highest.get('total_seconds'))})" if highest else None),
            ("Most billable hours",
             f"{highest_billable.get('name')} ({_hours_text(highest_billable.get('billable_seconds'))})"
             if highest_billable else None),
        ]
    return rows


def _project_table(projects: list[dict[str, Any]], *, fixed: bool) -> Markup:
    """One compact, email-safe table. Every value escaped; no raw HTML from data."""
    if not projects:
        return Markup("")
    headers = ["Project", "Internal", "Billable", "Total"] + (["Remaining"] if fixed else [])
    head = Markup("").join(
        Markup('<th align="{a}" style="{s}">{h}</th>').format(a="left" if i == 0 else "right", s=_HEAD, h=h)
        for i, h in enumerate(headers)
    )
    body = Markup("")
    for project in projects:
        name_cell = Markup('<span style="font-weight:600;color:#0F172A;">{n}</span>').format(n=project.get("name"))
        if fixed:
            name_cell += Markup(
                '<br><span style="font-size:11px;color:#64748B;">{alloc} allocated · {used} used to date</span>'
            ).format(alloc=_hours_text(project.get("allocation_seconds")),
                     used=_hours_text(project.get("used_to_date_seconds")))
        cells = [
            Markup('<td style="{s}">{v}</td>').format(s=_CELL, v=name_cell),
            Markup('<td align="right" style="{s}color:#475569;">{v}</td>').format(s=_CELL, v=_hours_text(project.get("internal_seconds"))),
            Markup('<td align="right" style="{s}color:#475569;">{v}</td>').format(s=_CELL, v=_hours_text(project.get("billable_seconds"))),
            Markup('<td align="right" style="{s}font-weight:700;color:#0F172A;">{v}</td>').format(s=_CELL, v=_hours_text(project.get("total_seconds"))),
        ]
        if fixed:
            cells.append(Markup('<td align="right" style="{s}">{v}</td>').format(s=_CELL, v=_remaining_markup(project)))
        body += Markup("<tr>{c}</tr>").format(c=Markup("").join(cells))
    return Markup(
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="border:1px solid #E2E8F0;border-radius:10px;border-collapse:separate;overflow:hidden;">'
        '<tr>{head}</tr>{body}</table>'
    ).format(head=head, body=body)


def _section(title: str, note: str, table: Markup) -> Markup:
    if not table:
        return Markup("")
    return Markup(
        '<h2 style="margin:30px 0 4px 0;font-family:Helvetica,Arial,sans-serif;font-size:17px;'
        'font-weight:700;color:#0F172A;">{title}</h2>'
        '<p style="margin:0 0 12px 0;font-family:Helvetica,Arial,sans-serif;font-size:13px;'
        'line-height:20px;color:#64748B;">{note}</p>{table}'
    ).format(title=title, note=note, table=table)


def _summary_cta(url: Optional[str]) -> Markup:
    if url is None:
        return Markup("")
    return Markup(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" class="st-cta" '
        'style="margin:24px 0 0 0;"><tr><td align="center" style="background-color:#2563EB;border-radius:8px;">'
        '<a href="{url}" style="display:inline-block;padding:13px 30px;font-family:Helvetica,Arial,sans-serif;'
        'font-size:15px;font-weight:700;color:#FFFFFF;text-decoration:none;">View Detailed Project Report</a>'
        '</td></tr></table>'
    ).format(url=url)


def _summary_text_lines(payload: dict[str, Any], url: Optional[str]) -> list[str]:
    totals = payload.get("totals") or {}
    lines = [
        "MONTHLY PROJECT SUMMARY",
        f"Project performance summary for {payload.get('month_label') or ''}",
        "",
        _greeting(payload.get("name")),
        "",
        f"Reporting period: {payload.get('period_label') or ''}",
        "Covers every project in the organisation." if payload.get("company_wide")
        else "Covers the projects you lead or work on.",
        "",
        f"  Projects                 {int(totals.get('projects_worked') or 0)}",
        f"  Total hours used         {_hours_text(totals.get('total_seconds'))}",
        f"  Internal hours           {_hours_text(totals.get('internal_seconds'))}",
        f"  Billable hours           {_hours_text(totals.get('billable_seconds'))}",
    ]
    for label, value in _highlight_rows(totals):
        if value:
            lines.append(f"  {label}{' ' * max(1, 25 - len(label))}{value}")
    if not totals.get("projects_worked"):
        lines += ["", "No project activity was recorded during this reporting period."]
    if payload.get("fixed_projects"):
        lines += ["", f"FIXED HOURS PROJECTS ({len(payload['fixed_projects'])})", "(Remaining = allocation minus billable hours used to date; internal hours do not use the allocation.)"]
        for p in payload["fixed_projects"]:
            lines.append(
                f"- {p.get('name')}: internal {_hours_text(p.get('internal_seconds'))}, "
                f"billable {_hours_text(p.get('billable_seconds'))}, total {_hours_text(p.get('total_seconds'))}, "
                f"allocated {_hours_text(p.get('allocation_seconds'))}, used to date {_hours_text(p.get('used_to_date_seconds'))}, "
                f"remaining {_remaining_text(p)}"
            )
    if payload.get("flexible_projects"):
        lines += ["", f"FLEXIBLE TIME PROJECTS ({len(payload['flexible_projects'])})", "(No fixed allocation, so no remaining hours.)"]
        for p in payload["flexible_projects"]:
            lines.append(
                f"- {p.get('name')}: internal {_hours_text(p.get('internal_seconds'))}, "
                f"billable {_hours_text(p.get('billable_seconds'))}, total {_hours_text(p.get('total_seconds'))}"
            )
    if url:
        lines += ["", f"View Detailed Project Report: {url}"]
    lines += ["", "Monitra — Staff Management System", "Store Transform"]
    return lines


def build_monthly_project_summary_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    """One recipient's monthly project summary, from the payload frozen at queue time.

    No database session is in scope here: everything shown was computed for
    this recipient's scope before it was queued.
    """
    subject = monthly_project_summary_subject(payload)
    totals = payload.get("totals") or {}
    month = str(payload.get("month_label") or "")
    url = monthly_project_summary_url(payload)
    worked = int(totals.get("projects_worked") or 0)

    frame = _frame_context(
        subject=subject,
        preheader=(
            f"{worked} project{'s' if worked != 1 else ''}, {_hours_text(totals.get('total_seconds'))} "
            f"tracked in {month}." if worked else f"No project activity was recorded in {month}."
        ),
        footer_note=(
            "You are receiving this because you are an administrator, an owner or a leader in "
            "Monitra. It is sent once a month and covers only the previous completed month."
        ),
    )
    scope_note = (
        "This summary covers every project in your organisation."
        if payload.get("company_wide")
        else "This summary covers only the projects you lead or work on."
    )
    empty_note = (
        Markup('<p style="margin:22px 0 0 0;padding:14px 16px;background-color:#F8FAFC;border:1px solid #E2E8F0;'
               'border-radius:10px;font-family:Helvetica,Arial,sans-serif;font-size:14px;color:#475569;">'
               'No project activity was recorded during this reporting period.</p>')
        if not worked else Markup("")
    )
    highlights = Markup(
        '<h2 style="margin:30px 0 12px 0;font-family:Helvetica,Arial,sans-serif;font-size:17px;'
        'font-weight:700;color:#0F172A;">Highlights</h2>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="margin:0;border:1px solid #EDF0F5;border-radius:10px;">{rows}</table>'
    ).format(rows=detail_rows(_highlight_rows(totals)))

    html = render_page(
        "monthly_project_summary.html",
        {
            **frame,
            "greeting": _greeting(payload.get("name")),
            "month_label": month,
            "period_label": str(payload.get("period_label") or ""),
            "scope_note": scope_note,
            "summary_cards": _summary_cards(totals),
            "empty_note": empty_note,
            "highlights": highlights,
            "fixed_section": _section(
                f"Fixed Hours Projects ({len(payload.get('fixed_projects') or [])})",
                "Remaining is the allocation minus billable hours used from the project's start "
                f"through the end of {month}. Internal hours do not use the allocation.",
                _project_table(list(payload.get("fixed_projects") or []), fixed=True),
            ),
            "flexible_section": _section(
                f"Flexible Time Projects ({len(payload.get('flexible_projects') or [])})",
                "No fixed allocation, so there are no remaining hours.",
                _project_table(list(payload.get("flexible_projects") or []), fixed=False),
            ),
            "cta_bottom": _summary_cta(url),
        },
    )
    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(_summary_text_lines(payload, url)),
        reply_to=(settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=frame["_inline_images"],
    )


BUILDERS[TYPE_MONTHLY_PROJECT_SUMMARY] = build_monthly_project_summary_email


# ----------------------------------------------------------------------
# Workflow 12 — fixed-hours budget alert
# ----------------------------------------------------------------------

PROJECT_MANAGEMENT_PATH = "/admin/project-management"
MEMBER_PROJECTS_PATH = "/member/projects"

#: Per state: the badge text (the state never depends on colour alone), the
#: badge colours, the heading, and the message.
BUDGET_ALERT_PRESENTATION: dict[str, dict[str, str]] = {
    "remaining_50": {
        "label": "50% Hours Remaining", "subject": "50% Hours Remaining",
        # Dark text on yellow: white on a yellow this bright is unreadable.
        "accent": "#1F2937", "bg": "#FACC15", "border": "#EAB308",
        "headline": "Half of the allocated project hours remain.",
        "message": "Half of the allocated hours for this project have been used. "
                   "It is a good moment to review progress and the work still planned.",
    },
    "remaining_20": {
        "label": "20% Hours Remaining", "subject": "20% Hours Remaining",
        "accent": "#FFFFFF", "bg": "#EA580C", "border": "#EA580C",
        "headline": "20% of the allocated project hours remain.",
        "message": "Only 20% of the allocated hours are left. The remaining work may "
                   "need closer monitoring against the budget.",
    },
    "remaining_10": {
        "label": "10% Hours Remaining", "subject": "10% Hours Remaining",
        "accent": "#FFFFFF", "bg": "#DC2626", "border": "#DC2626",
        "headline": "10% of the allocated project hours remain.",
        "message": "Only 10% of the allocated hours are left. Please review the remaining "
                   "scope and progress with the team.",
    },
    "consumed": {
        "label": "100% Hours Consumed", "subject": "100% Hours Consumed",
        "accent": "#FFFFFF", "bg": "#991B1B", "border": "#991B1B",
        "headline": "All of the allocated project hours have been used.",
        "message": "The project's allocated hours are fully consumed. Any further tracked "
                   "work will take the project over its approved allocation.",
    },
    "over_budget": {
        "label": "OVER BUDGET", "subject": "Project Over Budget",
        "accent": "#FFFFFF", "bg": "#991B1B", "border": "#991B1B",
        "headline": "This project is over its allocated hours.",
        "message": "The project's allocated hours are fully consumed and tracked work has "
                   "gone beyond the approved allocation.",
    },
}


def _budget_presentation(payload: dict[str, Any]) -> dict[str, str]:
    presentation = BUDGET_ALERT_PRESENTATION.get(str(payload.get("state") or ""))
    if presentation is None:
        raise KeyError(f"No budget alert email is defined for state {payload.get('state')!r}.")
    return presentation


def project_budget_alert_subject(payload: dict[str, Any]) -> str:
    """"Monitra — 20% Hours Remaining: Project Alpha" / "Monitra — Project Over Budget: …"."""
    name = str(payload.get("project_name") or "").strip()
    return clean_subject(f"Monitra — {_budget_presentation(payload)['subject']}: {name}")


def project_budget_alert_url(payload: dict[str, Any]) -> Optional[str]:
    """Project Management, where Used, Internal and Remaining are shown per
    project; members without directory access get their own Projects page,
    the only project screen their account can open. Existing routes only."""
    return _app_url(PROJECT_MANAGEMENT_PATH if payload.get("can_view_directory") else MEMBER_PROJECTS_PATH)


def _budget_rows(payload: dict[str, Any]) -> list[tuple[str, Any]]:
    over = int(payload.get("over_budget_seconds") or 0)
    rows: list[tuple[str, Any]] = [
        ("Project", payload.get("project_name")),
        ("Project type", "Fixed Hours"),
        ("Project status", payload.get("project_status")),
        ("Fixed hours", _hours_text(payload.get("allocation_seconds"))),
        ("Used hours", _hours_text(payload.get("used_seconds"))),
        ("Remaining hours", _hours_text(payload.get("remaining_seconds"))),
    ]
    if over > 0:
        rows.append(("Over budget by", _hours_text(over)))
    rows += [
        ("Internal hours", f"{_hours_text(payload.get('internal_seconds'))} (not counted against the budget)"
         if payload.get("internal_seconds") else None),
        ("Generated", _display_times(payload.get("generated_at"))[2]),
    ]
    return rows


def build_project_budget_alert_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    presentation = _budget_presentation(payload)
    subject = project_budget_alert_subject(payload)
    url = project_budget_alert_url(payload)
    frame = _frame_context(
        subject=subject,
        preheader=f"{payload.get('project_name')}: {presentation['label']}. {presentation['headline']}",
        footer_note=(
            "You are receiving this because you are an administrator, an owner or a leader "
            "for this project in Monitra. Each alert is sent once per project budget."
        ),
    )
    # A solid, centred capsule: the state is the first thing the reader sees.
    badge = Markup(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="margin:4px 0 24px 0;"><tr><td align="center">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td align="center" style="padding:14px 34px;background-color:{bg};border:2px solid {border};'
        'border-radius:999px;font-family:Helvetica,Arial,sans-serif;font-size:18px;line-height:22px;'
        'font-weight:800;letter-spacing:0.08em;text-transform:uppercase;color:{accent};">{label}</td>'
        '</tr></table></td></tr></table>'
    ).format(**presentation)
    cta = Markup("")
    if url:
        cta = Markup(
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" class="st-cta" '
            'style="margin:28px 0 0 0;"><tr><td align="center" style="background-color:#2563EB;border-radius:8px;">'
            '<a href="{url}" style="display:inline-block;padding:13px 30px;font-family:Helvetica,Arial,sans-serif;'
            'font-size:15px;font-weight:700;color:#FFFFFF;text-decoration:none;">View Project</a></td></tr></table>'
        ).format(url=url)
    rows = _budget_rows(payload)
    html = render_page(
        "project_budget_alert.html",
        {
            **frame,
            "status_badge": badge,
            "project_name": str(payload.get("project_name") or ""),
            "headline": presentation["headline"],
            "greeting": _greeting(payload.get("name")),
            "message": presentation["message"],
            "detail_rows": detail_rows(rows),
            "cta_block": cta,
        },
    )
    text = [
        presentation["label"].upper(),
        str(payload.get("project_name") or ""),
        "",
        _greeting(payload.get("name")),
        "",
        presentation["headline"],
        presentation["message"],
        "",
        *[f"  {label}{' ' * max(1, 17 - len(label))}{value}" for label, value in rows if value],
        "",
        "Remaining is the fixed allocation minus used (billable) hours. Internal hours do not use the allocation.",
    ]
    if url:
        text += ["", f"View Project: {url}"]
    text += ["", "Monitra — Staff Management System", "Store Transform"]
    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(text),
        reply_to=(settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=frame["_inline_images"],
    )


BUILDERS[TYPE_PROJECT_BUDGET_ALERT] = build_project_budget_alert_email


# ----------------------------------------------------------------------
# Workflow 13 — a notice about an employee's screenshot
# ----------------------------------------------------------------------

#: The Content-ID the screenshot is attached under, and referenced by in the HTML.
SCREENSHOT_CID = "screenshot_image"
#: A screenshot wider than this is scaled down for the email. A merged
#: multi-monitor capture can be several thousand pixels wide, and a mailbox is
#: a poor place to keep one at full size; this is still sharp on any screen.
SCREENSHOT_EMAIL_MAX_WIDTH = 1400


def screenshot_notice_subject(payload: dict[str, Any]) -> str:
    """"Monitra — A notice about your screenshot".

    Carries neither the notice text nor the sender: it is the line that shows on
    a lock screen, and the notice is for the employee, not for whoever is
    standing behind them.
    """
    return clean_subject("Monitra — A notice about your screenshot")


def fetch_screenshot_for_email(payload: dict[str, Any]) -> InlineImage:
    """The screenshot, downloaded from storage and made email-safe.

    Stored as WebP, which Outlook and some other clients do not render inline,
    so it is converted to JPEG here. The bytes are read at *send* time rather
    than frozen into the queued row: the row stays small, and a retry reads the
    same file again.

    Raises `EmailDeliveryError` when the image cannot be read, so the outbox
    retries it with backoff. A notice about a screenshot that arrives without
    the screenshot would be worse than one that arrives a few minutes late.
    """
    file_id = str(payload.get("drive_file_id") or "").strip()
    if not file_id:
        raise EmailDeliveryError("The screenshot has no stored image to attach.")

    from io import BytesIO

    from PIL import Image

    from app.services.google_drive_service import drive_service

    try:
        raw = drive_service.download_file(file_id)
    except Exception as exc:  # noqa: BLE001 - any storage failure is retryable
        raise EmailDeliveryError("The screenshot could not be read from storage.") from exc

    try:
        with Image.open(BytesIO(raw)) as source:
            image = source.convert("RGB")
        if image.width > SCREENSHOT_EMAIL_MAX_WIDTH:
            height = round(image.height * SCREENSHOT_EMAIL_MAX_WIDTH / image.width)
            image = image.resize((SCREENSHOT_EMAIL_MAX_WIDTH, max(1, height)))
        out = BytesIO()
        image.save(out, format="JPEG", quality=85, optimize=True)
        return InlineImage(
            cid=SCREENSHOT_CID, filename="screenshot.jpg", content=out.getvalue(), subtype="jpeg",
        )
    except Exception as exc:  # noqa: BLE001
        raise EmailDeliveryError("The screenshot image could not be prepared for email.") from exc


def _screenshot_block() -> Markup:
    """The picture, framed. Width is capped so it fits a phone."""
    return Markup(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="margin:0 0 22px 0;border:1px solid #E2E8F0;border-radius:10px;'
        'border-collapse:separate;overflow:hidden;background-color:#0F172A;"><tr><td>'
        '<img src="cid:{cid}" alt="The screenshot this notice is about" width="560" '
        'style="display:block;border:0;outline:none;text-decoration:none;width:100%;'
        'max-width:100%;height:auto;" />'
        '</td></tr></table>'
    ).format(cid=SCREENSHOT_CID)


def build_screenshot_notice_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    """The notice, addressed to the employee whose screenshot it is about.

    Sent by the deployment's one authorised mailbox, with the *sender's* name
    beside it ("Grace Hopper via Monitra") and Reply-To pointing at them, so
    hitting Reply reaches the person who wrote the notice.
    """
    subject = screenshot_notice_subject(payload)
    sender = " ".join(str(payload.get("sender_name") or "").split()) or "A Monitra administrator"
    sender_role = str(payload.get("sender_role") or "").strip()
    message = str(payload.get("message") or "")
    _day, _clock, captured = _display_times(payload.get("captured_at"))
    project = str(payload.get("project_name") or "").strip() or None
    task = str(payload.get("task_name") or "").strip() or None
    lead = f"{sender} has sent you a notice about one of your screenshots."

    frame = _frame_context(
        subject=subject,
        preheader=f"{sender} sent you a notice about your screenshot from {captured}.",
        footer_note=(
            "You are receiving this because someone reviewing your tracked time wrote "
            "a notice about one of your screenshots."
        ),
    )
    image = fetch_screenshot_for_email(payload)

    rows = detail_rows([
        ("From", f"{sender} ({sender_role})" if sender_role else sender),
        ("Project", project),
        ("Task", task),
        ("Screenshot taken", captured),
    ])
    reply_note = "You can reply to this email to answer the sender directly."

    html = render_page(
        "screenshot_notice.html",
        {
            **frame,
            "greeting": _greeting(payload.get("recipient_name")),
            "lead": lead,
            "screenshot_block": _screenshot_block(),
            "message_html": paragraphs(message),
            "detail_rows": rows,
            "reply_note": reply_note,
        },
    )

    text_lines = [
        "A NOTICE ABOUT YOUR SCREENSHOT",
        "",
        _greeting(payload.get("recipient_name")),
        "",
        lead,
        "The screenshot is attached to this message.",
        "",
        "Notice",
        "-" * 48,
        message,
        "-" * 48,
        "",
        f"  From              {sender}" + (f" ({sender_role})" if sender_role else ""),
    ]
    if project:
        text_lines.append(f"  Project           {project}")
    if task:
        text_lines.append(f"  Task              {task}")
    text_lines += [f"  Screenshot taken  {captured}", "", reply_note, "", "Monitra — Staff Management System", "Store Transform"]

    sender_email = str(payload.get("sender_email") or "").strip()
    reply_to = None
    if sender_email:
        try:
            reply_to = normalise_address(sender_email, field_label="Sender email")
        except EmailAddressError:
            reply_to = None

    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(text_lines),
        from_name=_sender_display_name(sender),
        reply_to=reply_to or (settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=(*frame["_inline_images"], image),
    )


BUILDERS[TYPE_SCREENSHOT_NOTICE] = build_screenshot_notice_email


# ----------------------------------------------------------------------
# Workflow 14 -- an administrator changed a member's Login / Add Task switch
# ----------------------------------------------------------------------

#: Each of the four emails, by (switch, allowed). The copy is fixed -- nothing
#: in it is typed by a person -- and only says what the switch really does:
#: Login stops every sign-in and ends the running timer (`MemberService`), and
#: Add Task withdraws the single `tasks:create` permission and nothing else.
#: No reason is quoted because none is recorded: the administrator is not asked
#: for one, and inventing one would be worse than saying nothing.
_ACCESS_EXCLUDED_STYLE = {
    "label": "Excluded", "accent": "#BE123C", "chip_bg": "#FFF1F2", "chip_border": "#FECDD3",
}
_ACCESS_ALLOWED_STYLE = {
    "label": "Allowed", "accent": "#047857", "chip_bg": "#ECFDF5", "chip_border": "#A7F3D0",
}

MEMBER_ACCESS_PRESENTATION: dict[tuple[str, bool], dict[str, str]] = {
    ("login", False): {
        **_ACCESS_EXCLUDED_STYLE,
        "what": "Logging in",
        "subject": "Monitra Access Update — You Have Been Excluded from Logging In",
        "preheader": "An administrator has excluded your account from logging in to Monitra.",
        "heading": "You have been excluded from logging in",
        "lead": "An administrator has excluded your account from logging in to Monitra.",
        "body": (
            "You have been signed out of the Monitra desktop app and website. If a timer "
            "was running, it was stopped and the time tracked up to that point has been "
            "saved. You will not be able to sign in again until an administrator allows "
            "your account. If you believe this is a mistake, or you need access to be "
            "restored, please contact your administrator or HR."
        ),
        "cta": "",
    },
    ("login", True): {
        **_ACCESS_ALLOWED_STYLE,
        "what": "Logging in",
        "subject": "Monitra Access Update — You Can Log In Again",
        "preheader": "Good news — you have been allowed to log in to Monitra again.",
        "heading": "You can log in to Monitra again",
        "lead": (
            "Good news — an administrator has allowed your account to log in to Monitra "
            "again. The issue has been resolved."
        ),
        "body": (
            "You can now sign in to the desktop app and the website as usual, and pick up "
            "where you left off. If you still cannot sign in, please contact your "
            "administrator or HR."
        ),
        "cta": "Log In to Monitra",
    },
    ("add_tasks", False): {
        **_ACCESS_EXCLUDED_STYLE,
        "what": "Adding tasks",
        "subject": "Monitra Access Update — You Have Been Excluded from Adding Tasks",
        "preheader": "An administrator has turned off adding tasks for your account.",
        "heading": "You have been excluded from adding tasks",
        "lead": "An administrator has excluded your account from adding tasks in Monitra.",
        "body": (
            "You can still sign in, track your time and use the rest of Monitra as before — "
            "only creating new tasks is turned off, until an administrator allows it again. "
            "If you need a task created in the meantime, please ask your administrator or "
            "team leader. If you believe this is a mistake, please contact your "
            "administrator or HR."
        ),
        "cta": "",
    },
    ("add_tasks", True): {
        **_ACCESS_ALLOWED_STYLE,
        "what": "Adding tasks",
        "subject": "Monitra Access Update — You Can Add Tasks Again",
        "preheader": "Good news — you have been allowed to add tasks in Monitra again.",
        "heading": "You can add tasks again",
        "lead": (
            "Good news — an administrator has allowed your account to add tasks in Monitra "
            "again. The issue has been resolved."
        ),
        "body": (
            "You can now create new tasks from the desktop app and the website, exactly as "
            "before. If adding a task still does not work, please contact your "
            "administrator or HR."
        ),
        "cta": "",
    },
}


def member_access_presentation(payload: dict[str, Any]) -> dict[str, str]:
    """How one switch in one position is worded.

    An unknown pair raises rather than falling back to neutral wording: the
    payload is written by `queue_member_access_notification`, which only queues
    the four pairs above, so anything else means the two have drifted apart and
    a vague-but-wrong email is worse than a row that retries and is logged.
    """
    key = (str(payload.get("switch") or ""), bool(payload.get("allowed")))
    presentation = MEMBER_ACCESS_PRESENTATION.get(key)
    if presentation is None:
        raise KeyError(f"No member access email is defined for {key!r}.")
    return presentation


def member_access_subject(payload: dict[str, Any]) -> str:
    """"Monitra Access Update — You Have Been Excluded from Logging In".

    No name and no administrator: it is the line shown on a lock screen.
    """
    return clean_subject(member_access_presentation(payload)["subject"])


def build_member_access_email(payload: dict[str, Any], recipients: list[str]) -> OutgoingEmail:
    """The Excluded / Allowed notice for Login or Add Task, to the member it concerns."""
    presentation = member_access_presentation(payload)
    subject = member_access_subject(payload)
    greeting = _greeting(payload.get("name"))
    _day, _clock, changed = _display_times(payload.get("changed_at"))

    frame = _frame_context(
        subject=subject,
        preheader=presentation["preheader"],
        footer_note=(
            "You are receiving this because an administrator changed your access in "
            "Monitra. It is sent once for each change."
        ),
    )

    rows = detail_rows([
        ("Access", presentation["what"]),
        ("Status", presentation["label"]),
        ("Changed", changed),
    ])
    # Only the "allowed" emails link anywhere, and only when MONITRA_APP_URL is a
    # real https:// address: an excluded member has nothing to open, and a button
    # to localhost is worse than none.
    url = _app_url("/login") if presentation["cta"] else None
    cta_block = _manual_cta(url, presentation["cta"])

    html = render_page(
        "member_access.html",
        {
            **frame,
            "status_chip": _status_chip(presentation),
            "heading": presentation["heading"],
            "greeting": greeting,
            "lead": presentation["lead"],
            "body": presentation["body"],
            "detail_rows": rows,
            "cta_block": cta_block,
        },
    )

    text_lines = [
        presentation["heading"].upper(),
        "",
        greeting,
        "",
        presentation["lead"],
        "",
        presentation["body"],
        "",
        f"  Access   {presentation['what']}",
        f"  Status   {presentation['label']}",
        f"  Changed  {changed}",
    ]
    if url:
        text_lines += ["", f"{presentation['cta']}: {url}"]
    if (support := (settings.MONITRA_SUPPORT_EMAIL or "").strip()):
        text_lines += ["", f"Need a hand? Write to {support}."]
    text_lines += ["", "Monitra — Staff Management System", "Store Transform"]

    return OutgoingEmail(
        to=recipients,
        subject=subject,
        html=html,
        text="\n".join(text_lines),
        reply_to=(settings.EMAIL_REPLY_TO or "").strip() or None,
        inline_images=frame["_inline_images"],
    )


BUILDERS[TYPE_MEMBER_ACCESS] = build_member_access_email
