from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError as SQLAlchemyInterfaceError
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from app.api.auth import router as auth_router
from app.api.project import router as project_router
from app.api.task import router as task_router
from app.api.project_member import router as project_member_router
from app.api.task_assignee import router as task_assignee_router
from app.api.time_entry import router as time_entry_router
from app.api.time_entry_reconcile import router as time_entry_reconcile_router
from app.api.manual_time_entry import router as manual_time_entry_router
from app.api.employees import router as employees_router
from app.api.time_entry_screenshot import router as time_entry_screenshot_router
from app.api.members import router as members_router
from app.api.project_management import router as project_management_router
from app.WFPM.router import internal_router as wfpm_internal_router, router as wfpm_router
from app.api.time_entry_app_usage import router as time_entry_app_usage_router
from app.api.time_entry_activity import router as time_entry_activity_router
from app.api.url_usage import router as url_usage_router
from app.api.time_entry_idle_period import router as idle_period_router
from app.api.teams import router as teams_router
from app.api.time_tracking import router as time_tracking_router
from app.api.desktop_notifications import router as desktop_notifications_router
from app.api.desktop_release import router as desktop_release_router
from app.api.feedback import router as feedback_router
from app.api.email_notifications import router as email_notifications_router
from app.api.system import router as system_router
from app.api.desktop_version_enforcement import router as desktop_version_enforcement_router
from app.api.activity_rollup import router as activity_rollup_router
from app.api.clients import router as clients_router, public_router as clients_public_router
from app.react_apis.reports import router as reports_router
from app.react_apis.manual_time_entry import router as react_manual_time_entry_router
from app.react_apis.member_usage import router as member_usage_router
from app.react_apis.member_activity_log import router as member_activity_log_router
from app.react_apis.activity_logs import router as activity_logs_router
from app.core.request_context import request_context_middleware
from app.core.request_log import RequestLogMiddleware
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
        # Configured is not reachable. The probe actually opens the root
        # folder with the configured credential and logs
        # SCREENSHOT_STORAGE_PROBE with the outcome; on a background thread so
        # a slow or failing Drive never delays the process from serving. Its
        # result is what /health reports as `screenshot_storage.probe`.
        _drive_service._refresh_probe_in_background()
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

# The email registry is checked at boot and a fault stops the start: an outbox
# builder that is not registered (so Email Management cannot show it and nobody
# can be copied on it), a copyable email that is never queued, or a configuration
# flag the registry names that does not exist. Each is a developer error that
# would otherwise surface as a silently unmanageable email in production.
from app.core.email_catalogue import check_registry as _check_email_registry  # noqa: E402
from app.services.email.messages import BUILDERS as _EMAIL_BUILDERS  # noqa: E402

_check_email_registry(_EMAIL_BUILDERS, settings)

# The WFPM timer integration, stated at boot for the same reason again. With
# WFPM_TIMER_START_URL / WFPM_TIMER_STOP_URL unset, timers start and stop
# exactly as they always have and WFPM is simply never told -- nothing
# anywhere fails. Non-sensitive
# by construction: `describe_configuration()` reports whether a URL and a
# token are present, never either value.
try:
    from app.WFPM.timer_sync import describe_configuration as _describe_wfpm

    _wfpm_config = _describe_wfpm()
    if _wfpm_config["configured"]:
        logger.info(
            "WFPM timer integration: start enabled, stop %s (token %s)",
            "enabled" if _wfpm_config["stop_configured"] else "DISABLED (WFPM_TIMER_STOP_URL is not set)",
            "present" if _wfpm_config["token_present"] else "NOT set",
        )
    else:
        logger.warning(
            "WFPM timer integration is DISABLED: %s. Timers started in Monitra "
            "will not start a timer in WFPM.",
            _wfpm_config["reason"],
        )
except Exception:  # noqa: BLE001
    logger.warning("Could not report WFPM integration configuration", exc_info=True)

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
app.include_router(desktop_version_enforcement_router)
app.include_router(desktop_notifications_router)
app.include_router(activity_rollup_router)
# Registered once, without the /api/v1 prefix: its routes are a scheduler
# trigger, a public image URL, and (clients_public_router) an invitation's
# Reject link (and the older Approve link, which now forwards to the
# set-password page), and all of these are referenced by absolute path —
# from a cron configuration and from inside already-delivered email. A second
# spelling of any of them would be a second URL to keep working forever.
app.include_router(email_notifications_router)
app.include_router(time_entry_reconcile_router)
app.include_router(clients_public_router)
# The WFPM timer-event sweeper: another scheduler entry point, same reasoning.
app.include_router(wfpm_internal_router)

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
app.include_router(desktop_version_enforcement_router, prefix=api_prefix)
app.include_router(desktop_notifications_router, prefix=api_prefix)
app.include_router(activity_rollup_router, prefix=api_prefix)

