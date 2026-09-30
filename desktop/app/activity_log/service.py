"""
Backend call for the desktop's own activity events.

One method, one endpoint, in the same shape as `app/maintenance/service.py`:
it carries the report and hands the answer back. What the desktop reports is
narrow on purpose -- that the application was opened, and that it was closed.
Everything else in the activity trail (signing in, the timer, tasks) is
recorded by the backend from the requests that already perform those actions;
these two are the only facts only the client knows.

Never called from the GUI thread. `SyncService` is the one caller, draining
the durable queue on its own thread, so a report survives a quit, an outage
and a restart, and a failure here can never delay the window closing.
"""
from typing import Any, Dict

from app.api.client import ApiClient, TIMEOUT_FAST
from app.api.exceptions import ApiConnectionError, ApiError, ApiHttpError

#: The events this client may report. Mirrors `ClientEventCreate.event` in
#: backend/app/schemas/activity_log.py; anything else is refused there.
CLIENT_EVENT_OPENED = "app_opened"
CLIENT_EVENT_CLOSED = "app_closed"
CLIENT_EVENTS = (CLIENT_EVENT_OPENED, CLIENT_EVENT_CLOSED)


class ActivityLogApiService:
    """Client for the backend's client-event endpoint."""

    ENDPOINT = "/api/v1/activity-logs/client-events"

    def __init__(self, api_client: ApiClient) -> None:
        self.api_client = api_client

    def record_client_event(self, event: str, occurred_at: str) -> Dict[str, Any]:
        """Report that the application was opened or closed.

        :param event: One of `CLIENT_EVENTS`.
        :param occurred_at: ISO-8601 UTC instant the event happened, captured
            when it happened -- not when this request is sent, which may be a
            later launch. The backend is idempotent on (user, event, instant),
            so a retried report is answered with the row already recorded.
        :raises ApiError: On any failure, with `status_code` when the backend
            answered, so the queue can tell a refusal from an outage.
        """
        payload = {"event": event, "occurred_at": occurred_at}
        try:
            response = self.api_client.post(self.ENDPOINT, json_data=payload, timeout=TIMEOUT_FAST)
            data = response.json()
            return data if isinstance(data, dict) else {}
        except ApiHttpError as exc:
            if exc.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            # 404 is an older deployment without the endpoint -- the queue
            # completes the action rather than retrying it for ever.
            raise ApiError(
                f"Could not report {event} (HTTP {exc.status_code}).",
                status_code=exc.status_code,
            )
        except ApiConnectionError:
            raise ApiError(f"Could not report {event}: network error.")
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"Could not report {event}: {exc}")
