from fastapi import FastAPI
from app.api.auth import router as auth_router
from app.api.project import router as project_router
from app.api.task import router as task_router
from app.api.project_member import router as project_member_router
from app.api.task_assignee import router as task_assignee_router
from app.api.time_entry import router as time_entry_router
from app.api.manual_time_entry import router as manual_time_entry_router
from app.api.employees import router as employees_router
from app.api.time_entry_screenshot import router as time_entry_screenshot_router
from app.api.members import router as members_router
from app.api.project_management import router as project_management_router
from app.api.time_entry_app_usage import router as time_entry_app_usage_router
from app.api.time_entry_activity import router as time_entry_activity_router
from app.api.url_usage import router as url_usage_router
from app.api.time_entry_idle_period import router as idle_period_router
from app.api.teams import router as teams_router
from app.api.time_tracking import router as time_tracking_router
from app.api.desktop_release import router as desktop_release_router
from app.api.feedback import router as feedback_router
from app.api.email_notifications import router as email_notifications_router
from app.api.system import router as system_router
from app.api.activity_rollup import router as activity_rollup_router
from app.react_apis.reports import router as reports_router
from app.react_apis.manual_time_entry import router as react_manual_time_entry_router
from app.react_apis.member_usage import router as member_usage_router
from app.react_apis.reports_page.router import router as reports_page_router
from app.react_apis.dashboard.router import router as dashboard_router
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
import logging

# Setup logging for serverless environment
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Staff Management System API",
    description="Backend API for Staff Management System",
    version="0.1.0"
)

# Log app initialization for debugging
logger.info(f"FastAPI app initialized. Environment: {settings.ENV}")

# Screenshot storage, stated at boot. Desktop clients keep captures on disk and
# retry when this is wrong, so a misconfiguration is invisible from the server
# side until someone reads an upload's 503 — and the two most common mistakes
# (an unset variable, and a stale process still holding an old .env) both look
# identical from the client. Naming the resolved configuration here makes the
# running process say which one it actually has. Non-sensitive by
# construction: `describe_configuration()` reports the folder id and whether a
# credential is present, never any part of the key.
try:
    from app.services.google_drive_service import drive_service as _drive_service

    _drive_config = _drive_service.describe_configuration()
    if _drive_config["configured"]:
        logger.info(
            "Screenshot storage: Google Drive root %s (credentials from %s)",
            _drive_config["root_folder_id"], _drive_config["credential_source"],
        )
    else:
        logger.warning(
            "Screenshot storage is DISABLED: %s. Desktop clients will queue "
            "screenshots locally and retry.",
            _drive_service.unconfigured_reason(),
        )
except Exception:  # noqa: BLE001
    logger.warning("Could not report screenshot storage configuration", exc_info=True)

# Email, stated at boot, for the same reason. A deployment with no mail
# configuration looks completely healthy: feedback still saves, sign-in still
# works, and the notifications simply queue forever with nothing to send them.
# Saying so here is what turns that into something a person can notice.
# Non-sensitive by construction: `describe_configuration()` reports which
# settings are populated, never any value.
try:
    from app.services.email import describe_configuration as _describe_email
    from app.services.email import describe_feedback_recipients as _describe_recipients
    from app.services.email import unconfigured_reason as _email_unconfigured

    _email_config = _describe_email()
    if _email_config["configured"]:
        logger.info(
            "Email delivery: provider=%s assets=%s authenticated=%s",
            _email_config["provider"], _email_config["asset_mode"],
            _email_config["smtp_authenticated"],
        )
    else:
        logger.warning(
            "Email delivery is DISABLED: %s. Welcome and feedback notifications "
            "will queue in email_notifications and stay pending.",
            _email_unconfigured(),
        )
    if not _describe_recipients()["configured"]:
        logger.warning(
            "Feedback notifications have NO recipients: set FEEDBACK_ADMIN_EMAIL "
            "and FEEDBACK_HR_EMAIL, or no one will be told about submitted feedback."
        )
except Exception:  # noqa: BLE001
    logger.warning("Could not report email configuration", exc_info=True)

