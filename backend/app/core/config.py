import os
import logging
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    DATABASE_URL_DEV: str = ""
    DATABASE_URL: str = ""
    EXTERNAL_AUTH_BASE_URL: str = "https://nothing.peakworkos.com"
    EXTERNAL_AUTH_LOGIN_PATH: str = "/wp-json/st-performance/v1/auth/hubstaff/login"
    # Single sign-on: the provider signs its own JWT for the browser handoff, so the
    # backend verifies that token with the provider and reads the identity behind it
    # instead of trusting anything the URL carries.
    EXTERNAL_AUTH_TOKEN_VALIDATE_PATH: str = "/wp-json/jwt-auth/v1/token/validate"
    EXTERNAL_AUTH_PROFILE_PATH: str = "/wp-json/st-performance/v1/user/profile"
    EXTERNAL_AUTH_CONNECT_TIMEOUT: float = 10.0
    EXTERNAL_AUTH_READ_TIMEOUT: float = 20.0

    # Some upstream WAFs reject Python's default HTTP user agent even when the
    # credentials and JSON payload are valid. Keep this configurable so the
    # integration can use the same API-client identity as the verified request.
    WORDPRESS_LOGIN_USER_AGENT: str = "PostmanRuntime/7.56.1"
    DEFAULT_ORGANIZATION_ID: int = 1

    @property
    def WORDPRESS_LOGIN_URL(self) -> str:
        return f"{self.EXTERNAL_AUTH_BASE_URL.rstrip('/')}/{self.EXTERNAL_AUTH_LOGIN_PATH.lstrip('/')}"

    @property
    def WORDPRESS_TOKEN_VALIDATE_URL(self) -> str:
        return f"{self.EXTERNAL_AUTH_BASE_URL.rstrip('/')}/{self.EXTERNAL_AUTH_TOKEN_VALIDATE_PATH.lstrip('/')}"

    @property
    def WORDPRESS_PROFILE_URL(self) -> str:
        return f"{self.EXTERNAL_AUTH_BASE_URL.rstrip('/')}/{self.EXTERNAL_AUTH_PROFILE_PATH.lstrip('/')}"

    # ── Desktop releases ──────────────────────────────────────────────────
    # What the desktop client's update check is told. Set by the release
    # process only once a draft GitHub release has actually been published --
    # never from a git tag, because a tag exists before anyone has decided the
    # build is good. Clearing DESKTOP_LATEST_VERSION is how a bad release is
    # withdrawn: the in-app prompt stops recommending it immediately.
    #
    # Left empty, the endpoint answers an honest "unknown" and no user is
    # prompted. Never populate these with a placeholder.
    DESKTOP_LATEST_VERSION: str = ""
    DESKTOP_DOWNLOAD_URL: str = ""
    DESKTOP_RELEASE_NOTES_URL: str = ""

    # ── Desktop → web single sign-on handoff ──────────────────────────────
    # How long the desktop's "Profile" handoff token stays valid. It only has
    # to survive the trip from minting it to the browser opening the web
    # client, so it is measured in seconds, not minutes: the token travels in
    # a URL, and a URL is written to history.
    SSO_HANDOFF_TOKEN_EXPIRE_SECONDS: int = 60

    # ── Screenshot storage (Google Drive) ─────────────────────────────────
    # Credentials live here and *only* here. The desktop client never receives
    # a service account key, a private key or a Drive token — it uploads the
    # image to this backend over its ordinary authenticated session, and this
    # backend is the only thing that talks to Google.
    #
    # Supply the service account one of two ways:
    #   GOOGLE_SERVICE_ACCOUNT_JSON       — the JSON itself (what a platform
    #                                       like Vercel can hold as a secret)
    #   GOOGLE_SERVICE_ACCOUNT_JSON_PATH  — a path to the key file on disk
    # The inline form wins when both are set. Neither has a default: with the
    # service unconfigured, screenshot upload answers 503 rather than pretending
    # to have stored an image.
    #
    # GOOGLE_DRIVE_ROOT_FOLDER_ID is the folder the Year/Month/User/Date tree is
    # built under. It must be shared with the service account's own address
    # (client_email in the key) with Editor access, or every upload fails with a
    # 404 from Drive that has nothing to do with the file being uploaded.
    GOOGLE_DRIVE_ROOT_FOLDER_ID: str = ""
    GOOGLE_SERVICE_ACCOUNT_JSON: str = ""
    GOOGLE_SERVICE_ACCOUNT_JSON_PATH: str = ""
    #: Largest screenshot the upload endpoint accepts. The desktop targets
    #: ~120 KB and produces a fixed 1000x1000 image, so this is a guard against
    #: a malformed or hostile client, not a normal-path limit.
    SCREENSHOT_MAX_UPLOAD_BYTES: int = 5 * 1024 * 1024
    #: The window the screenshot timeline groups by. Must match the desktop's
    #: `WINDOW_DURATION_MINUTES`, which is what the capture schedule uses.
    SCREENSHOT_WINDOW_MINUTES: int = 10

    @property
    def google_drive_configured(self) -> bool:
        """Whether screenshot storage can work at all."""
        return bool(
            self.GOOGLE_DRIVE_ROOT_FOLDER_ID
            and (self.GOOGLE_SERVICE_ACCOUNT_JSON or self.GOOGLE_SERVICE_ACCOUNT_JSON_PATH)
        )

    # ── Transactional email ───────────────────────────────────────────────
    # Two automated emails leave this system: a one-time welcome when an
    # account is first provisioned, and a notification to Admin/HR when
    # somebody submits feedback from the desktop client. Both are queued in
    # the `email_notifications` outbox and delivered from there, so nothing
    # below is ever read on a request's critical path.
    #
    # Credentials live here and *only* here. The desktop client never receives
    # an SMTP host, username, password or a recipient address — it posts
    # feedback to this backend over its ordinary authenticated session, and
    # this backend is the only thing that talks to a mail server.
    #
    # EMAIL_PROVIDER selects the transport:
    #   "smtp"     — a real mail server, configured by the SMTP_* values below
    #   "console"  — logs that a message would have been sent, and its
    #                recipients/subject only. For local development.
    #   "disabled" — queue but never deliver. Rows stay pending.
    # Left unconfigured, delivery is skipped and the outbox row stays pending
    # rather than being marked sent: an email this deployment cannot send must
    # never be recorded as one it did.
    EMAIL_PROVIDER: str = "smtp"
    EMAIL_FROM_ADDRESS: str = ""
    EMAIL_FROM_NAME: str = "Monitra"
    #: Where a human reply should go. Optional; omitted from the message when empty.
    EMAIL_REPLY_TO: str = ""

    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    #: STARTTLS on the standard submission port. Turn this off only for a
    #: local relay that speaks plain SMTP on a loopback interface.
    SMTP_USE_TLS: bool = True
    #: Implicit TLS (SMTPS, usually port 465). Mutually exclusive with STARTTLS;
    #: when both are set this one wins, because the socket is already wrapped.
    SMTP_USE_SSL: bool = False
    #: Hard ceiling on one delivery attempt. Delivery runs outside the request
    #: path, but an unbounded socket read would still pin a serverless
    #: invocation until the platform killed it mid-send.
    SMTP_TIMEOUT_SECONDS: float = 20.0

    # ── Feedback notification recipients ──────────────────────────────────
    # Never hard-coded: these are people, and who they are changes without the
    # code changing. Each accepts a single address; FEEDBACK_NOTIFICATION_EMAILS
    # accepts a comma-separated list for any additional recipient. All three are
    # merged, validated and de-duplicated by `resolve_feedback_recipients()`.
    # With none of them set, feedback is still persisted and the notification is
    # simply not queued.
    FEEDBACK_ADMIN_EMAIL: str = ""
    FEEDBACK_HR_EMAIL: str = ""
    FEEDBACK_NOTIFICATION_EMAILS: str = ""

    # ── Welcome email ─────────────────────────────────────────────────────
    #: Whether a newly provisioned account is welcomed at all. The idempotency
    #: guarantee does not depend on this flag — it is the outbox's unique
    #: (notification_type, dedupe_key) that makes a second send impossible.
    WELCOME_EMAIL_ENABLED: bool = True
    #: The "Open Monitra" destination. The welcome email renders its call to
    #: action only when this holds a real https:// URL — a button pointing at
    #: localhost, or at a guess, is worse than no button.
    MONITRA_APP_URL: str = ""
    #: Shown as "need help? write to ..." in both templates. Omitted when empty.
    MONITRA_SUPPORT_EMAIL: str = ""

    # ── Client invitations ────────────────────────────────────────────────
    #: This backend's own publicly reachable base URL. The invitation email's
    #: Approve/Reject buttons are direct backend GET links (not frontend
    #: routes), so they need an absolute URL to this service rather than to
    #: MONITRA_APP_URL, which points at the web client.
    API_BASE_URL: str = ""
    #: How long an invitation's Approve/Reject link stays valid.
    CLIENT_INVITATION_EXPIRE_HOURS: int = 72

    # ── Release announcement ──────────────────────────────────────────────
    #: Whether publishing a desktop release emails every active user about it.
    #: One announcement per user per *version* — publishing the second artifact
    #: of the same version sends nothing. Turn this off to publish quietly (a
    #: pilot, a re-publish after a withdrawal); as with the welcome email, the
    #: once-per-user guarantee does not depend on this flag.
    RELEASE_EMAIL_ENABLED: bool = True

    # ── Weekly productivity report ────────────────────────────────────────
    #: Whether the Monday sweep queues anything at all. A runtime kill switch
    #: for the whole workflow — as with every other email flag here, the
    #: once-per-user-per-week guarantee does not depend on it: that is the
    #: outbox's unique (notification_type, dedupe_key).
    WEEKLY_REPORT_ENABLED: bool = True
    #: The calendar the report period is cut on. Defaults to the timezone the
    #: rest of this system already reports in (`app.core.time_format.IST`), so
    #: a week in the email is the same week the dashboard shows. Changing it
    #: moves the Monday/Sunday boundary and nothing else — stored timestamps
    #: stay UTC.
    WEEKLY_REPORT_TIMEZONE: str = "Asia/Kolkata"
    #: When the sweep is meant to run, in WEEKLY_REPORT_TIMEZONE. These do not
    #: schedule anything by themselves — a serverless deployment has no
    #: resident process to hold a timer, so the actual trigger is the cron
    #: entry in `vercel.json`. They are the single source that entry is
    #: derived from: `weekly_cron_expression()` converts them to the UTC cron
    #: line, and a test asserts `vercel.json` still matches, so the two cannot
    #: drift apart silently.
    WEEKLY_REPORT_DAY: str = "monday"
    WEEKLY_REPORT_HOUR: int = 9
    WEEKLY_REPORT_MINUTE: int = 0

    # ── Email delivery mechanics ──────────────────────────────────────────
    #: How many times one notification may be attempted before it is parked as
    #: `failed`. With the backoff below, six attempts span roughly six hours.
    EMAIL_MAX_ATTEMPTS: int = 6
    #: Delay before the first retry. Each subsequent attempt doubles it, up to
    #: EMAIL_RETRY_MAX_DELAY_SECONDS, and every delay carries jitter — a whole
    #: queue retrying in lockstep after an outage is a self-inflicted load test.
    EMAIL_RETRY_BASE_DELAY_SECONDS: int = 60
    EMAIL_RETRY_MAX_DELAY_SECONDS: int = 3600
    #: Most notifications one sweep will attempt. Bounded so a backlog cannot
    #: make a single invocation run past its platform timeout.
    EMAIL_DISPATCH_BATCH_SIZE: int = 20
    #: Shared secret for the dispatch sweeper endpoint, which is how a
    #: scheduler (Vercel Cron, an external cron, a CI job) drains the outbox.
    #: Unset, that endpoint answers 503 and is never open — an unauthenticated
    #: trigger for outbound mail is an open relay with extra steps.
    EMAIL_DISPATCH_TOKEN: str = ""
    #: Public base URL the email templates load their logos from, e.g.
    #: "https://staff.peakworkos.com/email-assets". Left empty, the logos are
    #: attached to the message and referenced by Content-ID instead, which is
    #: what a deployment without public asset hosting should use. Never point
    #: this at localhost: the recipient's mail client, not this server, is what
    #: resolves it.
    EMAIL_ASSET_BASE_URL: str = ""

    @property
    def email_configured(self) -> bool:
        """Whether this deployment can actually deliver a message."""
        provider = (self.EMAIL_PROVIDER or "").strip().lower()
        if not self.EMAIL_FROM_ADDRESS:
            return False
        if provider == "console":
            return True
        if provider == "smtp":
            return bool(self.SMTP_HOST)
        return False

    ENV: str = os.getenv("ENV", "development")

    # JWT_SECRET_KEY must be set in .env for production; development has a default
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "super-secret-key-change-me-in-production")
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # The hard ceiling on one sign-in. A refresh token is only ever valid until
    # the session it belongs to expires, and rotating it never moves that date,
    # so this is literally "how long a user stays signed in before the identity
    # provider must see their credentials again". Only a fresh login starts a
    # new window. Raised from 7 to 90 for the persistent desktop session.
    REFRESH_TOKEN_EXPIRE_DAYS: int = 90

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(__file__), "..", "..", ".env"), 
        env_file_encoding="utf-8", 
        extra="ignore"
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Validate required environment variables for production
        if self.ENV == "production":
            if not self.DATABASE_URL:
                logger.error("ERROR: DATABASE_URL is not set in production environment!")
            if self.JWT_SECRET_KEY == "super-secret-key-change-me-in-production":
                logger.warning("WARNING: Using default JWT_SECRET_KEY in production! Set JWT_SECRET_KEY in environment variables.")

        logger.info(f"Settings initialized. Environment: {self.ENV}")

# Create settings instance
try:
    settings = Settings()
except Exception as e:
    logger.error(f"Failed to initialize settings: {str(e)}")
    # Create a fallback settings object
    settings = Settings(DATABASE_URL_DEV="", DATABASE_URL="", ENV="development")

