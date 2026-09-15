"""
activity_service — Captures, aggregates and persists user activity.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from PySide6.QtCore import Signal

from background_services.activity.input_counter import InputEventCounter
from background_services.activity.input_probe import InputProbe
from background_services.activity.today_summary import ActivityTotals, totals_from_percent
from background_services.activity.unwanted_activity import UnwantedActivityMonitor
from core.service import LoopService


# Configurable Thresholds & Weights for normalized Activity % calculation
MAX_KEYBOARD_STROKES_PER_INTERVAL = 120
MAX_MOUSE_CLICKS_PER_INTERVAL = 30
MAX_MOUSE_MOVEMENTS_PER_INTERVAL = 400

KEYBOARD_WEIGHT = 0.40
MOUSE_CLICK_WEIGHT = 0.30
MOUSE_MOVEMENT_WEIGHT = 0.30


def calculate_activity_percentage(
    keyboard_strokes: int,
    mouse_clicks: int,
    mouse_movements: int,
    active_seconds: int = 0,
    window_seconds: int = 60
) -> int:
    """
    Normalised activity (0-100) from the input actually counted in a window.

    Each counter is scored against the volume a fully engaged minute is
    expected to produce, capped at 1.0, and the three are combined with the
    weights above.

    **Why this is not "seconds the user was present".** It used to be. The
    first branch of this function read

        if window_seconds > 0 and active_seconds >= 0:
            return round(active_seconds / window_seconds * 100)

    and `active_seconds` is a count, so `>= 0` is always true: the weighted
    model below it was unreachable, and every keystroke, click and movement
    the client captured was stored, uploaded — and ignored. What the number
    actually reported was "in how many sampled seconds did Windows say input
    had occurred in the last 1.5 seconds", which saturates: anyone moving a
    mouse continuously scores 100%, so a whole day of screenshots carried an
    identical, uninformative 100%. Worse, presence comes from
    `GetLastInputInfo`, which sees input the client's hooks cannot (an
    elevated window, for one) — so windows were recorded at 100% with zero
    counted events, a number nothing in the captured data could support.

    Counting resolves both: it cannot exceed what was observed, and it
    distinguishes reading from typing. `active_seconds` is still recorded
    alongside, because presence is a real measurement and is worth keeping —
    it just is not this number.

    Thresholds are per minute, so a partial window (the tail of a session) is
    scaled to its real length rather than being scored as if it were a full
    one — otherwise every session's last window would read near zero.
    """
    if window_seconds <= 0:
        return 0

    scale = window_seconds / 60.0
    k_max = MAX_KEYBOARD_STROKES_PER_INTERVAL * scale
    c_max = MAX_MOUSE_CLICKS_PER_INTERVAL * scale
    m_max = MAX_MOUSE_MOVEMENTS_PER_INTERVAL * scale

    k_score = min(max(0, keyboard_strokes) / k_max, 1.0) if k_max > 0 else 0.0
    c_score = min(max(0, mouse_clicks) / c_max, 1.0) if c_max > 0 else 0.0
    m_score = min(max(0, mouse_movements) / m_max, 1.0) if m_max > 0 else 0.0

    total_score = (
        k_score * KEYBOARD_WEIGHT
        + c_score * MOUSE_CLICK_WEIGHT
        + m_score * MOUSE_MOVEMENT_WEIGHT
    )
    return max(0, min(100, round(total_score * 100)))


class ActivityService(LoopService):
    """
    Samples user input once per second while a timer is running and flushes an
    aggregated window to storage every `WINDOW_SECONDS`.

    Two capture mechanisms feed the same window, and they answer different
    questions — they are not two sources for one number:

    - InputProbe (presence): "was the user there this second, and did the
      pointer move". No counters; see its docstring for why it no longer has
      any.
    - InputEventCounter: the single source of keystroke/click/movement
      counts, for the backend's time_entry_activity columns, for the
      activity percentage, and for the watched-key tallies the
      unwanted-activity rules consume. Its listeners run ONLY between
      start_tracker() and stop_tracker(). On a platform where the probe is
      unsupported (macOS) but the counter works, a second with any counted
      event is treated as active, so the percentage works there too instead
      of reading unmeasured.

    Signals:
        activity_window_recorded(dict)  — one completed window
        activity_percent_changed(int)   — current session percentage
        unwanted_activity_alert(str)    — user-facing warning to display
    """

    name = "activity"

    activity_window_recorded = Signal(dict)
    activity_percent_changed = Signal(int)
    unwanted_activity_alert = Signal(str)

    SAMPLE_INTERVAL_MS = 1000
    WINDOW_SECONDS = 60

    def __init__(self, runtime, cache, parent=None) -> None:
        super().__init__(runtime, parent)
        self._cache = cache
        self._probe = InputProbe()
        self._monitor = UnwantedActivityMonitor(
            on_event=self._on_unwanted_event,
            on_alert=self.unwanted_activity_alert.emit,
        )
        self._counter = InputEventCounter(watch_keys=self._monitor.watch_keys)
        self.interval_ms = self.SAMPLE_INTERVAL_MS

        self._entry_id: Optional[int] = None
        #: The timer session's own stable key. Windows sampled before the
        #: backend has issued an entry id are written against it and adopted
        #: later -- see `_flush_window` and `bind_entry_id`.
        self._client_op: Optional[str] = None
        self._tracking = False
        self._window_start: Optional[str] = None
        self._sampled = 0
        self._active = 0
        self._key_events = 0
        self._mouse_events = 0
        self._keyboard_strokes = 0
        self._mouse_clicks = 0
        self._mouse_movements = 0
        self._held_events: list = []

    @property
    def supported(self) -> bool:
        return self._probe.supported

    def idle_seconds(self) -> Optional[float]:
        """Seconds since the last system-wide keyboard or mouse input.

        `None` when the platform cannot measure it. Exposed here because this
        service already owns the only input-observation path in the process:
        idle detection reads this instead of installing global listeners of
        its own, so there is still exactly one place system input is watched.
        """
        return self._probe.idle_seconds()

    def current_percent(self) -> int:
        return calculate_activity_percentage(
            self._keyboard_strokes,
            self._mouse_clicks,
            self._mouse_movements,
            self._active,
            max(1, self._sampled)
        )

    def live_window_totals(self) -> ActivityTotals:
        """
        The window currently being sampled, as an addable weighted total.

        This is the only measurement that exists nowhere else: it has not been
        flushed to the local cache, so it cannot have been uploaded either.
        The dashboard adds it to the persisted totals to keep TODAY'S ACTIVITY
        moving between the once-a-minute window flushes, without any risk of
        counting the same seconds twice.

        Reads plain integer counters, so it is safe to call from the GUI
        thread on every tick.
        """
        if not self._tracking or self._sampled <= 0:
            return ActivityTotals()
        return totals_from_percent(self.current_percent(), self._sampled)

    def percent_for_entry(self, entry_id: int) -> int:
        try:
            return self._cache.get_activity_percent_for_entry(entry_id)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read activity for entry %s", entry_id)
            return 0

    def _reset_window(self) -> None:
        self._window_start = datetime.now(timezone.utc).isoformat()
        self._sampled = 0
        self._active = 0
        self._key_events = 0
        self._mouse_events = 0
        self._keyboard_strokes = 0
        self._mouse_clicks = 0
        self._mouse_movements = 0

    def _flush_window(self) -> None:
        """Write the sampled window to the local queue and start a new one.

        A window with no entry id yet is still written, against this
        session's `client_op`; the adoption binds it once the backend issues
        the id. Holding it open instead is what used to happen, and it broke
        an offline session in two ways: `_sampled` grew past WINDOW_SECONDS
        for as long as the session stayed unattributed, so the whole session
        landed as ONE sample -- measured at `window_seconds = 7200` for two
        hours offline, which the backend refuses outright (its schema caps a
        window at 3600) so the session's activity was never stored at all --
        and everything measured so far lived only in memory, where a crash
        took it.

        Without a client_op there is nothing that could ever adopt the row,
        so the window is held rather than written somewhere it can never be
        attributed. That is the same rule `AppUsageService._flush_segment`
        applies, for the same reason.
        """
        if self._sampled <= 0 or self._window_start is None:
            self._reset_window()
            return

        if self._entry_id is None and not self._client_op:
            self.log.debug(
                "holding a %ds activity window: no entry id and no session key "
                "to attribute it with",
                self._sampled,
            )
            return

        act_percent = self.current_percent()
        record = {
            "time_entry_id": self._entry_id,
            "window_start": self._window_start,
            "window_seconds": self._sampled,
            "active_seconds": self._active,
            "key_events": self._key_events,
            "mouse_events": self._mouse_events,
            "keyboard_strokes": self._keyboard_strokes,
            "mouse_clicks": self._mouse_clicks,
            "mouse_movements": self._mouse_movements,
            "activity_percent": act_percent,
        }
        try:
            self._cache.save_activity_sample(
                time_entry_id=self._entry_id,
                window_start=self._window_start,
                window_seconds=self._sampled,
                active_seconds=self._active,
                key_events=self._key_events,
                mouse_events=self._mouse_events,
                keyboard_strokes=self._keyboard_strokes,
                mouse_clicks=self._mouse_clicks,
                mouse_movements=self._mouse_movements,
                activity_percent=act_percent,
                client_op=self._client_op,
            )
        except Exception:  # noqa: BLE001
            self.log.exception("could not persist activity window")
        else:
            self.log.info(
                "activity window: %d/%ds active (%d%%, keys=%d, clicks=%d, moves=%d) for entry %s",
                self._active, self._sampled, act_percent,
                self._keyboard_strokes, self._mouse_clicks, self._mouse_movements,
                self._entry_id,
            )
            self.activity_window_recorded.emit(record)
        self._reset_window()

    def start_tracker(self, session: Dict[str, Any]) -> None:
        self._entry_id = session.get("entry_id")
        self._client_op = session.get("client_op")
        self._tracking = True
        self._reset_window()
        self._held_events = []
        self._monitor.start_session()
        counting = self._counter.start()
        self.log.info(
            "activity capture started for entry %s (probe supported=%s, "
            "counting=%s)",
            self._entry_id, self._probe.supported, counting,
        )

    def bind_entry_id(self, entry_id: int) -> None:
        """
        Attach a backend entry id that arrived after tracking began.

        Windows already written against this session's `client_op` belong to
        the same entry, so they are adopted here — the same treatment
        `AppUsageService.bind_entry_id` gives a segment measured before the
        entry existed. Unwanted-activity events detected in that gap are
        written now for the same reason.

        This covers a session that is still running. A start confirmed
        through the durable queue may land after the session has stopped, and
        `SyncService._adopt_session_telemetry` covers that; both are
        idempotent, and both are required.
        """
        self._entry_id = entry_id
        self._adopt_queued_windows(entry_id)
        self._persist_held_events()

    def _adopt_queued_windows(self, entry_id: int) -> None:
        if not self._client_op:
            return
        try:
            adopted = self._cache.bind_activity_samples_to_entry(
                self._client_op, entry_id
            )
        except Exception:  # noqa: BLE001
            self.log.exception(
                "could not bind buffered activity windows to entry %s", entry_id
            )
        else:
            if adopted:
                self.log.info(
                    "bound %d buffered activity window(s) to entry %s",
                    adopted, entry_id,
                )

    def stop_tracker(self) -> None:
        self._flush_window()
        self._persist_held_events()
        self._counter.stop()
        self._monitor.stop_session()
        self._tracking = False
        self._entry_id = None
        self._client_op = None
        self._window_start = None
        self._held_events = []

    # ── Unwanted activity ─────────────────────────────────────────────────────

    def _on_unwanted_event(self, record: dict) -> None:
        """One detection occurrence from the rule engine: queue it (and its
        deduction, on every deduct_after-th occurrence) for upload, or hold
        it until the backend has issued an entry id."""
        if self._entry_id is None:
            self._held_events.append(record)
            return
        self._persist_event(self._entry_id, record)

    def _persist_held_events(self) -> None:
        if not self._held_events or self._entry_id is None:
            return
        held, self._held_events = self._held_events, []
        for record in held:
            self._persist_event(self._entry_id, record)

    def _persist_event(self, entry_id: int, record: dict) -> None:
        try:
            self._cache.save_unwanted_activity(
                record_id=record["client_event_id"],
                time_entry_id=entry_id,
                activity_type=record["activity_type"],
                key_or_action=record["key_or_action"],
                occurrence_count=record["occurrence_count"],
                alerted=record["alerted"],
                alert_count=record["alert_count"],
                recorded_at=record["recorded_at"],
            )
            if record.get("deduction_seconds", 0) > 0:
                self._cache.save_adjustment(
                    record_id=f"adj-{record['client_event_id']}",
                    time_entry_id=entry_id,
                    adjustment_seconds=-int(record["deduction_seconds"]),
                    reason=(
                        f"Unwanted activity rule '{record['activity_type']}' "
                        f"({record['key_or_action']}) reached occurrence "
                        f"{record['occurrence_index']}"
                    ),
                    source_activity_type=record["activity_type"],
                    source_key_or_action=record["key_or_action"],
                    source_client_event_id=record["client_event_id"],
                    recorded_at=record["recorded_at"],
                )
        except Exception:  # noqa: BLE001
            self.log.exception("could not persist unwanted-activity event")

    def tick(self) -> Optional[int]:
        if not self._tracking:
            return self.SAMPLE_INTERVAL_MS

        if self._entry_id is None:
            session = self.runtime.timer.active_session() or {}
            entry_id = session.get("entry_id")
            if self._client_op is None:
                self._client_op = session.get("client_op")
            if entry_id is not None:
                # Adopt through the same path as bind_entry_id, so an id
                # noticed here rather than delivered to us still releases the
                # windows already queued against this session's client_op.
                self.bind_entry_id(entry_id)

        counts = self._counter.snapshot_and_reset()
        counted_any = (
            counts["keystrokes"] > 0 or counts["clicks"] > 0 or counts["movements"] > 0
        )
        self._keyboard_strokes += counts["keystrokes"]
        self._mouse_clicks += counts["clicks"]
        self._mouse_movements += counts["movements"]

        sample = self._probe.sample(self.SAMPLE_INTERVAL_MS / 1000.0 * 1.5)
        if sample is None:
            if not self._counter.supported:
                return self.SAMPLE_INTERVAL_MS
            sample = {"active": counted_any, "mouse": counts["movements"] > 0}

        self._sampled += 1
        if sample.get("active") or counted_any:
            self._active += 1
        # Which *kind* of input a second contained comes from the counter,
        # the only thing in this process that sees individual events. The
        # probe contributes presence and pointer movement and nothing else:
        # it used to report "present and the cursor did not move" as a
        # keyboard second, which measured nothing about the keyboard.
        if sample.get("mouse") or counts["clicks"] > 0 or counts["movements"] > 0:
            self._mouse_events += 1
        if counts["keystrokes"] > 0:
            self._key_events += 1

        self._monitor.feed(self._counter.drain_watched_presses())

        self.heartbeat()

        if self._sampled >= self.WINDOW_SECONDS:
            self._flush_window()
        elif self._sampled % 5 == 0:
            self.activity_percent_changed.emit(self.current_percent())

        return self.SAMPLE_INTERVAL_MS

    def on_stop(self, timeout_ms: int) -> bool:
        self._flush_window()
        self._persist_held_events()
        self._counter.stop()
        return super().on_stop(timeout_ms)
