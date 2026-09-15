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

from datetime import datetime, timezone
from typing import Any, Optional

from markupsafe import Markup

from app.core.config import settings
from app.core.time_format import IST, to_ist
from app.services.email import assets
from app.services.email.provider import (
    EmailAddressError, OutgoingEmail, assert_header_safe, normalise_address,
)
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
    return clean_subject(
        f"Monitra {version} is available — what's new" if version
        else "A new version of Monitra is available"
    )


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
    )


#: Maps a notification type to the builder that renders it. The outbox is
#: type-agnostic: adding a third email is a builder, an entry here and a
#: `dedupe_key`, with nothing in the delivery machinery changing.
BUILDERS = {
    "welcome": build_welcome_email,
    "feedback": build_feedback_email,
    "feedback_status": build_feedback_status_email,
    "release": build_release_email,
}
