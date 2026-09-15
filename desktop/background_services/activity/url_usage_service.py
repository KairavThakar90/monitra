"""
url_usage_service — Tracks real-time browser URL usage while a timer is active.

Subclasses `LoopService` (QObject + moveToThread + QTimer) running on a
dedicated service thread. Driven by `TimerService` lifecycle hooks.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from background_services.activity.day_split import split_by_ist_day
from core.service import LoopService
from tracking.active_window import get_active_window_details
from tracking.browsers import UrlSource, get_browser_manager


class UrlUsageService(LoopService):
    """Samples active browser URL usage while a tracking session is active."""

    name = "url_usage"

    #: How often the active browser URL is sampled.
    SAMPLE_INTERVAL_MS = 2000
    #: Maximum continuous session duration before flushing an incremental segment.
    MAX_SEGMENT_SECONDS = 60.0
    #: Maximum allowed gap between samples before treating it as a resume after sleep.
    MAX_OBSERVATION_GAP_SECONDS = 30.0
    #: Idle cadence when not tracking.
    IDLE_INTERVAL_MS = 5000

    def __init__(self, runtime, cache, parent=None) -> None:
        super().__init__(runtime, parent)
        self._cache = cache
        self.interval_ms = self.IDLE_INTERVAL_MS
        self._browser_manager = get_browser_manager()

        self._entry_id: Optional[int] = None
        #: The timer session's stable key, so a browser session measured
        #: before the backend issued an entry id can be adopted later rather
        #: than discarded. See `AppUsageService` for the full reasoning.
        self._client_op: Optional[str] = None
        self._tracking = False

        # Current URL session state
        self._current_browser: Optional[str] = None
        self._current_domain: Optional[str] = None
        self._current_url: Optional[str] = None
        self._current_title: Optional[str] = None
        self._current_client_event_id: Optional[str] = None
        self._current_is_private: Optional[bool] = None
        self._session_start: Optional[float] = None
        self._last_observed: Optional[float] = None
        self._session_recorded_at: Optional[str] = None
        #: Browsers already reported as having no readable URL, so a browser
        #: that never exposes one costs a single log line per session rather
        #: than one every two seconds for the length of the session.
        self._unavailable_reported: set = set()

    # ── Tracker interface (driven by TimerService) ────────────────────────────

    def start_tracker(self, session: Dict[str, Any]) -> None:
        self._entry_id = session.get("entry_id")
        self._client_op = session.get("client_op")
        self._tracking = True
        self._reset_session()
        self._unavailable_reported.clear()
        self.log.info("browser URL usage tracking started for entry %s", self._entry_id)
        self.wake()

    def bind_entry_id(self, entry_id: int) -> None:
        """Attribute in-progress URL sessions to a late-arriving entry id."""
        self._entry_id = entry_id
        if self._client_op:
            try:
                adopted = self._cache.bind_url_usage_to_entry(self._client_op, entry_id)
            except Exception:  # noqa: BLE001
                self.log.exception("could not bind buffered URL usage to entry %s", entry_id)
            else:
                if adopted:
                    self.log.info(
                        "bound %d buffered browser session(s) to entry %s", adopted, entry_id
                    )

    def _observe_now(self) -> None:
        """Count the time up to this instant as observed.

        The user really was on the current page until they stopped the
        timer, so the part-interval since the last sample belongs to it --
        but only if the loop was alive across it. An unobserved gap (sleep,
        stall) is still discarded rather than claimed.
        """
        base = self._last_observed if self._last_observed is not None else self._session_start
        if base is None:
            return
        now = time.monotonic()
        if now - base <= self.MAX_OBSERVATION_GAP_SECONDS:
            self._last_observed = now

    def stop_tracker(self) -> None:
        self._observe_now()
        self._flush_session()
        self._tracking = False
        self._entry_id = None
        self._client_op = None
        self._reset_session()
        self.log.info("browser URL usage tracking stopped")

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def _reset_session(self) -> None:
        self._current_browser = None
        self._current_domain = None
        self._current_url = None
        self._current_title = None
        self._current_client_event_id = None
        self._current_is_private = None
        self._session_start = None
        self._last_observed = None
        self._session_recorded_at = None

    def _flush_session(self) -> None:
        # As in `AppUsageService._flush_segment`: a session with no entry id
        # yet is still written, against this session's `client_op`, and
        # adopted when the id arrives. Only one with neither is unattributable
        # and therefore not written at all.
        if (
            self._session_start is None or
            self._last_observed is None or
            not self._current_browser or
            not self._current_domain
        ):
            return
        if self._entry_id is None and not self._client_op:
            return

        duration = int(self._last_observed - self._session_start)
        if duration <= 0:
            return

        client_event_id = self._current_client_event_id or str(uuid.uuid4())
        started_at = self._session_recorded_at or datetime.now(timezone.utc).isoformat()
        # One row per IST calendar day the session touched, so a tab left open
        # over midnight is not credited entirely to the day it was opened on.
        chunks = split_by_ist_day(started_at, duration)
        try:
            for index, (chunk_start, chunk_seconds) in enumerate(chunks):
                # Each row needs its own idempotency key -- `client_event_id`
                # is UNIQUE locally and is what the backend de-duplicates on,
                # so reusing one key for both halves would drop the second.
                # The suffix is derived, not random, so a retry of the same
                # split produces the same keys and stays idempotent.
                self._cache.save_url_usage(
                    time_entry_id=self._entry_id,
                    browser_name=self._current_browser,
                    domain=self._current_domain,
                    url=self._current_url,
                    page_title=self._current_title,
                    duration_seconds=chunk_seconds,
                    recorded_at=chunk_start,
                    client_event_id=(
                        client_event_id if len(chunks) == 1
                        else f"{client_event_id}-d{index}"
                    ),
                    client_op=self._client_op,
                    is_private=self._current_is_private,
                )
        except Exception:  # noqa: BLE001
            self.log.exception("could not persist browser URL usage session")
        else:
            self.log.debug(
                "url usage session flushed: %s - %s (%ds)",
                self._current_browser, self._current_domain, duration
            )

    def _begin_session(self, observation, now: float) -> None:
        self._current_browser = observation.browser_name
        self._current_domain = observation.domain
        self._current_url = observation.url
        self._current_title = observation.page_title
        self._current_is_private = observation.is_private
        self._current_client_event_id = str(uuid.uuid4())
        self._session_start = now
        self._last_observed = now
        self._session_recorded_at = datetime.now(timezone.utc).isoformat()

    # ── Loop tick ─────────────────────────────────────────────────────────────

    def tick(self) -> Optional[int]:
        if not self._tracking:
            return self.IDLE_INTERVAL_MS

        if self._entry_id is None:
            session = self.runtime.timer.active_session() or {}
            self._entry_id = session.get("entry_id")
            if self._client_op is None:
                self._client_op = session.get("client_op")

        app_name, window_title, _, _, hwnd = get_active_window_details()
        now = time.monotonic()

        # Check if current active window is a supported browser
        observation = self._browser_manager.extract_browser_info(
            app_name, window_title, hwnd or 0
        )

        if observation is None:
            # User switched to a non-browser application (e.g. VS Code, Slack)
            self._close_open_session()
            self.heartbeat()
            return self.SAMPLE_INTERVAL_MS

        if not observation.has_url:
            # A supported browser whose URL genuinely cannot be read right
            # now -- Firefox without accessibility enabled, a browser window
            # with no address bar, or an omnibox mid-typing. The previous
            # behaviour was to record the sentinel domain "unknown-domain",
            # which the UI then rendered as the link https://unknown-domain.
            # Recording nothing is the honest outcome: the time is still
            # captured as application usage against the browser itself.
            key = (observation.browser_name, observation.is_private)
            if key not in self._unavailable_reported:
                self._unavailable_reported.add(key)
                self.log.info(
                    "URL_UNAVAILABLE browser=%s private=%s source=%s; recording "
                    "application usage only, with no URL invented for it",
                    observation.browser_name,
                    "unknown" if observation.is_private is None
                    else str(observation.is_private).lower(),
                    UrlSource.UNAVAILABLE,
                )
            self._close_open_session()
            self.heartbeat()
            return self.SAMPLE_INTERVAL_MS

        if self._session_start is None:
            self._begin_session(observation, now)
            self.heartbeat()
            return self.SAMPLE_INTERVAL_MS

        # A page is identified by its URL, not by its title: a title that
        # changes on its own (a chat gaining a name, an unread-count prefix)
        # is the same page and must not split the segment, or a busy tab
        # would produce a stream of duplicate two-second records.
        # The private state is part of what identifies the segment. Opening the
        # same URL in an incognito window is a different browsing session from
        # the normal-window one, and merging them would attribute private time
        # to a normal-window record (and the reverse). A transition either way
        # therefore closes the open segment and starts a new one, exactly as a
        # URL change does.
        same_page = (
            observation.browser_name == self._current_browser and
            observation.domain == self._current_domain and
            observation.url == self._current_url and
            observation.is_private == self._current_is_private
        )

        gap = now - (self._last_observed or now)
        elapsed = now - self._session_start

        if gap > self.MAX_OBSERVATION_GAP_SECONDS:
            # Sleep, hibernation or a stalled loop: everything after
            # `_last_observed` is unobserved time and is not ours to claim.
            # Flush what was actually measured, then start clean.
            self._flush_session()
            self._begin_session(observation, now)
        elif not same_page or elapsed >= self.MAX_SEGMENT_SECONDS:
            self._last_observed = now
            self._flush_session()
            self._begin_session(observation, now)
        else:
            self._last_observed = now
            if observation.page_title:
                self._current_title = observation.page_title

        self.heartbeat()
        return self.SAMPLE_INTERVAL_MS

    def _close_open_session(self) -> None:
        """Finalise any in-progress segment and forget it."""
        if self._session_start is not None:
            self._flush_session()
            self._reset_session()

    def on_stop(self, timeout_ms: int) -> bool:
        self._observe_now()
        self._flush_session()
        return super().on_stop(timeout_ms)