# 3. Registrations for routers that contain their own /api/v1 internal prefix
# These must only be registered once without prefix parameters to avoid double-prefixing.
from app.api.screenshot_privacy import router as screenshot_privacy_router
from app.api.screenshot_settings import router as screenshot_settings_router
from app.api.member_display import router as member_display_router
from app.api.email_cc import router as email_cc_router

app.include_router(project_management_router)
app.include_router(wfpm_router)
app.include_router(teams_router)
app.include_router(time_tracking_router)
app.include_router(reports_router)
app.include_router(react_manual_time_entry_router)
app.include_router(member_usage_router)
app.include_router(member_activity_log_router)
app.include_router(activity_logs_router)
app.include_router(reports_page_router)
app.include_router(dashboard_router)
app.include_router(screenshot_privacy_router)
app.include_router(screenshot_settings_router)
app.include_router(member_display_router)
app.include_router(email_cc_router)
app.include_router(clients_router)

@app.get("/")
def read_root():
    return {"message": "Staff Management System API is running.", "environment": settings.ENV}

@app.get("/health")
def health_check(deep: bool = False):
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
        else:
            # Whether the configured credential can actually open the root
            # folder, from the cached probe (see GoogleDriveService.probe):
            # never a Drive round trip on this request, and never more than a
            # coarse reason code, since this endpoint is public.
            storage["probe"] = drive_service.probe()
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

    try:
        from app.WFPM.timer_sync import describe_configuration as describe_wfpm

        payload["wfpm_timer_sync"] = describe_wfpm()
    except Exception:  # noqa: BLE001
        logger.warning("Could not report WFPM integration in /health", exc_info=True)
        payload["wfpm_timer_sync"] = {"configured": False, "reason": "unavailable"}

    try:
        from app.core.timer_policy import TimerPolicy

        # What the server does about a timer whose desktop has gone quiet. `applies`
        # is false until the deployment turns the scheduled run on: it then only
        # reports (docs/TIMING_MODEL.md section 9).
        from app.api.time_entry_reconcile import last_run_snapshot

        payload["timer_reconciler"] = {
            "applies": bool(settings.TIMER_RECONCILER_ENABLED),
            **TimerPolicy.current().as_dict(),
            # None until the scheduler has called this process: proof, or the lack of it,
            # that the installed task actually runs.
            "last_run": last_run_snapshot(),
        }
    except Exception:  # noqa: BLE001
        logger.warning("Could not report the timer policy in /health", exc_info=True)
        payload["timer_reconciler"] = {"applies": False, "reason": "unavailable"}

    # Pool utilisation of the process that answered. Each worker has its own pool,
    # so the figure is per worker; sustained `checked_out` near `max_connections`
    # is the early warning for exhaustion.
    from app.core.database import pool_snapshot

    pool = pool_snapshot()
    if pool is not None:
        payload["database_pool"] = pool

    if deep:
        # Opt-in: the plain check stays free of the database, because a probe that
        # opens connections is itself a load. `?deep=1` is what a monitor should
        # call -- it answers "can this process reach the database, and how fast?",
        # which the plain check cannot (it is healthy while every request fails).
        payload["database"] = _probe_database()
        if payload["database"]["status"] != "ok":
            payload["status"] = "degraded"
        else:
            # Whether the host's timers are actually calling this backend, from the
            # durable job ledger. A scheduler that was never installed, or stopped,
            # makes the deployment `degraded` here -- so an outside monitor polling
            # this URL raises the alarm through a channel that is not the email the
            # missing scheduler would have sent. Counts and timestamps only.
            payload["scheduled_jobs"] = _probe_scheduled_jobs()
            if not payload["scheduled_jobs"].get("healthy", False):
                payload["status"] = "degraded"

    return payload


