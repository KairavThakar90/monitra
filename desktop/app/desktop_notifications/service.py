"""
Backend call for the administrator-managed notification schedule.

One method, one endpoint, in the same shape as `app/maintenance/service.py`:
it carries the request and hands the answer back. What the schedule *means*
(which reminders are on, when) is the backend's answer; parsing it into
something the scheduler can trust is the schedule service's job, not this
client's.
"""
from contextlib import contextmanager
from typing import Any, Dict, Iterator

from app.api.client import ApiClient, ApiStream, TIMEOUT_FAST
from app.api.exceptions import ApiConnectionError, ApiError, ApiHttpError
from core.logging_setup import get_logger

log = get_logger("notification_schedule.api")


class NotificationScheduleApiService:
    """Client for `GET /desktop-notifications/schedule` and, to hear about a
    change the moment it is made, `GET /desktop-notifications/stream`."""

    def __init__(self, api_client: ApiClient) -> None:
        self.api_client = api_client

    @contextmanager
    def open_stream(self, since: int, *, read_timeout: float) -> Iterator[ApiStream]:
        """Open the change stream; leave the block to close it.

        The stream carries no schedule, only a signal that the version moved
        past `since` -- the schedule itself is always fetched with
        `get_schedule`. Failures to open it are raised as `ApiError`, a 404 with
        its `status_code` (an older deployment without the route is a normal
        state during a rollout); failures while reading are raised by
        `ApiStream.lines()` as the client's own exception types.
        """
        try:
            with self.api_client.stream_get(
                "/desktop-notifications/stream", {"since": since}, read_timeout=read_timeout,
            ) as stream:
                yield stream
        except ApiHttpError as exc:
            raise ApiError(
                f"Notification change stream refused (HTTP {exc.status_code}).",
                status_code=exc.status_code,
            )

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
