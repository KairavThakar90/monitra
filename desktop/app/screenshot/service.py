"""
Backend calls for the screenshot module's own configuration.

One method, in the same shape as `IdleApiService` (`app/idle/service.py`):
it translates HTTP outcomes into `ApiError` with the backend's own
explanation attached, and it never decides anything. The backend is
authoritative for how often this user's screen is captured; this layer only
carries the request and hands the answer back.
"""
from typing import Any, Dict

from app.api.client import ApiClient, TIMEOUT_FAST
from app.api.exceptions import (
    ApiConnectionError, ApiError, ApiHttpError, error_detail,
)
from core.logging_setup import get_logger

log = get_logger("screenshot.api")


def _explain(action: str, exc: ApiHttpError) -> ApiError:
    """Turn an HTTP failure into a sentence the user can act on."""
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


class ScreenshotApiService:
    """Client for the backend's screenshot-configuration endpoint."""

    def __init__(self, api_client: ApiClient) -> None:
        self.api_client = api_client

    def get_config(self) -> Dict[str, Any]:
        """The signed-in user's screenshot capture configuration.

        `{"capture_frequency": int}` -- minutes. The same field rides along
        on `GET /auth/me`, which is where the desktop seeds it from at
        login; this endpoint exists so the scheduler can refresh it without
        re-fetching the whole profile.
        """
        try:
            response = self.api_client.get("/screenshots/config", timeout=TIMEOUT_FAST)
            return response.json()
        except ApiHttpError as exc:
            raise _explain("Loading the screenshot configuration", exc)
        except ApiConnectionError:
            raise ApiError("Could not load the screenshot configuration: network error.")
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"Could not load the screenshot configuration: {exc}")

    def get_privacy_config(self) -> Dict[str, Any]:
        """Fetch the screenshot privacy exclusions (apps and URLs).

        Unlike `/screenshots/config` above, this router
        (`backend/app/api/screenshot_privacy.py`) is registered only once,
        at its own internal `/api/v1/screenshot` prefix -- there is no
        duplicate bare-root registration to fall back on, so the leading
        `/api/v1` here is required, not optional.
        """
        try:
            response = self.api_client.get("/api/v1/screenshot/privacy-config", timeout=TIMEOUT_FAST)
            return response.json()
        except ApiHttpError as exc:
            raise _explain("Loading the screenshot privacy configuration", exc)
        except ApiConnectionError:
            raise ApiError("Could not load the screenshot privacy configuration: network error.")
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"Could not load the screenshot privacy configuration: {exc}")
