"""
schedule_service — fetches the administrator's notification schedule.

An administrator decides, from the web, which wellbeing reminders the desktop
shows and when, and may add custom notifications (see
docs/DESKTOP_NOTIFICATIONS.md). This service is the *only* thing in the client
that asks the backend for that schedule. It does not schedule, show or time
anything: `WellbeingService` stays the one scheduler and reads the snapshot
held here on each of its ticks, so it stays free of network work.

Design constraints, all from DO_NOT_DO.md:

**A light poll, and a stream to hear sooner.** A `LoopService` that asks about
every half minute (jittered, so a fleet does not arrive in lockstep) -- the
cadence of the maintenance notice and the dashboard's sync probe, and for the
same reason: an administrator who sets a notification for a minute from now
expects it at that minute, and a desktop can only honour that if it has heard of
it by then. At the five-minute cadence this had, a notification saved at
11:12:13 for 11:13 was shown at 11:15:17. The request is one small read; nothing
is written unless the version changed. It holds while signed out, while the
network service says the backend is not worth trying, and waits much longer
against a backend that does not have the endpoint (404).

On top of that sits the **change stream** (`_listen`): while signed in and
online, the rest of the time is spent reading `GET /desktop-notifications/stream`,
which the backend keeps open and writes a comment line to every two seconds
until the schedule's `version` moves. The first event ends the listening and the
next tick fetches the schedule -- an administrator's change, or a message they
pushed, is on the screen within a second or two instead of at the next poll. It
is the same loop thread, so there is no second owner of the schedule and no new
thread. The stream is an accelerator and never the only path:

  * the schedule is still fetched with the plain request (on every event, at
    least every `LEVEL_CHECK_MS`, and as the poll whenever the stream is not
    working), so a missed event costs minutes, not correctness;
  * it ends within a ping of being asked to stop (`stopping`), of signing out,
    or of the network going -- the loop checks each time the server speaks,
    which is why the server pings (a request held open and silent could not be
    abandoned);
  * a failing stream is retried with doubling, jittered backoff, a server
    without the route (404) is not asked again for minutes, and a stream that
    ends again at once counts as failing -- never a reconnect loop;
  * an event is edge-triggered: one per change of version, and the same version
    signalled twice with no progress in between is a failure, not a trigger.

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

**It tells the scheduler what time it is.** The schedule's times are IST wall-clock
times, and the scheduler compares them with this machine's clock. A machine whose
clock -- or whose time zone, set wrong while the displayed time was corrected by hand
-- is out by minutes or hours would then show "13:30" at the wrong moment, or beyond
its ten-minute grace and not at all. Every response carries the server's clock
(`server_time`); the offset between it and this machine's (`clock_offset_seconds`,
measured at the midpoint of the request so the round trip cancels) is what
`WellbeingService` adds to its own reading. The server is the reference, as for tracked
time (docs/TIMING_MODEL.md). No offset is known until the first answer, and the
scheduler then uses the machine's clock as it always did.

**It does not let the scheduler run on a guess.** With nothing persisted -- the first
sign-in on a machine, or the first after a sign-out, which wipes it -- the schedule is
None until the backend answers, and a daily reminder already inside its grace window
would be shown at its *default* time even though the administrator moved or switched it
off. So `ready` stays False after sign-in until the first answer arrives (a failure
counts: the desktop is then genuinely working from defaults), or `SETTLE_SECONDS`
pass, so an offline start is not held for ever.

Not exported from `background_services.notifications.__init__`: this module
reads the wellbeing catalogue for the set of known keys, and that package
imports `notifications` itself, so re-exporting it there would make the two
import each other.
"""
from __future__ import annotations

import json
import random
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from datetime import time as dtime
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

from app.api.exceptions import ApiError
from app.desktop_notifications.service import NotificationScheduleApiService
from background_services.network import NetworkState
from background_services.wellbeing.reminders import (
    DAILY_REMINDERS,
    INTERVAL_REMINDERS,
    MAX_PER_HOUR_RANGE,
    PUSH_TTL_SECONDS,
)
from PySide6.QtCore import Signal

from core.service import LoopService, ServiceState
from core.time_format import parse_utc
from core.validation import validate_time_of_day