# 1. Base registrations for desktop client endpoints (which expect paths without /api/v1)
app.include_router(auth_router)
app.include_router(project_router)
app.include_router(task_router)
app.include_router(project_member_router)
app.include_router(task_assignee_router)
app.include_router(time_entry_router)
app.include_router(manual_time_entry_router)
app.include_router(employees_router)
app.include_router(time_entry_screenshot_router)
app.include_router(members_router)
app.include_router(time_entry_app_usage_router)
app.include_router(time_entry_activity_router)
app.include_router(url_usage_router)
app.include_router(idle_period_router)
app.include_router(desktop_release_router)
app.include_router(feedback_router)
app.include_router(system_router)
app.include_router(activity_rollup_router)
# Registered once, without the /api/v1 prefix: its two routes are a scheduler
# trigger and a public image URL, and both are referenced by absolute path —
# from a cron configuration and from inside already-delivered email. A second
# spelling of either would be a second URL to keep working forever.
app.include_router(email_notifications_router)

# 2. Registrations with the /api/v1 prefix (expected by React frontend and prefix-aware desktop calls)
api_prefix = "/api/v1"
app.include_router(auth_router, prefix=api_prefix)
app.include_router(time_entry_router, prefix=api_prefix)
app.include_router(manual_time_entry_router, prefix=api_prefix)
app.include_router(employees_router, prefix=api_prefix)
app.include_router(time_entry_screenshot_router, prefix=api_prefix)
app.include_router(members_router, prefix=api_prefix)
app.include_router(time_entry_app_usage_router, prefix=api_prefix)
app.include_router(time_entry_activity_router, prefix=api_prefix)
app.include_router(url_usage_router, prefix=api_prefix)
app.include_router(idle_period_router, prefix=api_prefix)
app.include_router(desktop_release_router, prefix=api_prefix)
app.include_router(feedback_router, prefix=api_prefix)
app.include_router(system_router, prefix=api_prefix)
app.include_router(activity_rollup_router, prefix=api_prefix)

# 3. Registrations for routers that contain their own /api/v1 internal prefix
# These must only be registered once without prefix parameters to avoid double-prefixing.
app.include_router(project_management_router)
app.include_router(teams_router)
app.include_router(time_tracking_router)
app.include_router(reports_router)
app.include_router(react_manual_time_entry_router)
app.include_router(member_usage_router)
app.include_router(reports_page_router)
app.include_router(dashboard_router)

@app.get("/")
def read_root():
    return {"message": "Staff Management System API is running.", "environment": settings.ENV}

@app.get("/health")
def health_check():
    """Liveness, plus whether this deployment can actually store screenshots.

    A desktop client keeps captures on disk and retries when upload answers
    503, so a deployment missing its Drive settings looks identical to a
    healthy one from every direction except an upload. Reporting the resolved
    configuration here means the deployment can be checked directly after a
    redeploy, rather than by reading platform logs. Non-sensitive by
    construction: `describe_configuration()` returns the root folder id and
    where the credential came from, never any part of the key.
    """
    payload = {"status": "healthy", "environment": settings.ENV}
    try:
        from app.services.google_drive_service import drive_service

        storage = drive_service.describe_configuration()
        if not storage["configured"]:
            storage["reason"] = drive_service.unconfigured_reason()
        payload["screenshot_storage"] = storage
    except Exception:  # noqa: BLE001
        # Health must stay answerable even if storage cannot be introspected.
        logger.warning("Could not report screenshot storage in /health", exc_info=True)
        payload["screenshot_storage"] = {"configured": False, "reason": "unavailable"}

    try:
        from app.services.email import (
            describe_configuration, describe_feedback_recipients, unconfigured_reason,
        )

        email = describe_configuration()
        if not email["configured"]:
            email["reason"] = unconfigured_reason()
        email["feedback_recipients"] = describe_feedback_recipients()
        email["dispatch_endpoint_enabled"] = bool(settings.EMAIL_DISPATCH_TOKEN)
        payload["email"] = email
    except Exception:  # noqa: BLE001
        logger.warning("Could not report email configuration in /health", exc_info=True)
        payload["email"] = {"configured": False, "reason": "unavailable"}

    return payload


# Configure CORS to always allow both local and production frontends
cors_origins = [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
    "https://staff-management-system-frontend-six.vercel.app",
    "https://staff.peakworkos.com",
    "https://monitra-lvzq.vercel.app",
    "https://staff-management.vercel.app",
    "https://stafftrack.io",
    "https://www.stafftrack.io"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
