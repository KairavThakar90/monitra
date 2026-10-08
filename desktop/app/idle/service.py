"""
Backend calls for the idle-time module.

One method per endpoint, in the same shape as the other services in `app/`:
they translate HTTP outcomes into `ApiError` with the backend's own
explanation attached, and they never decide anything. The backend is
authoritative for whether idle time is counted, how long an idle period
actually was, and where reassigned time lands — this layer only carries the
request and hands the answer back.

Paths are the bare ones (`/idle-periods`, not `/api/v1/idle-periods`),
matching `TimeEntryService`. The backend registers the router under both.
"""
import time
from typing import Any, Dict, Optional

from app.api.client import ApiClient, TIMEOUT_FAST
from app.api.exceptions import (
    ApiConnectionError, ApiError, ApiHttpError, ApiTimeoutError, error_detail,
)
from core.logging_setup import get_logger

log = get_logger("idle.api")

#: Per-phase timeout of every request that decides something. The client's
#: default is 10 s; these answer a user who is looking at a popup, may stop an
#: entry and write adjustments, and are retried by hand, so they get a little
#: longer than a list load without approaching the service's own total
#: deadline (`IdleService.REQUEST_DEADLINE_SECONDS`), which is what actually
#: bounds the wait.
TIMEOUT_DECISION = 15.0


def _network_failure(action: str, exc: BaseException) -> ApiError:
    """A request that never got an answer, with *which way* it failed kept.

    A timeout and a refused connection used to collapse into one sentence
    ("network error" / the raw exception text), so nothing downstream -- and
    nothing in the log -- could tell a slow backend from a dead network.
    `kind` carries it; the message stays one a user can read.
    """
    if isinstance(exc, ApiTimeoutError):
        err = ApiError(f"{action}: the server took too long to answer.")
        err.kind = "timeout"
    elif isinstance(exc, ApiConnectionError):
        err = ApiError(f"{action}: network error.")
        err.kind = "connection"
    else:
        err = ApiError(f"{action}: {exc}")
        err.kind = "other"
    return err


def _explain(action: str, exc: ApiHttpError) -> ApiError:
    """Turn an HTTP failure into a sentence the user can act on.

    The backend already explains its refusals ("Idle period has already been
    resolved.", "Only a pending idle period can be reassigned."). Replacing
    that with a bare status code is what made a previous generation of these
    services impossible to debug from a screenshot.
    """
    detail = error_detail(exc.response_body)
    log.warning(
        "%s failed: HTTP %s%s", action, exc.status_code,
        f" -- {exc.response_body}" if exc.response_body else "",
    )
    if exc.status_code == 401:
        return ApiError("Session expired. Please log in again.", status_code=401)
    if detail:
        return ApiError(detail, status_code=exc.status_code)
    return ApiError(
        f"{action} failed (HTTP {exc.status_code}).", status_code=exc.status_code
    )


