"""
Stopping a timer folds the session into the cached day without double counting.

The day's cache is the server's own `GET /time-entries` list, and while a
timer runs that list contains the running entry as a row whose `net_seconds`
the server filled with the live elapsed time at the moment it was read. The
fold that runs on Stop used to *add* the session to the first cached row of
the same task -- which, for a task tracked for the first time that day, was
that running row. TOTAL TIME TODAY and the task's hours then showed roughly
double the session until the next server read; offline, for ever. It was most
visible after "No, discard idle time" + Stop, where the total was supposed to
drop by the idle time and instead jumped.

These tests pin the three cases: the stopping entry's own row is replaced
with the finished figures; a running row is never added to; a finished row
of the same task (or a fresh row) is.
"""
from ui.dashboard_window import banked_seconds, _is_finished

DAY = "2026-09-17"


def _running(entry_id, task_id, live_net):
    # The server's running row: total_seconds 0, net_seconds live.
    return {
        "id": entry_id, "task_id": task_id, "start_time": "2026-09-17T09:00:00Z",
        "end_time": None, "status": "running", "total_seconds": 0,
        "adjustment_seconds": 0, "net_seconds": live_net,
    }


def _finished(entry_id, task_id, seconds, adjustment=0):
    return {
        "id": entry_id, "task_id": task_id, "start_time": "2026-09-17T08:00:00Z",
        "end_time": "2026-09-17T08:30:00Z", "status": "stopped",
        "total_seconds": seconds, "adjustment_seconds": adjustment,
        "net_seconds": max(0, seconds + adjustment),
    }


def _day_total(cache):
    return sum(banked_seconds(e) for e in cache.get_cached_time_entries(DAY) if _is_finished(e))


def test_the_stopping_entrys_own_row_is_replaced_not_added_to(cache):
    cache.cache_time_entries(DAY, [_finished(1, 7, 1800), _running(42, 9, live_net=600)])

    # The session ran 700s measured; 100s of idle time were discarded.
    cache.add_elapsed_to_cached_time_entry(
        DAY, 9, 600, entry_id=42, stopped_at="2026-09-17T09:11:40Z", measured_seconds=700,
    )

    rows = {e["id"]: e for e in cache.get_cached_time_entries(DAY)}
    assert rows[42]["net_seconds"] == 600
    assert rows[42]["total_seconds"] == 700
    assert rows[42]["status"] == "completed"
    assert rows[42]["end_time"] == "2026-09-17T09:11:40Z"
    assert _day_total(cache) == 1800 + 600          # not 1800 + 600 + 600


def test_a_running_row_of_the_same_task_is_never_folded_into(cache):
    """No entry id (an older caller): the fold must still not land on the
    live row. It gets a row of its own instead."""
    cache.cache_time_entries(DAY, [_running(42, 9, live_net=600)])

    cache.add_elapsed_to_cached_time_entry(DAY, 9, 600)

    rows = cache.get_cached_time_entries(DAY)
    live = next(e for e in rows if e["id"] == 42)
    assert live["net_seconds"] == 600 and live["status"] == "running"
    synthetic = [e for e in rows if e["id"] != 42]
    assert len(synthetic) == 1
    assert synthetic[0]["net_seconds"] == 600 and _is_finished(synthetic[0])
    assert _day_total(cache) == 600


def test_a_finished_row_of_the_same_task_is_added_to(cache):
    cache.cache_time_entries(DAY, [_finished(1, 7, 1800)])

    cache.add_elapsed_to_cached_time_entry(DAY, 7, 300, entry_id=99, measured_seconds=300)

    rows = cache.get_cached_time_entries(DAY)
    assert len(rows) == 1
    assert rows[0]["net_seconds"] == 2100 and rows[0]["total_seconds"] == 2100
    assert _day_total(cache) == 2100


def test_a_fresh_row_carries_the_netted_figure(cache):
    cache.cache_time_entries(DAY, [])

    cache.add_elapsed_to_cached_time_entry(
        DAY, 5, 540, entry_id=77, stopped_at="2026-09-17T10:00:00Z", measured_seconds=600,
    )

    rows = cache.get_cached_time_entries(DAY)
    assert len(rows) == 1
    assert rows[0]["net_seconds"] == 540 and rows[0]["total_seconds"] == 600
    assert _is_finished(rows[0])
    assert _day_total(cache) == 540


def test_nothing_is_written_for_a_zero_session(cache):
    cache.cache_time_entries(DAY, [_running(42, 9, live_net=0)])
    cache.add_elapsed_to_cached_time_entry(DAY, 9, 0, entry_id=42)
    assert cache.get_cached_time_entries(DAY)[0]["status"] == "running"
