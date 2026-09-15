"""
app_usage_service — Tracks which application the user is working in.

Replaces `tracking/app_usage_tracker.py`. The behaviour (sample the foreground
window, aggregate contiguous use into segments, flush segments to storage for
the sync service to upload) is preserved; the threading is not.

The previous tracker ran its sampling QTimer on the GUI thread and wrote to
SQLite from there, every two seconds, for the entire duration of a tracking
session — competing with the sync consumer for the same shared connection.
Here the sampling loop owns a service thread, and storage gives that thread its
own connection, so neither can stall the UI.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from background_services.activity.day_split import split_by_ist_day
from core.service import LoopService
from tracking.active_window import get_active_window_details
from tracking.app_identity import ClassificationStatus, resolve_application


class AppUsageService(LoopService):
    """Samples the foreground window while a tracking session is active."""

    name = "app_usage"

    #: How often the foreground window is sampled.
    SAMPLE_INTERVAL_MS = 2000
    #: A single unbroken segment is flushed at least this often, so a long
    #: session in one application still produces incremental records rather
    #: than one enormous record that is lost if the process dies.
    MAX_SEGMENT_SECONDS = 60.0
    #: Idle cadence when nothing is being tracked.
    IDLE_INTERVAL_MS = 5000
    #: A gap larger than this between samples means the machine was asleep or
    #: the loop was stalled. That time was never observed, so it is not
    #: claimed: the segment is closed at the last real observation.
    MAX_OBSERVATION_GAP_SECONDS = 30.0

    def __init__(self, runtime, cache, parent=None) -> None:
        super().__init__(runtime, parent)
        self._cache = cache
        self.interval_ms = self.IDLE_INTERVAL_MS

        self._entry_id: Optional[int] = None
        self._client_op: Optional[str] = None
        self._tracking = False
        self._current_app: Optional[str] = None
        self._current_title: Optional[str] = None
        self._segment_start: Optional[float] = None
        self._last_observed: Optional[float] = None
        self._segment_recorded_at: Optional[str] = None
        #: Applications already reported as unclassified, so an executable
        #: the catalogue does not know costs one log line per session rather
        #: than one every two seconds for the length of the session.
        self._unclassified_reported: set = set()

    # ── Tracker interface (driven by TimerService) ────────────────────────────

    def start_tracker(self, session: Dict[str, Any]) -> None:
        self._entry_id = session.get("entry_id")
        # The timer session's own stable key. Segments captured before the
        # backend has issued an entry id are stored against it and adopted
        # later -- see `_flush_segment` and `bind_entry_id`.
        self._client_op = session.get("client_op")
        self._tracking = True
        self._reset_segment()
        self._unclassified_reported.clear()
        self.log.info("application usage tracking started for entry %s", self._entry_id)
        self.wake()

    def bind_entry_id(self, entry_id: int) -> None:
        """Attribute the in-progress segment to a late-arriving entry id."""
        self._entry_id = entry_id
        # Segments already written against this session's client_op belong to
        # the same entry. Adopting them here is what makes an offline start
        # keep its first minutes of application usage instead of discarding
        # them -- the same treatment `bind_screenshots_to_entry` gives a
        # capture taken before the entry existed.
        if self._client_op:
            try:
                adopted = self._cache.bind_app_usage_to_entry(self._client_op, entry_id)
            except Exception:  # noqa: BLE001
                self.log.exception("could not bind buffered application usage to entry %s", entry_id)
            else:
                if adopted:
                    self.log.info(
                        "bound %d buffered application usage segment(s) to entry %s",
                        adopted, entry_id,
                    )

    def _observe_now(self) -> None:
        """Count the time up to this instant as observed.

        Used when the tracking session ends: the user really was in the
        current application right up to the moment they stopped the timer, so
        the trailing part-interval since the last sample belongs in the
        segment. It is only claimed if the loop was actually alive over that
        stretch -- an unobserved gap (sleep, stall) is still discarded.
        """
        base = self._last_observed if self._last_observed is not None else self._segment_start
        if base is None:
            return
        now = time.monotonic()
        if now - base <= self.MAX_OBSERVATION_GAP_SECONDS:
            self._last_observed = now

    def stop_tracker(self) -> None:
        self._observe_now()
        self._flush_segment()
        self._tracking = False
        self._entry_id = None
        self._client_op = None
        self._reset_segment()
        self.log.info("application usage tracking stopped")

    # ── Segments ──────────────────────────────────────────────────────────────

    def _reset_segment(self) -> None:
        self._current_app = None
        self._current_title = None
        self._segment_start = None
        self._last_observed = None
        self._segment_recorded_at = None

    def _flush_segment(self) -> None:
        # A segment with no entry id yet is still written, against this
        # session's `client_op`; `bind_entry_id` adopts it once the backend
        # issues the id. Dropping it here is what used to lose the whole of
        # an offline start's application usage -- time that was genuinely
        # measured, against an application that was genuinely identified.
        # Without a client_op there is nothing to adopt it later, so a
        # segment that cannot ever be attributed is still not written.
        if self._segment_start is None or not self._current_app:
            return
        if self._entry_id is None and not self._client_op:
            return
        # Measured from the last sample that actually observed this
        # application, not from "now". Reading the clock at flush time
        # silently absorbed any gap since the last observation -- so a laptop
        # that slept for an hour with VS Code in front woke up and recorded
        # an hour of VS Code use that nobody performed.
        duration = int((self._last_observed or self._segment_start) - self._segment_start)
        if duration <= 0:
            return
        started_at = self._segment_recorded_at or datetime.now(timezone.utc).isoformat()
        # A segment that runs through midnight is stored as one row per
        # calendar day. Every reader dates a row by its start, so writing a
        # crossing segment whole would credit the whole of it to the day it
        # began on -- see background_services/activity/day_split.py.
        try:
            for chunk_start, chunk_seconds in split_by_ist_day(started_at, duration):
                self._cache.save_app_usage(
                    time_entry_id=self._entry_id,
                    application_name=self._current_app,
                    window_title=self._current_title,
                    duration_seconds=chunk_seconds,
                    recorded_at=chunk_start,
                    client_op=self._client_op,
                )
        except Exception:  # noqa: BLE001
            self.log.exception("could not persist application usage segment")
        else:
            self.log.debug("app usage segment: %s for %ds", self._current_app, duration)

    def _begin_segment(self, app_name: Optional[str], title: Optional[str]) -> None:
        self._current_app = app_name
        self._current_title = title
        self._segment_start = time.monotonic()
        self._last_observed = self._segment_start
        self._segment_recorded_at = datetime.now(timezone.utc).isoformat()

    # ── Loop ──────────────────────────────────────────────────────────────────

    def _identify(self) -> tuple[Optional[str], Optional[str]]:
        """The current foreground application's canonical name, and its title.

        The executable path is passed alongside the process name so that
        `resolve_application` can key on the binary -- the one identifier
        that is the same on both platforms and does not change when a
        display name is localized or a window is renamed.

        Returns ``(None, title)`` when nothing identifies the process. That
        is not an application called "unknown"; it is the absence of an
        observation, and the caller records nothing for it.
        """
        app_name, window_title, exe_path, _pid, _hwnd = get_active_window_details()
        identity = resolve_application(process_name=app_name, executable_path=exe_path)

        if identity.status == ClassificationStatus.PARTIALLY_CLASSIFIED:
            # Recorded under its real executable name, so the row is
            # diagnosable rather than anonymous. One line per session names
            # the program a maintainer would add to the catalogue.
            if identity.key not in self._unclassified_reported:
                self._unclassified_reported.add(identity.key)
                self.log.info(
                    "application %r is not in the identity catalogue (%s); "
                    "recording it under its executable name",
                    identity.name, identity.reason,
                )
        elif identity.status == ClassificationStatus.UNKNOWN:
            if identity.reason not in self._unclassified_reported:
                self._unclassified_reported.add(identity.reason)
                self.log.info(
                    "no foreground application could be identified (%s); "
                    "recording nothing for these samples", identity.reason,
                )

        return identity.name, window_title

    def tick(self) -> Optional[int]:
        if not self._tracking:
            return self.IDLE_INTERVAL_MS

        if self._entry_id is None:
            session = self.runtime.timer.active_session() or {}
            self._entry_id = session.get("entry_id")
            if self._client_op is None:
                self._client_op = session.get("client_op")

        app_name, window_title = self._identify()
        now = time.monotonic()

        if app_name is None:
            # Nothing identifiable is in the foreground. Close what was
            # measured up to the last real observation and wait: holding the
            # segment open would credit this stretch to whichever
            # application happened to be in front before it.
            if self._segment_start is not None:
                self._flush_segment()
                self._reset_segment()
            self.heartbeat()
            return self.SAMPLE_INTERVAL_MS

        if self._segment_start is None:
            self._begin_segment(app_name, window_title)
            return self.SAMPLE_INTERVAL_MS

        # A segment is bounded by the *application*, not by the window title.
        # Keying it on the title too meant that typing in an editor, or a tab
        # switch in a browser, closed one segment and opened another every
        # two seconds: a stream of duplicate 2-second rows for one unbroken
        # stretch of work, each one a separate row to store, sync and render.
        changed = app_name != self._current_app
        elapsed = now - self._segment_start
        gap = now - (self._last_observed or now)

        if gap > self.MAX_OBSERVATION_GAP_SECONDS:
            # Unobserved time (sleep/stall). Close at the last real sample
            # and start again from this one rather than claiming the gap.
            self._flush_segment()
            self._begin_segment(app_name, window_title)
            self.heartbeat()
            return self.SAMPLE_INTERVAL_MS

        # This sample observed the current application; record that before
        # any early return, or a held-open segment would look stalled and
        # trip the gap check above on the next tick.
        self._last_observed = now

        if self._entry_id is None and not self._client_op:
            # Neither a backend entry id nor a session key to adopt the
            # segment later, so it cannot be persisted at all. Hold it open
            # rather than closing one that would simply be dropped:
            # `_flush_segment` would discard it and `_begin_segment` would
            # reset the clock, losing the elapsed time entirely. With a
            # client_op present -- the normal offline case -- the segment is
            # written and bound when the start lands, so no hold is needed
            # and the usual cap still applies.
            if not changed:
                return self.SAMPLE_INTERVAL_MS

        if changed or elapsed >= self.MAX_SEGMENT_SECONDS:
            self._flush_segment()
            self._begin_segment(app_name, window_title)
        elif window_title and window_title != self._current_title:
            # Same application, new window title: keep the segment running
            # and let it carry the most recent title.
            self._current_title = window_title

        self.heartbeat()
        return self.SAMPLE_INTERVAL_MS

    def on_stop(self, timeout_ms: int) -> bool:
        self._observe_now()
        self._flush_segment()
        return super().on_stop(timeout_ms)
