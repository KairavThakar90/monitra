"""Send the two real emails to a real inbox, through the real configuration.

The unit tests pin every rule and never send anything; this is the other half —
proof that *this machine's* configuration can actually put a message in a
mailbox, and that the two templates look right in a real mail client. It uses
the same provider, the same templates, the same logo handling and the same
recipient resolution the application uses. Nothing is stubbed.

It does not touch the database. No account is provisioned, no feedback is
stored, and no outbox row is written: the point here is the transport and the
rendering, and mixing the queue into that only makes a failure harder to read.
(The queue is proved separately, by the test suite and by submitting feedback
through the API.)

Usage, from backend/:

    # 1. Put your real values in backend/.env -- never on the command line,
    #    where they land in shell history:
    #
    #      EMAIL_PROVIDER=smtp
    #      EMAIL_FROM_ADDRESS=you@yourdomain.com
    #      SMTP_HOST=smtp.gmail.com
    #      SMTP_PORT=587
    #      SMTP_USERNAME=you@yourdomain.com
    #      SMTP_PASSWORD=<app password>
    #      FEEDBACK_ADMIN_EMAIL=admin@yourdomain.com
    #      FEEDBACK_HR_EMAIL=hr@yourdomain.com
    #
    # 2. Check what the configuration resolves to, sending nothing:
    python scripts/smoke_email.py --check

    # 3. Send the feedback notification to the configured Admin + HR:
    python scripts/smoke_email.py --feedback

    # 4. Send the welcome email to one address you control:
    python scripts/smoke_email.py --welcome --to you@yourdomain.com

    # Both:
    python scripts/smoke_email.py --all --to you@yourdomain.com

`--check` is worth running first every time. It reports which settings are
populated and who the feedback notification would reach, without contacting a
mail server — which is the difference between "my SMTP password is wrong" and
"I never set FEEDBACK_HR_EMAIL", two failures that otherwise look identical
from the inbox.

Options:
    --check       Report the resolved configuration and exit. Sends nothing.
    --welcome     Send the welcome email. Needs --to.
    --feedback    Send the feedback notification to the configured recipients.
    --all         Both of the above.
    --to ADDRESS  Recipient for --welcome. Also overrides the feedback
                  recipients, for testing without mailing the real Admin/HR.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.services.email import assets  # noqa: E402
from app.services.email.messages import (  # noqa: E402
    build_feedback_email, build_welcome_email,
)
from app.services.email.provider import (  # noqa: E402
    EmailError, get_email_provider, normalise_address, redact_error,
    unconfigured_reason,
)
from app.services.email.recipients import resolve_feedback_recipients  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

#: What the sample feedback says. Deliberately contains markup and several
#: lines: if the escaping or the paragraph handling is wrong, the mail that
#: arrives shows it immediately.
SAMPLE_MESSAGE = (
    "Smoke test from scripts/smoke_email.py.\n"
    "The timer showed 00:00 after resuming from sleep, twice this week.\n"
    "\n"
    "This paragraph exists to check line breaks survive. "
    "This <b>markup</b> and this <script>alert('xss')</script> must appear as "
    "literal text in your mail client, not as formatting or as a blank space."
)


def describe() -> bool:
    """Print the resolved configuration. Returns whether email can be sent."""
    reason = unconfigured_reason()
    print("Transport")
    print(f"  EMAIL_PROVIDER        {settings.EMAIL_PROVIDER or '(unset)'}")
    print(f"  EMAIL_FROM_ADDRESS    {settings.EMAIL_FROM_ADDRESS or '(unset)'}")
    print(f"  EMAIL_FROM_NAME       {settings.EMAIL_FROM_NAME or '(unset)'}")
    print(f"  SMTP_HOST             {settings.SMTP_HOST or '(unset)'}")
    print(f"  SMTP_PORT             {settings.SMTP_PORT}")
    print(f"  SMTP_USERNAME         {'set' if settings.SMTP_USERNAME else '(unset)'}")
    # Never printed, only reported as present or absent.
    print(f"  SMTP_PASSWORD         {'set' if settings.SMTP_PASSWORD else '(unset)'}")
    print(f"  TLS                   {'SMTPS' if settings.SMTP_USE_SSL else 'STARTTLS' if settings.SMTP_USE_TLS else 'NONE'}")

    print("\nFeedback recipients")
    configured = resolve_feedback_recipients()
    print(f"  FEEDBACK_ADMIN_EMAIL  {settings.FEEDBACK_ADMIN_EMAIL or '(unset)'}")
    print(f"  FEEDBACK_HR_EMAIL     {settings.FEEDBACK_HR_EMAIL or '(unset)'}")
    print(f"  FEEDBACK_NOTIFICATION_EMAILS  {settings.FEEDBACK_NOTIFICATION_EMAILS or '(unset)'}")
    print(f"  -> resolves to        {configured or 'NOBODY -- feedback would be saved and no one told'}")

    print("\nArtwork")
    mode = "hosted URL" if settings.EMAIL_ASSET_BASE_URL else "embedded (Content-ID)"
    print(f"  strategy              {mode}")
    for label, filename in (
        ("Monitra", assets.MONITRA_LOGO), ("Store Transform", assets.STORE_TRANSFORM_LOGO),
    ):
        installed = assets.asset_exists(filename)
        print(f"  {label:21} {'installed' if installed else 'MISSING -- renders as text'}")

    print("\nDispatch sweeper")
    print(f"  EMAIL_DISPATCH_TOKEN  {'set' if settings.EMAIL_DISPATCH_TOKEN else '(unset) -- retries will not run'}")

    if reason:
        print(f"\nCANNOT SEND: {reason}")
        return False
    print("\nConfiguration is sendable.")
    return True


def send(message, label: str) -> bool:
    print(f"\nSending {label} to {list(message.to)} ...")
    try:
        get_email_provider().send(message)
    except EmailError as exc:
        # Redacted: an SMTP rejection can quote the credential that failed.
        print(f"  FAILED: {redact_error(exc)}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {redact_error(exc)}")
        return False
    print(f"  SENT. Subject: {message.subject}")
    print(f"  Inline images: {len(message.inline_images)}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report configuration; send nothing.")
    parser.add_argument("--welcome", action="store_true", help="Send the welcome email.")
    parser.add_argument("--feedback", action="store_true", help="Send the feedback notification.")
    parser.add_argument("--all", action="store_true", help="Send both.")
    parser.add_argument("--to", help="Recipient for --welcome; overrides the feedback recipients.")
    args = parser.parse_args()

    sendable = describe()
    if args.check or not (args.welcome or args.feedback or args.all):
        if not (args.welcome or args.feedback or args.all):
            print("\nNothing sent. Pass --welcome, --feedback or --all to send.")
        return 0 if sendable else 1
    if not sendable:
        return 1

    override = None
    if args.to:
        try:
            override = [normalise_address(args.to, field_label="--to")]
        except EmailError as exc:
            print(f"\n--to is not a usable address: {exc}")
            return 1

    results = []

    if args.welcome or args.all:
        if not override:
            print("\n--welcome needs --to: the welcome email goes to one user, and this "
                  "script does not read your user table to find one.")
            return 1
        results.append(send(
            build_welcome_email({"user_id": 0, "name": "Smoke Test"}, override),
            "the welcome email",
        ))

    if args.feedback or args.all:
        recipients = override or resolve_feedback_recipients()
        if not recipients:
            print("\nNo feedback recipients configured. Set FEEDBACK_ADMIN_EMAIL and "
                  "FEEDBACK_HR_EMAIL, or pass --to.")
            return 1
        results.append(send(
            build_feedback_email({
                "feedback_id": 0,
                "user_id": 0,
                "username": "smoke.test",
                "user_name": "Smoke Test",
                "user_email": "smoke.test@example.com",
                "user_role": "employee",
                "category": "report_a_problem",
                "message": SAMPLE_MESSAGE,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "source": "Monitra Desktop",
            }, recipients),
            "the feedback notification",
        ))

    if all(results):
        print("\nAll messages accepted by the mail server. Check the inbox — and the "
              "spam folder, which is where an unauthenticated sending domain lands.")
        return 0
    print("\nOne or more messages failed. Nothing was queued; rerun when fixed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
