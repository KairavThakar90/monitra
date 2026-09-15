"""
The reported defect, end to end: working with several browser tabs raised
"repeated inactive/unwanted activity" warnings and deducted time.

This exercises the real chain a user's keyboard drives --

    InputEventCounter  ->  drain_watched_presses()
                       ->  UnwantedActivityMonitor.feed()
                       ->  alert + event record + deduction

-- with the pynput callbacks invoked directly, exactly as its listener thread
would, so the test is deterministic and unaffected by anyone typing on the
machine running it.

Two measured defects put ordinary work over the rule's threshold:

* **OS auto-repeat.** Windows resends WM_KEYDOWN continuously while a key is
  held, and every one of them counted as a press. Measured on Windows 11:
  holding CTRL for about a second produced 30 presses -- twice the whole
  15-press threshold -- from one real press.
* **Chorded modifiers.** CTRL+T, CTRL+TAB, CTRL+W and CTRL+click are how
  anyone works with several tabs open. Ten such chords tallied 50 presses.

Both crossed the threshold, so the alert fired, an unwanted-activity row was
stored against the time entry, and every third occurrence queued a **ten
minute deduction** from time the user had genuinely worked.

The rule itself is unchanged and must stay effective: the last tests here are
the ones that would fail if the fix had simply turned detection off.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from background_services.activity.input_counter import InputEventCounter
from background_services.activity.unwanted_activity import (
    DEFAULT_RULES, UnwantedActivityMonitor,
)


CTRL = SimpleNamespace(name="ctrl_l")
TAB = SimpleNamespace(name="tab")
RULE = DEFAULT_RULES[0]


class _Harness:
    """A counter and monitor wired together the way ActivityService wires
    them, with the per-second drain that the service's tick performs."""

    def __init__(self):
        self.events = []
        self.alerts = []
        self.monitor = UnwantedActivityMonitor(
            on_event=self.events.append, on_alert=self.alerts.append
        )
        self.counter = InputEventCounter(watch_keys=self.monitor.watch_keys)
        self.monitor.start_session()
        self._clock = 0.0

    def tick(self):
        """One second of ActivityService's loop."""
        self._clock += 1.0
        self.monitor.feed(self.counter.drain_watched_presses(), now=self._clock)

    @property
    def deducted_seconds(self):
        return sum(e.get("deduction_seconds", 0) for e in self.events)


@pytest.fixture
def harness():
    return _Harness()


def _key(name):
    return SimpleNamespace(name=name)


def _char(char):
    return SimpleNamespace(char=char)


# ── What must no longer fire ─────────────────────────────────────────────────

def test_a_minute_of_browser_tab_work_raises_no_alert(harness):
    """
    The user's own report. Twenty CTRL shortcuts in a minute -- new tab,
    cycle tabs, close tab, copy, paste -- is unremarkable work, and is more
    than the rule's threshold if each chord is counted as a CTRL press.
    """
    partners = ([_key("tab")] * 12 + [_char("t"), _char("w"), _char("c"),
                                      _char("v"), _char("f"), _char("l"),
                                      _char("s"), _char("z")])
    for partner in partners:
        harness.counter._on_press(CTRL)
        harness.counter._on_press(CTRL)       # auto-repeat during the hold
        harness.counter._on_press(partner)
        harness.counter._on_release(partner)
        harness.counter._on_release(CTRL)
        harness.tick()

    assert harness.alerts == []
    assert harness.events == []
    assert harness.deducted_seconds == 0


def test_opening_many_tabs_with_ctrl_click_raises_no_alert(harness):
    """CTRL+click on twenty links: the other half of "multiple tabs"."""
    for _ in range(20):
        harness.counter._on_press(CTRL)
        harness.counter._on_click(0, 0, None, pressed=True)
        harness.counter._on_release(CTRL)
        harness.tick()

    assert harness.alerts == []
    assert harness.events == []


def test_holding_ctrl_while_reading_raises_no_alert(harness):
    """One press, held. It used to score 30 -- twice the threshold."""
    harness.counter._on_press(CTRL)
    for _ in range(60):
        harness.counter._on_press(CTRL)       # OS auto-repeat
        harness.tick()
    harness.counter._on_release(CTRL)
    harness.tick()

    assert harness.alerts == []
    assert harness.deducted_seconds == 0


def test_ctrl_zoom_raises_no_alert(harness):
    """CTRL+scroll, twenty times: zooming a page in and out."""
    for _ in range(20):
        harness.counter._on_press(CTRL)
        harness.counter._on_scroll(0, 0, 0, 1)
        harness.counter._on_release(CTRL)
        harness.tick()

    assert harness.alerts == []
    assert harness.events == []


def test_no_time_is_ever_deducted_for_ordinary_shortcut_use(harness):
    """
    The consequence that made this expensive: three occurrences queue a ten
    minute deduction against a real time entry. Sixty chords -- four times
    the threshold under the old counting -- must deduct nothing.
    """
    for _ in range(60):
        harness.counter._on_press(CTRL)
        harness.counter._on_press(TAB)
        harness.counter._on_release(TAB)
        harness.counter._on_release(CTRL)
        harness.tick()

    assert harness.deducted_seconds == 0


# ── What must still fire ─────────────────────────────────────────────────────

def test_mashing_ctrl_on_its_own_still_alerts(harness):
    """The rule's actual purpose: a key pressed repeatedly, alone, to fake
    presence. Nothing here excuses it, and nothing here is a shortcut."""
    for _ in range(RULE.threshold):
        harness.counter._on_press(CTRL)
        harness.counter._on_release(CTRL)
    harness.tick()

    assert len(harness.alerts) == 1
    assert harness.events[0]["key_or_action"] == "ctrl"
    assert harness.events[0]["occurrence_count"] >= RULE.threshold


def test_mashing_still_deducts_on_the_configured_occurrence(harness):
    """Three occurrences, each past the cooldown, still produce the ten
    minute deduction the requirement calls for."""
    for occurrence in range(RULE.deduct_after):
        for _ in range(RULE.threshold):
            harness.counter._on_press(CTRL)
            harness.counter._on_release(CTRL)
        # Step past the cooldown so the next burst is a new occurrence.
        harness._clock += RULE.cooldown_seconds + 1
        harness.monitor.feed(harness.counter.drain_watched_presses(),
                             now=harness._clock)

    assert len(harness.events) == RULE.deduct_after
    assert harness.deducted_seconds == RULE.deduction_seconds


def test_shortcuts_mixed_with_mashing_do_not_hide_the_mashing(harness):
    """Interleaving real work with the behaviour the rule looks for must not
    mask it -- otherwise the fix would be an off switch."""
    for _ in range(RULE.threshold):
        harness.counter._on_press(CTRL)      # chorded: excused
        harness.counter._on_press(TAB)
        harness.counter._on_release(TAB)
        harness.counter._on_release(CTRL)

        harness.counter._on_press(CTRL)      # bare: counted
        harness.counter._on_release(CTRL)
    harness.tick()

    assert len(harness.alerts) == 1


def test_nothing_is_detected_outside_a_tracking_session(harness):
    """The monitor no-ops when no session is running; the counter's
    listeners are not even started then."""
    harness.monitor.stop_session()
    for _ in range(RULE.threshold * 2):
        harness.counter._on_press(CTRL)
        harness.counter._on_release(CTRL)
    harness.tick()

    assert harness.alerts == []
    assert harness.events == []
