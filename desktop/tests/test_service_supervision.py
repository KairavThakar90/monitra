"""
A supervised loop service that dies or stops ticking is repaired, once.

Idle detection is a `LoopService`. Nothing used to notice if its thread ended
or its tick stopped returning: the user simply never got a popup. These tests
use real threads (the failure being guarded against is a thread one) and pin
the guarantee that matters as much as the repair -- the repair never leaves two
live loops behind.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThread

from core.service import LoopService, ServiceManager, ServiceState


class Ticker(LoopService):
    name = "ticker"
    supervised = True
    interval_ms = 10

    def __init__(self, runtime=None):
        super().__init__(runtime or SimpleNamespace(storage=None))
        self.threads = []
        self.restarted_for = []
        self.block = None             # a threading.Event the next tick waits on

    def tick(self):
        self.threads.append(QThread.currentThread())
        self.heartbeat()
        gate, self.block = self.block, None
        if gate is not None:
            gate.wait(10)
        return 10

    def on_loop_restarted(self, reason):
        self.restarted_for.append(reason)


def _wait(qapp, condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def ticker(qapp):
    service = Ticker()
    service.start()
    assert _wait(qapp, lambda: len(service.threads) >= 3), "the loop never ticked"
    yield service
    service.stop(timeout_ms=2000)


def test_a_healthy_loop_is_left_alone(qapp, ticker):
    assert ticker.check_liveness() == "ok"
    assert ticker.restarted_for == []


def test_a_loop_whose_thread_died_is_restarted_and_ticks_again(qapp, ticker):
    old = ticker._thread
    old.quit()
    assert old.wait(2000)
    before = len(ticker.threads)

    assert ticker.check_liveness() == "restarted"
    assert ticker.restarted_for == ["thread_died"]
    assert ticker._thread is not old and ticker.loop_alive()
    assert _wait(qapp, lambda: len(ticker.threads) > before + 2), "the new loop does not tick"
    # Exactly one live loop: every tick since the repair is on the new thread.
    assert set(ticker.threads[before:]) == {ticker._thread} or all(
        t is ticker._thread for t in ticker.threads[before + 1:]
    )
    assert ticker.health.restart_count == 1


def test_a_second_check_after_a_repair_does_not_restart_again(qapp, ticker):
    ticker._thread.quit()
    ticker._thread.wait(2000)
    assert ticker.check_liveness() == "restarted"
    assert _wait(qapp, lambda: ticker.health.last_heartbeat is not None)
    assert ticker.check_liveness() == "ok"
    assert ticker.health.restart_count == 1


def test_a_loop_that_has_stopped_ticking_is_woken_first(qapp, ticker):
    ticker.health.last_heartbeat = time.time() - 3600
    assert ticker.check_liveness() == "woken"
    assert _wait(qapp, lambda: ticker.heartbeat_age() < 5)
    assert ticker.check_liveness() == "ok"
    assert ticker.restarted_for == []


def test_a_tick_that_never_returns_is_replaced_without_a_second_live_loop(qapp, ticker):
    gate = threading.Event()
    ticker.block = gate
    assert _wait(qapp, lambda: ticker.block is None), "the blocking tick never started"
    stuck_thread = ticker._thread
    ticker.health.last_heartbeat = time.time() - 3600
    ticker._stall_since = time.monotonic() - LoopService.STALL_REPLACE_SECONDS - 1

    assert ticker.check_liveness() == "replaced"
    assert ticker.restarted_for == ["tick_blocked"]
    assert ticker._thread is not stuck_thread
    assert len(ticker._retired_loops) == 1

    new_thread = ticker._thread
    assert _wait(qapp, lambda: any(t is new_thread for t in ticker.threads))

    ticks_on_stuck = sum(1 for t in ticker.threads if t is stuck_thread)
    gate.set()                                      # the blocked call finally returns
    time.sleep(0.2)
    qapp.processEvents()
    assert sum(1 for t in ticker.threads if t is stuck_thread) == ticks_on_stuck, \
        "the retired worker ticked again: two live loops"
    assert _wait(qapp, lambda: not stuck_thread.isRunning()), "the retired thread did not end"


def test_stopping_reaps_a_retired_loop(qapp):
    service = Ticker()
    service.start()
    assert _wait(qapp, lambda: len(service.threads) >= 2)
    gate = threading.Event()
    service.block = gate
    assert _wait(qapp, lambda: service.block is None)
    service.health.last_heartbeat = time.time() - 3600
    service._stall_since = time.monotonic() - LoopService.STALL_REPLACE_SECONDS - 1
    service.check_liveness()
    gate.set()
    assert service.stop(timeout_ms=2000)
    assert service._retired_loops == []


def test_a_stopped_service_is_never_revived(qapp, ticker):
    ticker.stop(timeout_ms=2000)
    assert ticker.check_liveness() == "stopped"
    assert not ticker.loop_alive()


def test_the_manager_supervises_only_what_asks_to_be_and_stops_supervising_first(qapp):
    class Plain(Ticker):
        name = "plain"
        supervised = False

    supervised, plain = Ticker(), Plain()
    manager = ServiceManager()
    manager.register(supervised)
    manager.register(plain)
    manager.start_all()
    try:
        assert manager._supervisor is not None
        assert _wait(qapp, lambda: len(supervised.threads) >= 2 and len(plain.threads) >= 2)
        for service in (supervised, plain):
            service._thread.quit()
            service._thread.wait(2000)
        manager.supervise()
        assert supervised.loop_alive(), "a supervised service was not repaired"
        assert not plain.loop_alive(), "an unsupervised service was touched"
    finally:
        failed = manager.stop_all(timeout_ms=2000)
    assert manager._supervisor is None
    assert failed == []


def test_an_exception_in_a_check_does_not_stop_the_others(qapp):
    class Broken(Ticker):
        name = "broken"
        def check_liveness(self):
            raise RuntimeError("boom")

    broken, fine = Broken(), Ticker()
    manager = ServiceManager()
    manager.register(broken)
    manager.register(fine)
    manager.start_all()
    try:
        assert _wait(qapp, lambda: len(fine.threads) >= 2)
        fine._thread.quit()
        fine._thread.wait(2000)
        manager.supervise()                       # must not raise
        assert fine.loop_alive()
    finally:
        manager.stop_all(timeout_ms=2000)


def test_idle_detection_is_supervised():
    from background_services.idle.idle_service import IdleService
    assert IdleService.supervised is True
