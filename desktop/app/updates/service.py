"""
Backend call for the update check.

One method, one endpoint, in the same shape as the other services in `app/`:
it carries the request and hands the answer back. The backend decides what the
latest release is and whether this client is behind it -- that comparison lives
in exactly one place, and it is not here.
"""
from typing import Any, Dict

from app.api.client import ApiClient, TIMEOUT_FAST
from app.api.exceptions import ApiConnectionError, ApiError, ApiHttpError
from core.logging_setup import get_logger

log = get_logger("updates.api")


class UpdateApiService:
    """Client for the backend's desktop-release endpoint."""

    def __init__(self, api_client: ApiClient) -> None:
        self.api_client = api_client

    def get_latest_version(self) -> Dict[str, Any]:
        """The latest published release, and whether this client is behind it.

        Returns the backend's payload unchanged::

            {"latest_version": "1.1.0" | None,
             "download_url": str | None,
             "release_notes_url": str | None,
             "update_available": bool,
             "client_version": str | None}

        `latest_version` is None when the deployment has not been told what
        the current release is. That is an honest unknown and the caller must
        treat it as "nothing to announce" -- never as an update.
        """
        try:
            response = self.api_client.get("/desktop/latest-version", timeout=TIMEOUT_FAST)
            return response.json()
        except ApiHttpError as exc:
            if exc.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            log.warning("update check failed: HTTP %s", exc.status_code)
            raise ApiError(
                f"Update check failed (HTTP {exc.status_code}).",
                status_code=exc.status_code,
            )
        except ApiConnectionError:
            raise ApiError("Could not check for updates: network error.")
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"Could not check for updates: {exc}")

    def report_install_outcome(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """Tell the backend how an update attempt ended.

        `report` is the payload `UpdateService` builds: the client event id
        (what makes a retry one row on the server), the target and previous
        versions, the outcome, the failure category, a short detail, and the
        instant with the client's own clock beside it so the server places it
        by age. The backend answers with the stored row; a repeat is answered
        with the same row. This endpoint stays open to a client the backend
        is otherwise refusing, so a failed update can always be reported.
        """
        try:
            response = self.api_client.post(
                "/desktop/update-reports", json_data=report, timeout=TIMEOUT_FAST,
            )
            return response.json()
        except ApiHttpError as exc:
            if exc.status_code == 401:
                raise ApiError("Session expired. Please log in again.", status_code=401)
            log.warning("update report refused: HTTP %s", exc.status_code)
            raise ApiError(
                f"Update report refused (HTTP {exc.status_code}).",
                status_code=exc.status_code,
            )
        except ApiConnectionError:
            raise ApiError("Could not report the update: network error.")
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"Could not report the update: {exc}")