#: Where the last good schedule is kept, so a start while offline can use it.
SCHEDULE_STATE_KEY = "notifications.schedule"

#: Whether *this member* is shown the notifications at all (User Management's
#: "Desktop notifications" switch). Kept apart from the schedule, which is the
#: deployment's and survives a sign-out: this one is the signed-in user's, so it is
#: wiped with the rest of `app_state` at logout and the next person starts from
#: their own profile.
MEMBER_ENABLED_STATE_KEY = "notifications.member_enabled"

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

    `at` is a time of day (IST), or None. A daily reminder with None keeps the
    catalogue's own time. An interval reminder with None repeats on its
    cadence; one with a time is shown once a day at that time instead.
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
class PushedNotification:
    """A message an administrator pushed to every desktop, to show once, now.

    `expires_at_mono` is a `time.monotonic()` instant: the push's age was
    measured by the server (`seconds_ago`), and this desktop turns that into its
    own clock once, when it parses the response, so it never compares its wall
    clock with the server's. Past it, the push is not shown.
    """

    id: str
    title: str
    message: str
    expires_at_mono: float


@dataclass(frozen=True)
class NotificationSchedule:
    """One complete, immutable reading of the administrator's schedule."""

    version: int
    builtin: Mapping[str, BuiltinSetting]
    custom: Tuple[CustomNotification, ...]
    #: How many notifications the desktop may show in a rolling hour, as the
    #: administrator set it. None when the response did not carry a usable
    #: number (an older backend, or one this desktop does not understand): the
    #: scheduler then uses its own default. Never fabricated here.
    max_per_hour: Optional[int] = None
    #: Messages pushed in the last few minutes, oldest first. Not part of what is
    #: persisted or compared: a push is shown once, soon after it is sent, and a
    #: restart must never replay one.
    pushes: Tuple[PushedNotification, ...] = field(default=(), compare=False)

    @property
    def builtin_off(self) -> int:
        return sum(1 for setting in self.builtin.values() if not setting.enabled)

    def to_json(self) -> Dict[str, Any]:
        """The normalised form that is persisted (and parses back to an equal
        schedule). Never contains anything but the contract's own fields."""
        return {
            "version": self.version,
            "max_per_hour": self.max_per_hour,
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


def _parse_push(item: Any, now_mono: float) -> Optional[PushedNotification]:
    if not isinstance(item, dict):
        return None
    ident, title, message = item.get("id"), item.get("title"), item.get("message")
    if not all(isinstance(v, str) and v.strip() for v in (ident, title, message)):
        return None
    age = item.get("seconds_ago")
    if isinstance(age, bool) or not isinstance(age, (int, float)) or age < 0 or age > PUSH_TTL_SECONDS:
        return None  # too old to be worth showing, or not a usable age
    return PushedNotification(
        id=ident, title=title, message=message, expires_at_mono=now_mono + (PUSH_TTL_SECONDS - age),
    )


def parse_schedule(raw: Any, now_mono: Optional[float] = None) -> Optional[NotificationSchedule]:
    """Parse a response (or a persisted record) into a snapshot.

    Returns None for anything that is not usable as a whole -- not a mapping,
    no integer `version`, or `builtin`/`custom` present but not lists. Items
    inside that are malformed are dropped one by one; a dropped built-in entry
    means that reminder keeps its default (on, every day). `now_mono` is the
    monotonic clock a pushed message's age is anchored to. Never raises.
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

        # An unusable limit does not spoil the rest of the schedule: it is
        # simply not applied, and the scheduler keeps its own default.
        low, high = MAX_PER_HOUR_RANGE
        max_per_hour = raw.get("max_per_hour")
        if isinstance(max_per_hour, bool) or not isinstance(max_per_hour, int) or not low <= max_per_hour <= high:
            max_per_hour = None

        pushes = []
        pushed_raw = raw.get("pushes", [])
        if isinstance(pushed_raw, list):
            now = _time.monotonic() if now_mono is None else now_mono
            seen_pushes = set()
            for item in pushed_raw:
                pushed = _parse_push(item, now)
                if pushed is not None and pushed.id not in seen_pushes:
                    seen_pushes.add(pushed.id)
                    pushes.append(pushed)

        return NotificationSchedule(
            version=version,
            builtin=MappingProxyType(builtin),
            custom=tuple(custom),
            max_per_hour=max_per_hour,
            pushes=tuple(pushes),
        )
    except Exception:  # noqa: BLE001 -- a malformed payload must never raise out
        return None


# ── The service ───────────────────────────────────────────────────────────────


class NotificationScheduleService(LoopService):
    """Keeps the current notification schedule, as an immutable snapshot."""

    name = "notification_schedule"

    #: An administrator changed something this desktop polls for -- a member
    #: switch, the screens captured, the screenshot size or quality, a privacy
    #: rule -- and it concerns this desktop. Emitted on the service's own thread,
    #: on the edge only (one per new configuration revision that concerns us);
    #: the runtime connects it to the services that own those polls, which then
    #: re-read now instead of at their next three-minute interval. The stream is
    #: the accelerator and never the only path: the polls remain the guarantee.
    config_changed = Signal()

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
    #: The change stream (see `_listen`). The server writes a ping every two
    #: seconds; silence for this long means the stream is dead -- a connection
    #: that dropped without saying so, or a proxy holding the pings back.
    STREAM_READ_TIMEOUT_S = 7.0
    #: How long one tick listens before it comes back to check on things (the
    #: server ends a stream after about 25 seconds on its own).
    STREAM_LISTEN_S = 22.0
    #: A stream that ends sooner than this without an event has not worked: the
    #: server is shutting down, or something in between is cutting it. Counted as
    #: a failure so that it backs off rather than reconnecting in a loop.
    STREAM_HEALTHY_AFTER_S = 5.0
    #: Fetch the whole schedule at least this often even while the stream is
    #: quiet -- insurance against a missed event, not the way changes arrive.
    LEVEL_CHECK_MS = 5 * 60 * 1000
    #: After an event, before fetching, so a burst of saves lands as one fetch.
    EVENT_FETCH_DELAY_MS = 300
    #: A stream that fails is not retried for this long, doubling per consecutive
    #: failure up to the cap, jittered. The poll carries on meanwhile.
    STREAM_RETRY_BASE_MS = 5 * 1000
    STREAM_RETRY_MAX_MS = 5 * 60 * 1000
    #: Against a backend without the route (404): an older deployment, a normal
    #: state during a rollout.
    STREAM_ABSENT_MS = 5 * 60 * 1000
    #: After sign-in, how long the scheduler waits for the first answer when it
    #: has no schedule at all (see `ready`). Long enough for a slow first
    #: request on a poor connection, short enough that a machine that cannot
    #: reach the backend still gets its reminders from the defaults.
    SETTLE_SECONDS = 20.0
    #: A clock offset beyond this is not a skewed clock, it is a response this
    #: desktop cannot use (a year-old timestamp, a wrong year): ignored.
    MAX_CLOCK_OFFSET_SECONDS = 366 * 24 * 3600
    #: An offset this large is worth a line in the log; smaller ones are the
    #: ordinary difference between two clocks and a round trip.
    CLOCK_SKEW_NOTICE_SECONDS = 60
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
        # The change stream. All of it is touched only on this service's thread
        # (`check_now` sets one flag from outside, which is safe).
        #: The newest `version` a fetch reported, as the server wrote it -- even
        #: when the rest of that response was unusable, so a response this
        #: desktop cannot parse is not signalled again and again.
        self._since = 0
        self._need_fetch = True
        self._last_fetch_at: Optional[float] = None
        self._stream_blocked_until = 0.0
        self._stream_failures = 0
        self._last_event_version: Optional[int] = None
        #: The desktop configuration revision this desktop last heard of, sent
        #: with every stream so the backend can say when it moves. 0 on start:
        #: the first stream of a process then answers at once (the revision is
        #: almost never 0) and the three configuration polls are made fresh --
        #: one small read each, and the right answer from the first minute.
        self._config_revision = 0
        self._last_event_config: Optional[int] = None
        self._stream_up = False
        #: Whether the backend has answered (or failed to) since this service
        #: started: the end of "nothing is known yet" (see `ready`).
        self._answered = False
        #: Monotonic instant at which "nothing is known yet" stops holding the
        #: scheduler, set the first time it is asked while signed in.
        self._settle_deadline: Optional[float] = None
        #: Seconds to add to this machine's clock to get the server's. Rebound
        #: whole on each answer; 0.0 until the first.
        self._clock_offset = 0.0
        #: Whether a large offset is already in the log (reported on the edge).
        self._skew_logged = False
        #: Whether this member is shown the notifications (see
        #: `MEMBER_ENABLED_STATE_KEY`). On until the server says otherwise, and only
        #: an explicit `False` turns it off, so a missing or damaged answer can never
        #: silence anyone. A plain bool, rebound whole: safe to read from any thread.
        self._member_enabled = True
        #: Whether anything fresher than the persisted copy has been heard (the
        #: sign-in profile, or an answer), so the persisted copy read at the first
        #: tick never overwrites it.
        self._member_known = False
        #: What is on disk for it, so it is written once per change and only from
        #: this service's thread, never from the GUI thread that seeds it.
        self._member_enabled_stored: Optional[bool] = None

    # ── Read side ─────────────────────────────────────────────────────────────

    @property
    def notifications_enabled(self) -> bool:
        """Whether the signed-in member is shown the notifications at all.

        False holds the whole reminder scheduler (built-in, custom and pushed);
        see `WellbeingService._gate_reason`. Functional alerts -- a permission
        that is needed, an update, an idle prompt -- are not the scheduler's and
        never depend on it.
        """
        return self._member_enabled

    def apply_user_profile(self, user_data: Optional[Dict[str, Any]]) -> None:
        """Seed the switch from a `/auth/me` payload (sign-in, session verification).

        Without this a member who is switched off would be shown reminders until
        the first poll answered -- and, on a machine another member used before,
        would inherit *that* member's setting meanwhile. A profile that does not
        carry the field (a backend older than the switch) means on.
        """
        if not isinstance(user_data, dict):
            return
        user = user_data.get("user") if isinstance(user_data.get("user"), dict) else user_data
        self._set_member_enabled(user.get("notifications_enabled", True))

    def _set_member_enabled(self, value: Any) -> None:
        """Adopt the switch. Edge-triggered; only a real boolean is believed."""
        if not isinstance(value, bool):
            return
        self._member_known = True
        if value == self._member_enabled:
            return
        self._member_enabled = value
        self.log.info(
            "NOTIFICATIONS_SWITCH enabled=%s; desktop notifications are turned %s for this account",
            value, "on" if value else "off",
        )
        # The scheduler may be idle (held by the switch) or sleeping until a
        # deadline: ask it to look again now. `wake` is queued and thread-safe.
        self._wake_scheduler()

    @property
    def schedule(self) -> Optional[NotificationSchedule]:
        """The current snapshot, or None when nothing has been fetched or
        persisted. Safe from any thread; the object is immutable."""
        return self._schedule

    @property
    def ready(self) -> bool:
        """Whether a consumer may trust `schedule` (including its being None).

        False until the first tick has read the persisted record, so the scheduler
        does not run -- and show a reminder an administrator switched off --
        against a snapshot that is merely not loaded yet. And, with nothing
        persisted, False until the backend has answered (or failed to) after
        sign-in, or `SETTLE_SECONDS` have passed: `None` means "every reminder on
        at its default time", which is wrong for a first sign-in on a machine whose
        administrator has moved or switched one off. A service that failed to start,
        or is stopping, never holds the scheduler up.
        """
        if self.stopping or self.state == ServiceState.FAILED:
            return True
        if not self._loaded:
            return False
        if self._schedule is not None or self._answered:
            return True
        # Nothing is known and the backend has not yet been asked, or has not
        # answered: wait for it, but not for ever. Asked of the scheduler only
        # while signed in, so the deadline starts at the sign-in.
        now = self._monotonic()
        if self._settle_deadline is None:
            self._settle_deadline = now + self.SETTLE_SECONDS
        return now >= self._settle_deadline

    @property
    def clock_offset_seconds(self) -> float:
        """Seconds to add to this machine's clock to read the server's, as of the
        last answer; 0.0 before the first. Positive means this machine is behind.
        Safe from any thread (a float, rebound whole)."""
        return self._clock_offset

    # ── Requests from the runtime ─────────────────────────────────────────────

    def check_now(self) -> None:
        """Poll now rather than at the next interval (safe from any thread)."""
        self._need_fetch = True
        self.wake()

    def reset_session(self) -> None:
        """Logout wipes `app_state`, the persisted copy included. The schedule
        itself is deployment-wide, not the user's, so the snapshot stays; it is
        simply written again on the next successful poll. The member's own switch
        is the user's, not the deployment's: it goes with them, and the next person
        to sign in is seeded from their own profile."""
        self._persisted = False
        self._member_enabled = True
        self._member_known = False
        self._member_enabled_stored = None

    # ── The loop ──────────────────────────────────────────────────────────────

    def _should_hold(self) -> Optional[str]:
        api_client = getattr(self.runtime, "api_client", None)
        if api_client is None or not getattr(api_client, "access_token", None):
            return "not signed in"
        network = getattr(self.runtime, "network", None)
        if network is not None and network.network_state not in NetworkState.WORTH_TRYING:
            return f"network {network.network_state}"
        return None

    def _monotonic(self) -> float:
        """The clock the stream's timings run on (a seam for tests)."""
        return _time.monotonic()

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

        now = self._monotonic()
        streaming = now >= self._stream_blocked_until
        if (
            self._need_fetch
            or not streaming
            or self._last_fetch_at is None
            or (now - self._last_fetch_at) * 1000 >= self.LEVEL_CHECK_MS
        ):
            sent = datetime.now(timezone.utc)
            try:
                payload = self._api.get_schedule()
            except ApiError as exc:
                # Quietly: the last good schedule stands. 404 is an older
                # deployment without the endpoint -- a long wait; anything else
                # is a blip, and the next poll is soon.
                self.log.debug("notification schedule poll failed: %s", exc)
                self.heartbeat(success=False)
                self._note_answered()
                absent = getattr(exc, "status_code", None) == 404
                return self._jittered(self.ABSENT_INTERVAL_MS if absent else self.RETRY_INTERVAL_MS)

            received = datetime.now(timezone.utc)
            self._last_fetch_at = now
            self._need_fetch = False
            self.heartbeat()
            if self.state == ServiceState.DEGRADED:
                self._set_state(ServiceState.RUNNING)
            self._note_version(payload)
            self._note_clock(payload, sent, received)
            self._apply(payload, source="fetched")
            self._note_answered()
            if not streaming:
                return self._jittered(self.POLL_INTERVAL_MS)

        return self._listen()

    def _note_answered(self) -> None:
        """The backend has been heard from (or has failed to be): the scheduler may
        stop waiting for it (`ready`). It is woken to say so only if it can have been
        waiting -- with a schedule in hand it never was, and a version edge has already
        woken it."""
        if self._answered:
            return
        self._answered = True
        if self._schedule is None:
            self._wake_scheduler()

    def _note_clock(self, payload: Any, sent: datetime, received: datetime) -> None:
        """Measure this machine's clock against the server's from one answer.

        `server_time` was stamped somewhere between the request leaving and the
        answer arriving; the midpoint is the best guess at when, and the error
        is at most half the round trip. An answer without a usable `server_time`
        (an older backend) changes nothing: the last offset stands, and 0.0 if
        there never was one.
        """
        server = parse_utc(payload.get("server_time")) if isinstance(payload, dict) else None
        if server is None:
            return
        offset = (server - (sent + (received - sent) / 2)).total_seconds()
        if abs(offset) > self.MAX_CLOCK_OFFSET_SECONDS:
            return
        self._clock_offset = offset
        large = abs(offset) >= self.CLOCK_SKEW_NOTICE_SECONDS
        if large != self._skew_logged:
            self._skew_logged = large
            if large:
                self.log.warning(
                    "NOTIFICATION_CLOCK_SKEW: this computer's clock is %.0f s %s the server's; "
                    "reminders are timed by the server's clock",
                    abs(offset), "behind" if offset > 0 else "ahead of",
                )
            else:
                self.log.info("NOTIFICATION_CLOCK_SKEW cleared: this computer's clock agrees with the server's")

    # ── The change stream ─────────────────────────────────────────────────────

    def _note_version(self, payload: Any) -> None:
        version = payload.get("version") if isinstance(payload, dict) else None
        if isinstance(version, int) and not isinstance(version, bool) and version >= 0:
            self._since = version

    @staticmethod
    def _event_version(line: str) -> Optional[int]:
        """The version in a `data:` line of the stream, or None for anything
        else (pings, blank separators, the `event:` line, a line this desktop
        does not understand)."""
        event = NotificationScheduleService._event_payload(line)
        return None if event is None else event[0]

    @staticmethod
    def _event_payload(line: str) -> Optional[Tuple[int, Optional[int], bool]]:
        """`(version, config, concerns)` from a `data:` line, or None for anything
        else. `config` is the configuration revision the event carries (None from a
        backend that does not send one) and `concerns` whether a configuration
        change since the one this desktop last heard concerns it."""
        if not isinstance(line, str) or not line.startswith("data:"):
            return None
        try:
            data = json.loads(line[5:].strip())
            version = data.get("version")
        except (ValueError, AttributeError):
            return None
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            return None
        config = data.get("config")
        if isinstance(config, bool) or not isinstance(config, int) or config < 0:
            config = None
        return version, config, data.get("concerns") is True

    def _listen(self) -> int:
        """Read the change stream until something happens. Returns the delay
        before the next tick.

        Blocks this service's thread -- which has nothing else to do -- for up to
        `STREAM_LISTEN_S`, but only ever until the server's next ping (two
        seconds): between lines it checks whether it has been asked to stop,
        whether the user signed out and whether the network went, and leaves.
        """
        started = self._monotonic()
        deadline = started + self.STREAM_LISTEN_S
        spoke = False
        try:
            with self._api.open_stream(
                self._since, read_timeout=self.STREAM_READ_TIMEOUT_S, since_config=self._config_revision,
            ) as stream:
                for line in stream.lines():
                    spoke = True
                    self.heartbeat()
                    event = self._event_payload(line)
                    if event is not None:
                        return self._on_event(*event)
                    if self.stopping:
                        return self.POLL_INTERVAL_MS
                    if self._should_hold() is not None:
                        return self.HOLD_INTERVAL_MS
                    if self._monotonic() >= deadline:
                        break
        except ApiError as exc:
            return self._stream_failed(f"{exc}", getattr(exc, "status_code", None))

        if not spoke or self._monotonic() - started < self.STREAM_HEALTHY_AFTER_S:
            # Opened and closed with nothing, or cut short: not a stream that works.
            return self._stream_failed("the stream ended at once", None)
        self._stream_working()
        return 0

    def _stream_working(self) -> None:
        self._stream_failures = 0
        if not self._stream_up:
            self._stream_up = True
            self.log.info("NOTIFICATION_STREAM_UP: changes arrive as they are made")

    def _on_event(self, version: int, config: Optional[int] = None, concerns: bool = False) -> int:
        """The server said the schedule's version moved, or the desktop
        configuration revision did. Fetch the schedule -- once -- and, when a
        configuration change concerns this desktop, tell the owners of the
        configuration polls to re-read now."""
        config_moved = config is not None and config != self._config_revision
        if version == self._last_event_version and not config_moved:
            # The same change signalled again although the fetch since then
            # should have caught up with it: the response is not getting
            # through, and signalling it again would be a loop.
            return self._stream_failed(f"version {version} signalled again with no progress", None)
        self._last_event_version = version
        self._stream_working()
        # The schedule is fetched again only if its version moved. A configuration
        # change alone ends every desktop's stream, and a hundred desktops each
        # fetching an unchanged schedule for it would be a hundred reads for nothing.
        if version != self._since:
            self._need_fetch = True
        if config_moved:
            self._config_revision = config
            self.log.info(
                "NOTIFICATION_STREAM_EVENT: configuration revision %d (%s this desktop), schedule version %d",
                config, "concerns" if concerns else "not for", version,
            )
            if concerns:
                self.config_changed.emit()
        else:
            self.log.info("NOTIFICATION_STREAM_EVENT: the schedule moved to version %d", version)
        return self.EVENT_FETCH_DELAY_MS

    def _stream_failed(self, reason: str, status: Optional[int]) -> int:
        """Stop using the stream for a while and carry on with the poll."""
        now = self._monotonic()
        was_up, self._stream_up = self._stream_up, False
        if status in (404, 405, 501):
            self._stream_blocked_until = now + self._jittered(self.STREAM_ABSENT_MS) / 1000.0
            self.log.info("NOTIFICATION_STREAM_UNAVAILABLE: the backend has no change stream; polling instead")
        else:
            self._stream_failures += 1
            wait_ms = min(self.STREAM_RETRY_MAX_MS, self.STREAM_RETRY_BASE_MS * 2 ** (self._stream_failures - 1))
            self._stream_blocked_until = now + self._jittered(wait_ms) / 1000.0
            (self.log.warning if was_up or self._stream_failures == 1 else self.log.info)(
                "NOTIFICATION_STREAM_FAILED: %s; polling, next try in about %d s (failure %d)",
                reason, wait_ms // 1000, self._stream_failures,
            )
        self._need_fetch = True
        return self._jittered(self.POLL_INTERVAL_MS)

    # ── Applying ──────────────────────────────────────────────────────────────

    def _apply(self, payload: Any, *, source: str) -> bool:
        """Adopt `payload` if it parses and its version is new. Returns
        whether the snapshot changed. Never raises."""
        # The member's switch is read from every answer, whatever its version: it
        # is the caller's, not the schedule's, and an administrator can change it
        # without the schedule's version moving. An answer without it (a backend
        # older than the switch) says nothing.
        if isinstance(payload, dict) and "enabled" in payload:
            self._set_member_enabled(payload["enabled"])
            self._persist_member_enabled()
        parsed = parse_schedule(payload, now_mono=self._monotonic())
        if parsed is None:
            self.log.warning("notification schedule ignored: the response was not usable")
            return False

        current = self._schedule
        if current is not None and parsed.version == current.version:
            # The level, not an edge. Only repair a persisted copy that
            # logout removed -- and adopt a pushed message this snapshot does
            # not carry. Pushes are not persisted, so after a restart the
            # persisted snapshot is at the live version with no pushes, and a
            # message pushed while the desktop was closed (still inside its
            # ten-minute lifetime) arrived on the first fetch and was dropped
            # here as "the level". The same happened to a member switched back
            # on while a push was live: the server had emptied `pushes` for
            # them at the same version. Only the pushes are new; the scheduler
            # is woken and nothing is logged as a schedule change.
            if not self._persisted:
                self._persist(parsed)
            known = {p.id for p in current.pushes}
            if any(p.id not in known for p in parsed.pushes):
                self._schedule = parsed
                self._wake_scheduler()
                return True
            return False

        self._schedule = parsed
        self.log.info(
            "NOTIFICATION_SCHEDULE_APPLIED version=%d builtin_off=%d custom=%d max_per_hour=%s source=%s",
            parsed.version, parsed.builtin_off, len(parsed.custom), parsed.max_per_hour, source,
        )
        if source == "fetched":
            self._persist(parsed)
        self._wake_scheduler()
        return True

    def _persist_member_enabled(self) -> None:
        """Remember the member's switch across a restart, once per change."""
        if self._cache is None or self._member_enabled_stored == self._member_enabled:
            return
        try:
            self._cache.save_app_state(MEMBER_ENABLED_STATE_KEY, self._member_enabled)
            self._member_enabled_stored = self._member_enabled
        except Exception:  # noqa: BLE001
            self.log.exception("could not persist the notifications switch")

    def _load_member_enabled(self) -> None:
        """The switch as it was when this machine last heard, so a start while
        offline does not show a switched-off member a reminder."""
        try:
            stored = self._cache.load_app_state(MEMBER_ENABLED_STATE_KEY)
        except Exception:  # noqa: BLE001
            self.log.exception("could not read the persisted notifications switch")
            return
        if isinstance(stored, bool):
            self._member_enabled_stored = stored
            if not self._member_known:
                self._member_enabled = stored

    def _load_persisted(self) -> None:
        if self._cache is None:
            return
        self._load_member_enabled()
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
            "NOTIFICATION_SCHEDULE_APPLIED version=%d builtin_off=%d custom=%d max_per_hour=%s source=persisted",
            parsed.version, parsed.builtin_off, len(parsed.custom), parsed.max_per_hour,
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
    "PushedNotification",
    "SCHEDULE_STATE_KEY",
    "parse_schedule",
]
