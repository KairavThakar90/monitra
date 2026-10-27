"""
Backend calls for the activity module's own configuration.

One method, in the same shape as `ScreenshotApiService` and `IdleApiService`: it
translates HTTP outcomes into `ApiError` with the backend's own explanation
attached, and it never decides anything. The backend is authoritative for whether
this member's activity may be measured (the switch an administrator sets in User
Management); this layer only carries the request and hands the answer back.
"""
from typing import Any, Dict

from app.api.client import ApiClient, TIMEOUT_FAST
from app.api.exceptions import (
    ApiConnectionError, ApiError, ApiHttpError, error_detail,
)
from core.logging_setup import get_logger

log = get_logger("activity.api")


class ActivityApiService:
    """Client for the backend's activity-configuration endpoint."""

    def __init__(self, api_client: ApiClient) -> None:
        self.api_client = api_client

    def get_config(self) -> Dict[str, Any]:
        """The signed-in user's activity configuration.

        `{"activity_tracking_enabled": bool}`. The same field rides along on
        `GET /auth/me`, which is where the desktop seeds it from at login; this
        endpoint exists so the tracker can refresh it without re-fetching the
        whole profile, so an administrator's change reaches a running desktop.
        """
        try:
            response = self.api_client.get("/activity/config", timeout=TIMEOUT_FAST)
            return response.json()
        except ApiHttpError as exc:
            detail = error_detail(exc.response_body)
            log.warning(
                "Loading the activity configuration failed: HTTP %s%s", exc.status_code,
                f" -- {exc.response_body}" if exc.response_body else "",
            )
            if exc.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            raise ApiError(
                detail or f"Loading the activity configuration failed (HTTP {exc.status_code}).",
                status_code=exc.status_code,
            )
        except ApiConnectionError:
            raise ApiError("Could not load the activity configuration: network error.")
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"Could not load the activity configuration: {exc}")
