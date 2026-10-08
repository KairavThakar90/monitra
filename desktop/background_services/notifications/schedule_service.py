"""
schedule_service — fetches the administrator's notification schedule.

An administrator decides, from the web, which wellbeing reminders the desktop
shows and when, and may add custom notifications (see
docs/DESKTOP_NOTIFICATIONS.md). This service is the *only* thing in the client
that asks the backend for that schedule. It does not schedule, show or time
anything: `WellbeingService` stays the one scheduler and reads the snapshot
held here on each of its ticks, so it stays free of network work.

Design constraints, all from DO_NOT_DO.md:

**A light poll, not a connection.** A `LoopService` that asks about every half
minute (jittered, so a fleet does not arrive in lockstep) -- the cadence of the
maintenance notice and the dashboard's sync probe, and for the same reason: an
administrator who sets a notification for a minute from now expects it at that
minute, and a desktop can only honour that if it has heard of it by then. At
the five-minute cadence this had, a notification saved at 11:12:13 for 11:13
was shown at 11:15:17 (the desktop had polled at 11:09:54, and its next poll
was 11:15:16). The request is one small read; nothing is written unless the
version changed. It holds while signed out, while the network service says the
backend is not worth trying, and waits much longer against a backend that does
not have the endpoint (404). It has no retry loop of its own: a failed poll is
silence, and the next one is simply the next interval.

**Failure keeps the last good schedule.** A poll that fails, or answers
something that does not parse, changes nothing. The schedule is never cleared
because the backend could not be reached and never replaced by a guess.

**Edge-triggered.** The backend answers the same `version` on every poll. The
snapshot is replaced, logged and persisted only when the version *differs* from
the one applied -- one `NOTIFICATION_SCHEDULE_APPLIED` line per change, never
per poll -- and the scheduler is woken on that edge only.

**An immutable snapshot, swapped atomically.** `schedule` returns a frozen
`NotificationSchedule` (or None). Readers on other threads never see a
half-built one: the service builds the new one completely and then rebinds one
attribute.

**Persisted, so an offline start works.** The last good schedule is written to
`app_state` through the cache and read back on the service's first tick, on its
own thread. With nothing fetched and nothing persisted the snapshot is None,
and consumers use the defaults (every built-in reminder on, no custom
notifications): nothing is ever fabricated.

**Parsed defensively.** The response crosses a trust boundary. Unknown
built-in keys, malformed items and wrong types are dropped; an unusable
response as a whole is ignored. Nothing raises out of this module. Times go
through the shared validation catalogue (`validate_time_of_day`); there is no
second time-of-day rule here.

Not exported from `background_services.notifications.__init__`: this module
reads the wellbeing catalogue for the set of known keys, and that package
imports `notifications` itself, so re-exporting it there would make the two
import each other.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import time as dtime
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

from app.api.exceptions import ApiError
from app.desktop_notifications.service import NotificationScheduleApiService
from background_services.network import NetworkState
from background_services.wellbeing.reminders import DAILY_REMINDERS, INTERVAL_REMINDERS
from core.service import LoopService, ServiceState
from core.validation import validate_time_of_day

#: Where the last good schedule is kept, so a start while offline can use it.
SCHEDULE_STATE_KEY = "notifications.schedule"

#: Every weekday, Monday = 0 ... Sunday = 6 (`datetime.weekday()`).
ALL_WEEKDAYS: frozenset = frozenset(range(7))

#: The built-in keys this desktop knows. Anything else in a response is a
#: reminder from a newer desktop and is ignored.
KNOWN_BUILTIN_KEYS = frozenset(
    [r.key for r in INTERVAL_REMINDERS] + [r.key for r in DAILY_REMINDERS]
)


# ── The snapshot ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BuiltinSetting:
    """What an administrator set for one built-in reminder.

    `at` is the daily reminder's time of day (IST) or None to keep the
    catalogue's own; it is always None for an interval reminder.
    """

    key: str
    enabled: bool
    at: Optional[dtime]
    weekdays: frozenset


@dataclass(frozen=True)
class CustomNotification:
    """An administrator-written notification, shown daily at `at` (IST)."""

    id: str
    title: str
    message: str
    at: dtime
    weekdays: frozenset


@dataclass(frozen=True)
class NotificationSchedule:
    """One complete, immutable reading of the administrator's schedule."""

    version: int
    builtin: Mapping[str, BuiltinSetting]
    custom: Tuple[CustomNotification, ...]

    @property
    def builtin_off(self) -> int:
        return sum(1 for setting in self.builtin.values() if not setting.enabled)

    def to_json(self) -> Dict[str, Any]:
        """The normalised form that is persisted (and parses back to an equal
        schedule). Never contains anything but the contract's own fields."""
        return {
            "version": self.version,
            "builtin": [
                {
                    "key": s.key,
                    "enabled": s.enabled,
                    "time": None if s.at is None else s.at.strftime("%H:%M"),
                    "weekdays": sorted(s.weekdays),
                }
                for s in self.builtin.values()
            ],
            "custom": [
                {
                    "id": c.id,
                    "title": c.title,
                    "message": c.message,
                    "time": c.at.strftime("%H:%M"),
                    "weekdays": sorted(c.weekdays),
                }
                for c in self.custom
            ],
        }


