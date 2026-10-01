"""
Backend call for the administrator-managed notification schedule.

One method, one endpoint, in the same shape as `app/maintenance/service.py`:
it carries the request and hands the answer back. What the schedule *means*
(which reminders are on, when) is the backend's answer; parsing it into
something the scheduler can trust is the schedule service's job, not this
client's.
"""
from typing import Any, Dict

from app.api.client import ApiClient, TIMEOUT_FAST
from app.api.exceptions import ApiConnectionError, ApiError, ApiHttpError
from core.logging_setup import get_logger

log = get_logger("notification_schedule.api")


class NotificationScheduleApiService:
    """Client for `GET /desktop-notifications/schedule`."""

    def __init__(self, api_client: ApiClient) -> None:
        self.api_client = api_client

    def get_schedule(self) -> Dict[str, Any]:
        """The deployment-wide notification schedule, payload unchanged.

        Every failure is raised as `ApiError` (a 404 keeps its
        `status_code`: an older deployment without the endpoint is a normal
        state during a rollout) and the caller holds quietly.
        """
        try:
            response = self.api_client.get("/desktop-notifications/schedule", timeout=TIMEOUT_FAST)
            return response.json()
        except ApiHttpError as exc:
            if exc.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            log.debug("notification schedule fetch failed: HTTP %s", exc.status_code)
            raise ApiError(
                f"Notification schedule fetch failed (HTTP {exc.status_code}).",
                status_code=exc.status_code,
            )
        except ApiConnectionError:
            raise ApiError("Could not fetch the notification schedule: network error.")
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"Could not fetch the notification schedule: {exc}")