class IdleApiService:
    """Client for the backend's idle-period endpoints."""

    def __init__(self, api_client: ApiClient) -> None:
        self.api_client = api_client
        #: Round trip of the last request that got an answer, for diagnostics.
        #: A plain int written from pool threads; a stale read is harmless.
        self.last_latency_ms: Optional[int] = None

    def _timed(self, send):
        started = time.monotonic()
        try:
            return send()
        finally:
            self.last_latency_ms = int((time.monotonic() - started) * 1000)

    # ── Configuration ─────────────────────────────────────────────────────────

    def get_config(self) -> Dict[str, Any]:
        """The signed-in user's idle configuration.

        `{"idle_enabled": bool, "idle_minutes": int}`. The same two fields
        ride along on `GET /auth/me`, which is where the desktop seeds them
        from at login; this endpoint exists so the detector can refresh them
        without re-fetching the whole profile.
        """
        try:
            response = self._timed(
                lambda: self.api_client.get("/idle-periods/config", timeout=TIMEOUT_FAST)
            )
            return response.json()
        except ApiHttpError as exc:
            raise _explain("Loading the idle configuration", exc)
        except Exception as exc:  # noqa: BLE001
            raise _network_failure("Could not load the idle configuration", exc)

    # ── Reporting ─────────────────────────────────────────────────────────────

    def report_idle_period(
        self,
        time_entry_id: int,
        idle_started_at: str,
        idle_detected_at: str,
        client_event_id: Optional[str] = None,
        client_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Report that the configured idle threshold has been reached.

        `idle_started_at` is the last observed input, `idle_detected_at` the
        moment the threshold was crossed — never the end of the idle period,
        which is only known once the user answers the popup.

        `client_event_id` makes the call idempotent: a retry, or a second
        report while one is still pending, returns the existing period rather
        than opening another.

        `client_time` is this machine's clock at the moment of sending. With
        it the backend places both instants by age, so a clock that is
        seconds or minutes off the server's is harmless; without it (and
        before the backend learned to read it) a clock even a second ahead
        was answered 400 on every retry, for ever.
        """
        payload = {
            "time_entry_id": time_entry_id,
            "idle_started_at": idle_started_at,
            "idle_detected_at": idle_detected_at,
        }
        if client_event_id:
            payload["client_event_id"] = client_event_id
        if client_time:
            payload["client_time"] = client_time
        try:
            response = self._timed(lambda: self.api_client.post(
                "/idle-periods", json_data=payload, timeout=TIMEOUT_DECISION,
            ))
            return response.json()
        except ApiHttpError as exc:
            raise _explain("Reporting idle time", exc)
        except Exception as exc:  # noqa: BLE001
            raise _network_failure("Could not report idle time", exc)

    def get_pending_idle_period(self, time_entry_id: int) -> Optional[Dict[str, Any]]:
        """The unresolved idle period for a time entry, or None.

        This is what makes recovery correct after a crash or a restart: a
        pending period lives on the server, so local state is never the only
        record of one. Returns None rather than raising when the backend has
        nothing pending.
        """
        try:
            response = self._timed(lambda: self.api_client.get(
                "/idle-periods/active",
                params={"time_entry_id": time_entry_id},
                timeout=TIMEOUT_FAST,
            ))
            data = response.json()
            return data if isinstance(data, dict) and data.get("id") else None
        except ApiHttpError as exc:
            if exc.status_code == 404:
                return None
            raise _explain("Checking for a pending idle period", exc)
        except Exception as exc:  # noqa: BLE001
            raise _network_failure("Could not check for a pending idle period", exc)

    # ── Resolution ────────────────────────────────────────────────────────────

    def resolve_idle_period(
        self,
        idle_period_id: int,
        keep_idle_time: bool,
        action: str,
        resolved_at: str,
    ) -> Dict[str, Any]:
        """Resolve a pending idle period with the user's answer.

        `action` is "stop" or "resume". The server decides whether the idle
        time counts — only keep + resume does — and, for "stop", stops the
        time entry through its own stop path. Repeating the same answer is
        idempotent there; a different answer after resolution is a 409.
        """
        payload = {
            "keep_idle_time": bool(keep_idle_time),
            "action": action,
            "resolved_at": resolved_at,
        }
        try:
            response = self._timed(lambda: self.api_client.post(
                f"/idle-periods/{idle_period_id}/resolve", json_data=payload,
                timeout=TIMEOUT_DECISION,
            ))
            return response.json()
        except ApiHttpError as exc:
            raise _explain("Resolving the idle period", exc)
        except Exception as exc:  # noqa: BLE001
            raise _network_failure("Could not resolve the idle period", exc)

    def reassign_idle_period(
        self, idle_period_id: int, project_id: int, task_id: int
    ) -> Dict[str, Any]:
        """Attribute this idle period's elapsed time to another project/task.

        The backend validates the project and the task (including that the
        task belongs to the chosen project) against what this user is
        authorised for, writes the destination entry and the offsetting
        deduction in one transaction, and leaves the idle period **pending** —
        the user must still answer the main popup.
        """
        payload = {"project_id": project_id, "task_id": task_id}
        try:
            response = self._timed(lambda: self.api_client.post(
                f"/idle-periods/{idle_period_id}/reassign", json_data=payload,
                timeout=TIMEOUT_DECISION,
            ))
            return response.json()
        except ApiHttpError as exc:
            raise _explain("Reassigning the idle time", exc)
        except Exception as exc:  # noqa: BLE001
            raise _network_failure("Could not reassign the idle time", exc)

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def send_diagnostics(self, payload: Dict[str, Any]) -> None:
        """Hand the backend a health report from the idle monitor, to be logged.

        Best effort by design: the caller never retries it and nothing waits
        on it. An older backend that has no such endpoint answers 404/405,
        which is silence here, not an error.
        """
        try:
            self.api_client.post(
                "/idle-periods/diagnostics", json_data=payload, timeout=TIMEOUT_FAST,
            )
        except ApiHttpError as exc:
            if exc.status_code in (404, 405):
                return
            raise _explain("Reporting idle diagnostics", exc)
        except Exception as exc:  # noqa: BLE001
            raise _network_failure("Could not report idle diagnostics", exc)