def _parse_time(value: Any) -> Optional[dtime]:
    result = validate_time_of_day(value)
    if not result.ok:
        return None
    hours, minutes = result.value.split(":")
    return dtime(int(hours), int(minutes))


def _parse_weekdays(value: Any) -> Optional[frozenset]:
    """A non-empty list of integers 0-6, or None. Booleans are not integers."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    days = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 6:
            return None
        days.add(item)
    return frozenset(days)


def _parse_builtin(item: Any) -> Optional[BuiltinSetting]:
    if not isinstance(item, dict):
        return None
    key = item.get("key")
    if not isinstance(key, str) or key not in KNOWN_BUILTIN_KEYS:
        return None
    enabled = item.get("enabled")
    if not isinstance(enabled, bool):
        return None
    weekdays = _parse_weekdays(item.get("weekdays"))
    if weekdays is None:
        return None
    raw_time = item.get("time")
    at = None if raw_time is None else _parse_time(raw_time)
    if raw_time is not None and at is None:
        return None
    return BuiltinSetting(key=key, enabled=enabled, at=at, weekdays=weekdays)


def _parse_custom(item: Any) -> Optional[CustomNotification]:
    if not isinstance(item, dict):
        return None
    ident, title, message = item.get("id"), item.get("title"), item.get("message")
    if not all(isinstance(v, str) and v.strip() for v in (ident, title, message)):
        return None
    at = _parse_time(item.get("time"))
    weekdays = _parse_weekdays(item.get("weekdays"))
    if at is None or weekdays is None:
        return None
    return CustomNotification(id=ident, title=title, message=message, at=at, weekdays=weekdays)


def parse_schedule(raw: Any) -> Optional[NotificationSchedule]:
    """Parse a response (or a persisted record) into a snapshot.

    Returns None for anything that is not usable as a whole -- not a mapping,
    no integer `version`, or `builtin`/`custom` present but not lists. Items
    inside that are malformed are dropped one by one; a dropped built-in entry
    means that reminder keeps its default (on, every day). Never raises.
    """
    try:
        if not isinstance(raw, dict):
            return None
        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            return None
        builtin_raw = raw.get("builtin", [])
        custom_raw = raw.get("custom", [])
        if not isinstance(builtin_raw, list) or not isinstance(custom_raw, list):
            return None

        builtin: Dict[str, BuiltinSetting] = {}
        for item in builtin_raw:
            setting = _parse_builtin(item)
            if setting is not None and setting.key not in builtin:
                builtin[setting.key] = setting

        custom = []
        seen = set()
        for item in custom_raw:
            notification = _parse_custom(item)
            if notification is not None and notification.id not in seen:
                seen.add(notification.id)
                custom.append(notification)

        return NotificationSchedule(
            version=version,
            builtin=MappingProxyType(builtin),
            custom=tuple(custom),
        )
    except Exception:  # noqa: BLE001 -- a malformed payload must never raise out
        return None


# ── The service ───────────────────────────────────────────────────────────────


class NotificationScheduleService(LoopService):
    """Keeps the current notification schedule, as an immutable snapshot."""

    name = "notification_schedule"

    #: A change an administrator makes reaches an open desktop within about
    #: half a minute (at worst this plus the jitter), so a notification set a
    #: minute ahead is known before its time. The same cadence as
    #: `MaintenanceService.CHECK_INTERVAL_MS`.
    POLL_INTERVAL_MS = 30 * 1000
    #: After a failed poll. Not longer than a poll: one lost request must not
    #: leave the desktop deaf to a change for minutes.
    RETRY_INTERVAL_MS = 30 * 1000
    #: Against a backend without the endpoint (404) -- an older deployment,
    #: a normal state during a rollout. Nothing to hear about for a long time,
    #: so a fleet does not ask a missing route every half minute.
    ABSENT_INTERVAL_MS = 5 * 60 * 1000
    #: While signed out or offline. No request is made; it is a cheap check
    #: for the moment the hold ends (login and the network's recovery edge
    #: also wake the loop directly).
    HOLD_INTERVAL_MS = 30 * 1000
    #: Soon after sign-in, but not on top of startup.
    FIRST_POLL_DELAY_MS = 5 * 1000
    interval_ms = POLL_INTERVAL_MS
    error_interval_ms = 60 * 1000
    #: One TIMEOUT_FAST request is the whole blocking budget of a tick; the
    #: persisted-record write is small but can wait on the busy timeout.
    stop_timeout_ms = 12_000

    def __init__(
        self,
        runtime,
        schedule_api: NotificationScheduleApiService,
        cache=None,
        parent=None,
    ) -> None:
        super().__init__(runtime, parent)
        self._api = schedule_api
        self._cache = cache if cache is not None else getattr(runtime, "cache", None)
        #: The current snapshot. Rebound whole, never mutated.
        self._schedule: Optional[NotificationSchedule] = None
        self._first_tick_done = False
        #: Whether the persisted record is known to match `_schedule`.
        #: Cleared on logout, which wipes `app_state`.
        self._persisted = False
        #: Set once the persisted schedule has been read (or found absent),
        #: so the scheduler knows whether it is working from the real thing.
        self._loaded = False

    # ── Read side ─────────────────────────────────────────────────────────────

    @property
    def schedule(self) -> Optional[NotificationSchedule]:
        """The current snapshot, or None when nothing has been fetched or
        persisted. Safe from any thread; the object is immutable."""
        return self._schedule

    @property
    def ready(self) -> bool:
        """Whether a consumer may trust `schedule` (including its being None).

        False only between the service being asked to start and its first
        tick having read the persisted record, so the scheduler does not run
        its first tick -- and show a reminder an administrator switched off --
        against a snapshot that is merely not loaded yet. A service that
        failed to start, or is stopping, never holds the scheduler up.
        """
        return self._loaded or self.stopping or self.state == ServiceState.FAILED

    # ── Requests from the runtime ─────────────────────────────────────────────

    def check_now(self) -> None:
        """Poll now rather than at the next interval (safe from any thread)."""
        self.wake()

    def reset_session(self) -> None:
        """Logout wipes `app_state`, the persisted copy included. The schedule
        itself is deployment-wide, not the user's, so the snapshot stays; it is
        simply written again on the next successful poll."""
        self._persisted = False

    # ── The loop ──────────────────────────────────────────────────────────────

    def _should_hold(self) -> Optional[str]:
        api_client = getattr(self.runtime, "api_client", None)
        if api_client is None or not getattr(api_client, "access_token", None):
            return "not signed in"
        network = getattr(self.runtime, "network", None)
        if network is not None and network.network_state not in NetworkState.WORTH_TRYING:
            return f"network {network.network_state}"
        return None

    def tick(self) -> Optional[int]:
        if not self._first_tick_done:
            self._first_tick_done = True
            self._load_persisted()
            self._loaded = True
            # The scheduler may be waiting for this (see `ready`).
            self._wake_scheduler()
            return self.FIRST_POLL_DELAY_MS

        hold_reason = self._should_hold()
        if hold_reason:
            self.log.debug("notification schedule poll held: %s", hold_reason)
            return self.HOLD_INTERVAL_MS

        try:
            payload = self._api.get_schedule()
        except ApiError as exc:
            # Quietly: the last good schedule stands. 404 is an older
            # deployment without the endpoint -- a long wait; anything else
            # is a blip, and the next poll is soon.
            self.log.debug("notification schedule poll failed: %s", exc)
            self.heartbeat(success=False)
            absent = getattr(exc, "status_code", None) == 404
            return self._jittered(self.ABSENT_INTERVAL_MS if absent else self.RETRY_INTERVAL_MS)

        self.heartbeat()
        if self.state == ServiceState.DEGRADED:
            self._set_state(ServiceState.RUNNING)
        self._apply(payload, source="fetched")
        return self._jittered(self.POLL_INTERVAL_MS)

    # ── Applying ──────────────────────────────────────────────────────────────

    def _apply(self, payload: Any, *, source: str) -> bool:
        """Adopt `payload` if it parses and its version is new. Returns
        whether the snapshot changed. Never raises."""
        parsed = parse_schedule(payload)
        if parsed is None:
            self.log.warning("notification schedule ignored: the response was not usable")
            return False

        current = self._schedule
        if current is not None and parsed.version == current.version:
            # The level, not an edge. Only repair a persisted copy that
            # logout removed.
            if not self._persisted:
                self._persist(parsed)
            return False

        self._schedule = parsed
        self.log.info(
            "NOTIFICATION_SCHEDULE_APPLIED version=%d builtin_off=%d custom=%d source=%s",
            parsed.version, parsed.builtin_off, len(parsed.custom), source,
        )
        if source == "fetched":
            self._persist(parsed)
        self._wake_scheduler()
        return True

    def _load_persisted(self) -> None:
        if self._cache is None:
            return
        try:
            stored = self._cache.load_app_state(SCHEDULE_STATE_KEY)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read the persisted notification schedule")
            return
        if stored is None:
            return
        parsed = parse_schedule(stored)
        if parsed is None:
            self.log.warning("the persisted notification schedule is unusable; ignoring it")
            return
        self._schedule = parsed
        self._persisted = True
        self.log.info(
            "NOTIFICATION_SCHEDULE_APPLIED version=%d builtin_off=%d custom=%d source=persisted",
            parsed.version, parsed.builtin_off, len(parsed.custom),
        )

    def _persist(self, schedule: NotificationSchedule) -> None:
        if self._cache is None:
            return
        try:
            self._cache.save_app_state(SCHEDULE_STATE_KEY, schedule.to_json())
            self._persisted = True
        except Exception:  # noqa: BLE001
            self.log.exception("could not persist the notification schedule")

    def _wake_scheduler(self) -> None:
        """Ask the wellbeing scheduler to look again now. Called on an edge
        only (first load, a changed version), so a new custom notification is
        in its next-deadline computation without waiting out its idle sleep.
        `wake` is queued and safe from this thread."""
        wellbeing = getattr(self.runtime, "wellbeing", None)
        wake = getattr(wellbeing, "wake", None)
        if wake is None:
            return
        try:
            wake()
        except Exception:  # noqa: BLE001
            self.log.exception("could not wake the reminder scheduler")

    @staticmethod
    def _jittered(interval_ms: int) -> int:
        return int(interval_ms * (0.85 + random.random() * 0.3))


__all__ = [
    "ALL_WEEKDAYS",
    "BuiltinSetting",
    "CustomNotification",
    "NotificationSchedule",
    "NotificationScheduleService",
    "SCHEDULE_STATE_KEY",
    "parse_schedule",
]