def _probe_scheduled_jobs() -> dict:
    from app.core.database import get_session_local
    from app.services.scheduled_jobs import scheduler_status

    db = None
    try:
        db = get_session_local()()
        verdict = scheduler_status(db)
        # The compact form: a monitor wants the verdict and the per-job last run,
        # not every tally. `/internal/jobs/status` carries the rest.
        return {
            "healthy": verdict["healthy"],
            "problems": verdict["problems"],
            "jobs": {
                name: {
                    "last_started_at": info["last_started_at"],
                    "last_status": info["last_status"],
                    "stale": info["stale"],
                    "never_ran": info["never_ran"],
                    "next_expected_at": info["next_expected_at"],
                }
                for name, info in verdict["jobs"].items()
            },
        }
    except Exception as exc:  # noqa: BLE001 - health must stay answerable
        logger.warning("HEALTH_SCHEDULED_JOBS_PROBE_FAILED: %s", type(exc).__name__)
        return {"healthy": False, "problems": [f"ledger unavailable: {type(exc).__name__}"], "jobs": {}}
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def _probe_database() -> dict:
    import time as _time

    from sqlalchemy import text

    from app.core.database import get_engine

    started = _time.perf_counter()
    try:
        with get_engine().connect() as connection:
            database_now = connection.execute(text("SELECT now()")).scalar()
        result = {"status": "ok", "latency_ms": int((_time.perf_counter() - started) * 1000)}
        from datetime import datetime, timezone

        if isinstance(database_now, datetime):
            # Application clock minus the database's. `last_seen_at` and every report's
            # "now" are the database's; the figures a response computes in Python are the
            # application's. A few seconds is nothing; minutes means they will disagree.
            if database_now.tzinfo is None:
                database_now = database_now.replace(tzinfo=timezone.utc)
            result["clock_skew_seconds"] = round(
                (datetime.now(timezone.utc) - database_now).total_seconds(), 3
            )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("HEALTH_DB_PROBE_FAILED: %s", type(exc).__name__)
        return {"status": "unreachable", "error": type(exc).__name__,
                "latency_ms": int((_time.perf_counter() - started) * 1000)}


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

# Added BEFORE CORS so it sits inside it: an exception that escapes a route is
# answered here with a JSON 500 that CORS then decorates. Left to Starlette's
# last-resort handler the 500 has no Access-Control-Allow-Origin and the browser
# reports a server error as a network failure. See app/core/request_log.py.
app.add_middleware(RequestLogMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Which client is calling (desktop or web) and from where, held for the life
# of one request so the activity trail can record it without every service
# taking a Request. See app/core/request_context.py.
app.middleware("http")(request_context_middleware)


def _db_unavailable_response(request: Request, exc: Exception, *, state: str) -> JSONResponse:
    from app.core.request_log import current_request_id

    request_id = current_request_id()
    logger.error(
        "DB_UNAVAILABLE: path=%s req=%s kind=%s error=%s",
        request.url.path, request_id, state, type(exc).__name__,
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "The service is temporarily unavailable; please retry shortly.",
                 "request_id": request_id},
        headers={"Retry-After": "2"},
    )


@app.exception_handler(SQLAlchemyOperationalError)
@app.exception_handler(SQLAlchemyInterfaceError)
async def database_unavailable_handler(request: Request, exc: Exception):
    """The database could not be reached, or dropped the connection mid-request.

    These used to escape as a plain-text 500 with no CORS headers. A server that
    lost its database for a moment is *unavailable*, not broken: 503 with
    `Retry-After` tells every client to ask again, and the log names the path
    and the request id. A statement the database cancelled for running too long
    (`DB_STATEMENT_TIMEOUT_MS`) is different -- asking again repeats the work --
    so it is a 504 with a long `Retry-After`, which clients do not retry.
    """
    original = getattr(exc, "orig", None)
    if original is not None and type(original).__name__ == "QueryCanceled":
        from app.core.request_log import current_request_id

        request_id = current_request_id()
        logger.error("DB_STATEMENT_TIMEOUT: path=%s req=%s", request.url.path, request_id)
        return JSONResponse(
            status_code=504,
            content={"detail": "The request took too long to complete.", "request_id": request_id},
            headers={"Retry-After": "30"},
        )
    return _db_unavailable_response(request, exc, state="connection")


@app.exception_handler(SQLAlchemyTimeoutError)
async def database_pool_exhausted_handler(request: Request, exc: SQLAlchemyTimeoutError):
    """The pool had no free connection within `DB_POOL_TIMEOUT_SECONDS`.

    Answered 503 with `Retry-After` instead of an unexplained 500, and logged
    with the pool's state: this is the "QueuePool limit ... reached" failure,
    and the log line is how it is counted. The desktop queues and retries on
    any 5xx; a 503 says honestly that the server is busy rather than broken.
    """
    from app.core.database import get_engine

    try:
        state = get_engine().pool.status()
    except Exception:  # noqa: BLE001
        state = "unavailable"
    from app.core.request_log import current_request_id

    logger.error(
        "DB_POOL_EXHAUSTED: path=%s req=%s pool=%s", request.url.path, current_request_id(), state
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "The service is busy; please retry shortly."},
        headers={"Retry-After": "2"},
    )
