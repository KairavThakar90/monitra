"""The opt-in resource monitor: inert unless asked for, honest when it is."""
from __future__ import annotations

import pytest

from background_services.diagnostics import ResourceMonitorService, diagnostics_enabled


class TestDisabledByDefault:
    def test_off_unless_the_variable_says_so(self, monkeypatch):
        monkeypatch.delenv("MONITRA_RESOURCE_LOG", raising=False)
        assert diagnostics_enabled() is False
        monkeypatch.setenv("MONITRA_RESOURCE_LOG", "1")
        assert diagnostics_enabled() is True

    def test_a_disabled_monitor_starts_no_thread_and_imports_no_psutil(self, runtime, monkeypatch):
        import sys

        monkeypatch.delenv("MONITRA_RESOURCE_LOG", raising=False)
        service = ResourceMonitorService(runtime)
        assert service.enabled is False
        service.start()
        assert service._thread is None
        assert service._process is None
        service.stop()

    def test_the_runtime_registers_it_last(self, runtime):
        names = [s.name for s in runtime.services.services]
        assert names[-1] == "resource_monitor"


class TestSampling:
    def test_a_sample_reports_the_process_and_the_runtime(self, runtime, monkeypatch):
        pytest.importorskip("psutil")
        monkeypatch.setenv("MONITRA_RESOURCE_LOG", "1")
        service = ResourceMonitorService(runtime)
        sample = service.sample()
        assert sample.rss_mb > 10
        assert sample.threads >= 1
        assert sample.py_threads >= 1
        assert sample.sync_pending >= 0
        assert sample.screenshots_pending >= 0
        assert "RESOURCE" in sample.line()

    def test_the_interval_is_bounded_below(self, runtime, monkeypatch):
        monkeypatch.setenv("MONITRA_RESOURCE_LOG", "1")
        monkeypatch.setenv("MONITRA_RESOURCE_INTERVAL_S", "1")
        assert ResourceMonitorService(runtime).interval_ms == 5_000
        monkeypatch.setenv("MONITRA_RESOURCE_INTERVAL_S", "junk")
        assert ResourceMonitorService(runtime).interval_ms == 30_000

    def test_history_is_bounded(self, runtime, monkeypatch):
        from background_services.diagnostics.resource_monitor import HISTORY

        monkeypatch.setenv("MONITRA_RESOURCE_LOG", "1")
        service = ResourceMonitorService(runtime)
        for _ in range(HISTORY + 5):
            service.tick()
        assert len(service.history()) == HISTORY
