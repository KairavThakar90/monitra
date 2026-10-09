"""
The auto-updater's safety properties, each one a way an update could hurt.

`test_update_installer.py` pins the orchestration (schedule, state machine,
checksum). This module pins what stands between a *wrong answer* and a user's
machine:

* a malformed, stale or hostile answer from the backend changes nothing;
* an artifact is only fetched from where this build says, over https, through
  redirects that never leave an approved host or downgrade;
* a corrupt, truncated or tampered download is discarded and nothing runs it;
* an installer is only run if it is validly signed (and by whom, when pinned);
* a failed installer is reported as a failure -- the relaunch of the old
  version must never read as success;
* every path to the helper survives a username with a space, an accent, an
  ampersand and a percent sign;
* and a failed automatic update is never a dead end: there is a manual link.

What is mocked and what is real is stated per test. The signature tests run
against real signed and unsigned binaries on Windows (WinVerifyTrust); the
helper tests run a real `cmd.exe` against compiled stand-in executables.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import version
from background_services.update import ReleaseInfo, UpdateService, UpdateState
from background_services.update import installer, policy, signature
from background_services.update.downloader import DownloadError, download_and_verify, updates_dir
from tests.test_update_installer import (
    GH_SETUP, INSTALLABLE, FakeCache, FakeTasks, make_service,
)

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")
INSTALLED = version.VERSION

# Versions strictly newer than the installed one, derived from it rather than
# written down, so that cutting a release (the only edit version.py takes)
# cannot turn "the next patch" into "the version already installed".
_MAJOR, _MINOR, _PATCH = (int(part) for part in INSTALLED.split("."))
NEWER_THAN_INSTALLED = [
    f"{_MAJOR}.{_MINOR}.{_PATCH + 1}",     # next patch
    f"{_MAJOR}.{_MINOR}.{_PATCH + 10}",    # two-digit patch: numeric, not lexical
    f"{_MAJOR}.{_MINOR + 1}.0",            # next minor
    f"{_MAJOR + 1}.0.0",                   # next major
    f"{_MAJOR + 9}.0.0",                   # two-digit major
]


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path))
    from core import paths

    paths.reset_cache()
    yield tmp_path
    paths.reset_cache()


@pytest.fixture
def production(monkeypatch):
    """A production build: the strictest policy, ignoring every test hook."""
    monkeypatch.setattr(policy, "_is_production", lambda: True)


@pytest.fixture
def development(monkeypatch):
    monkeypatch.setattr(policy, "_is_production", lambda: False)


def offered(**overrides):
    """An installable answer for a version newer than the one running."""
    data = dict(INSTALLABLE, latest_version="9.9.9", update_available=True)
    data.update(overrides)
    return data


def checked_service(payload, **kwargs):
    """A service that has completed its startup tick, ready for the real check."""
    service, api = make_service(payload, cache=kwargs.pop("cache", FakeCache()), **kwargs)
    service.tick()   # the first tick only schedules the startup delay
    return service, api


# ════════════════════════════════════════════════════════════════════════════
# Where an artifact may come from
# ════════════════════════════════════════════════════════════════════════════


class TestDownloadUrlPolicy:

    def test_an_https_release_url_on_an_approved_host_is_allowed(self, production):
        assert policy.check_url(GH_SETUP) is None

    @pytest.mark.parametrize("url", [
        "http://github.com/o/r/releases/download/v1/Monitra-Setup-1.exe",
        "ftp://github.com/x.exe",
        "file:///C:/Windows/System32/cmd.exe",
        "javascript:alert(1)",
        "//github.com/x.exe",
    ])
    def test_anything_but_https_is_refused(self, production, url):
        assert policy.check_url(url) is not None

    @pytest.mark.parametrize("url", [
        "https://evil.example.com/Monitra-Setup-1.exe",
        "https://github.com.evil.example/x.exe",
        "https://evilgithub.com/x.exe",
        "https://api.github.com/x.exe",
        "https://objects.githubusercontent.com/x.exe",   # a CDN is a redirect target, not a start
    ])
    def test_an_unapproved_host_is_refused(self, production, url):
        assert "not an approved release host" in policy.check_url(url)

    def test_credentials_and_odd_ports_are_refused(self, production):
        assert "credentials" in policy.check_url("https://u:p@github.com/o/r/x.exe")
        assert "port" in policy.check_url("https://github.com:8443/o/r/x.exe")

    @pytest.mark.parametrize("value", [None, "", 5, "https://", "https://github.com/a b.exe"])
    def test_nonsense_is_refused_not_crashed_on(self, production, value):
        assert policy.check_url(value) is not None

    def test_an_environment_variable_cannot_widen_a_production_build(self, production, monkeypatch):
        monkeypatch.setenv("MONITRA_UPDATE_EXTRA_HOSTS", "downloads.example.com")
        assert policy.check_url("https://downloads.example.com/x.exe") is not None

    def test_a_development_build_may_add_a_host_for_testing(self, development, monkeypatch):
        monkeypatch.setenv("MONITRA_UPDATE_EXTRA_HOSTS", "downloads.example.com")
        assert policy.check_url("https://downloads.example.com/x.exe") is None
        # ...and only that host: the approved list is added to, never replaced.
        assert policy.check_url("https://evil.example.com/x.exe") is not None

    def test_the_policy_fails_closed_when_the_environment_is_unknown(self, monkeypatch):
        import app.config

        monkeypatch.setattr(app.config, "settings", SimpleNamespace())   # no is_production
        assert policy._is_production() is True


class TestRedirectPolicy:

    def test_github_redirecting_to_its_cdn_is_allowed(self, production):
        for target in (
            "https://objects.githubusercontent.com/github-production-release-asset/abc",
            "https://release-assets.githubusercontent.com/x",
        ):
            assert policy.check_redirect(target) is None

    def test_a_downgrade_to_http_is_refused(self, production):
        assert "leaves https" in policy.check_redirect("http://github.com/x.exe")
        assert "leaves https" in policy.check_redirect("http://objects.githubusercontent.com/x")

    @pytest.mark.parametrize("target", [
        "https://evil.example.com/x.exe",
        "https://evilgithubusercontent.com/x.exe",         # suffix needs the leading dot
        "https://githubusercontent.com.evil.example/x",
        "https://u:p@objects.githubusercontent.com/x",
        "https://objects.githubusercontent.com:8443/x",
        "",
        None,
    ])
    def test_everything_else_is_refused(self, production, target):
        assert policy.check_redirect(target) is not None


class TestUnsignedPolicy:

    def test_production_never_runs_an_unsigned_installer(self, production, monkeypatch):
        monkeypatch.setenv("MONITRA_UPDATE_ALLOW_UNSIGNED", "1")   # ignored in production
        assert policy.unsigned_allowed() is False

    def test_a_development_build_needs_the_explicit_flag(self, development, monkeypatch):
        monkeypatch.delenv("MONITRA_UPDATE_ALLOW_UNSIGNED", raising=False)
        assert policy.unsigned_allowed() is False
        monkeypatch.setenv("MONITRA_UPDATE_ALLOW_UNSIGNED", "1")
        assert policy.unsigned_allowed() is True


# ════════════════════════════════════════════════════════════════════════════
# The download itself  (httpx.MockTransport: mocked network, real downloader)
# ════════════════════════════════════════════════════════════════════════════


BODY = b"Z" * 3000
GOOD = hashlib.sha256(BODY).hexdigest()


def _network(monkeypatch, handler):
    """Route the downloader's own client through `handler`. Returns the list of
    URLs actually requested, so a test can prove a refusal made no request."""
    requested = []
    real = httpx.Client

    def recording(request):
        requested.append(str(request.url))
        return handler(request)

    monkeypatch.setattr(
        "background_services.update.downloader.httpx.Client",
        lambda **kw: real(transport=httpx.MockTransport(recording), **kw),
    )
    return requested


def _fetch(**kw):
    args = dict(url=GH_SETUP, expected_sha256=GOOD, version="9.9.9")
    args.update(kw)
    return download_and_verify(**args)


def _no_part_files():
    return [p.name for p in updates_dir().iterdir() if p.suffix == ".part"]


class TestDownloadRedirects:

    def test_a_redirect_to_githubs_cdn_is_followed_and_the_file_verified(self, scratch, production, monkeypatch):
        def handler(request):
            if request.url.host == "github.com":
                return httpx.Response(302, headers={"location": "https://objects.githubusercontent.com/a/b"})
            return httpx.Response(200, content=BODY)

        requested = _network(monkeypatch, handler)
        result = _fetch()
        assert result.path.read_bytes() == BODY
        assert len(requested) == 2

    def test_a_relative_redirect_stays_on_the_same_approved_host(self, scratch, production, monkeypatch):
        def handler(request):
            if request.url.path.endswith(".exe") and "moved" not in request.url.path:
                return httpx.Response(301, headers={"location": "/o/r/releases/download/moved/Monitra-Setup-9.9.9.exe"})
            return httpx.Response(200, content=BODY)

        _network(monkeypatch, handler)
        assert _fetch().path.is_file()

    def test_an_https_to_http_redirect_is_refused(self, scratch, production, monkeypatch):
        def handler(request):
            if request.url.scheme == "https":
                return httpx.Response(302, headers={"location": "http://objects.githubusercontent.com/x"})
            return httpx.Response(200, content=BODY)

        requested = _network(monkeypatch, handler)
        with pytest.raises(DownloadError) as caught:
            _fetch()
        assert "leaves https" in caught.value.detail
        assert requested == [GH_SETUP], "the downgraded URL must never be requested"
        assert _no_part_files() == []

    def test_a_redirect_to_an_unapproved_host_is_refused_before_it_is_requested(self, scratch, production, monkeypatch):
        def handler(request):
            return httpx.Response(302, headers={"location": "https://evil.example.com/Monitra-Setup-9.9.9.exe"})

        requested = _network(monkeypatch, handler)
        with pytest.raises(DownloadError) as caught:
            _fetch()
        assert "not an approved release host" in caught.value.detail
        assert requested == [GH_SETUP]

    def test_a_redirect_with_no_target_is_refused(self, scratch, production, monkeypatch):
        _network(monkeypatch, lambda request: httpx.Response(302))
        with pytest.raises(DownloadError):
            _fetch()

    def test_a_redirect_loop_is_cut_off(self, scratch, production, monkeypatch):
        requested = _network(
            monkeypatch,
            lambda r: httpx.Response(302, headers={"location": "https://github.com/o/r/loop"}),
        )
        with pytest.raises(DownloadError) as caught:
            _fetch()
        assert "redirects" in caught.value.detail
        assert len(requested) == policy.MAX_REDIRECTS + 1

    def test_a_url_outside_the_approved_hosts_is_refused_without_a_request(self, scratch, production, monkeypatch):
        requested = _network(monkeypatch, lambda r: httpx.Response(200, content=BODY))
        with pytest.raises(DownloadError):
            _fetch(url="https://evil.example.com/Monitra-Setup-9.9.9.exe")
        assert requested == []

    def test_plain_http_is_refused_without_a_request(self, scratch, production, monkeypatch):
        requested = _network(monkeypatch, lambda r: httpx.Response(200, content=BODY))
        with pytest.raises(DownloadError):
            _fetch(url="http://github.com/o/r/releases/download/v9/Monitra-Setup-9.9.9.exe")
        assert requested == []

    def test_no_authorization_header_is_ever_sent(self, scratch, production, monkeypatch):
        seen = []

        def handler(request):
            seen.append(dict(request.headers))
            return httpx.Response(200, content=BODY)

        _network(monkeypatch, handler)
        _fetch()
        assert all("authorization" not in {k.lower() for k in h} for h in seen)


class TestDownloadIntegrity:

    def test_a_tampered_body_is_discarded(self, scratch, production, monkeypatch):
        _network(monkeypatch, lambda r: httpx.Response(200, content=b"Y" * len(BODY)))
        with pytest.raises(DownloadError) as caught:
            _fetch()
        assert "sha256 mismatch" in caught.value.detail
        assert list(updates_dir().iterdir()) == []

    def test_a_truncated_body_is_discarded(self, scratch, production, monkeypatch):
        _network(monkeypatch, lambda r: httpx.Response(200, content=BODY[:1000]))
        with pytest.raises(DownloadError) as caught:
            _fetch(expected_size=len(BODY))
        assert "incomplete" in caught.value.message
        assert list(updates_dir().iterdir()) == []

    def test_an_html_error_page_served_as_200_is_discarded(self, scratch, production, monkeypatch):
        _network(monkeypatch, lambda r: httpx.Response(200, content=b"<html>rate limited</html>"))
        with pytest.raises(DownloadError):
            _fetch()
        assert list(updates_dir().iterdir()) == []

    def test_an_interrupted_download_leaves_nothing_behind(self, scratch, production, monkeypatch):
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                yield b"Z" * 500
                raise httpx.ReadError("connection reset")

        _network(monkeypatch, lambda r: httpx.Response(200, stream=Broken()))
        with pytest.raises(DownloadError) as caught:
            _fetch()
        assert "no connection" in caught.value.message
        assert list(updates_dir().iterdir()) == []

    def test_a_timeout_is_reported_not_raised_raw(self, scratch, production, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        _network(monkeypatch, handler)
        with pytest.raises(DownloadError) as caught:
            _fetch()
        assert "timed out" in caught.value.message

    def test_a_server_error_is_reported_with_its_status(self, scratch, production, monkeypatch):
        _network(monkeypatch, lambda r: httpx.Response(503))
        with pytest.raises(DownloadError) as caught:
            _fetch()
        assert "503" in caught.value.detail

    def test_a_withdrawn_asset_is_a_clean_failure(self, scratch, production, monkeypatch):
        _network(monkeypatch, lambda r: httpx.Response(404))
        with pytest.raises(DownloadError) as caught:
            _fetch()
        assert "404" in caught.value.detail
        assert list(updates_dir().iterdir()) == []

    def test_a_full_disk_is_refused_before_anything_is_requested(self, scratch, production, monkeypatch):
        requested = _network(monkeypatch, lambda r: httpx.Response(200, content=BODY))
        monkeypatch.setattr(
            "background_services.update.downloader.shutil.disk_usage",
            lambda path: SimpleNamespace(total=10**9, used=10**9 - 1000, free=1000),
        )
        with pytest.raises(DownloadError) as caught:
            _fetch(expected_size=len(BODY))
        assert "free disk space" in caught.value.message
        assert requested == []

    def test_a_disk_that_fills_mid_download_is_reported(self, scratch, production, monkeypatch):
        _network(monkeypatch, lambda r: httpx.Response(200, content=BODY))
        real_open = os.fdopen

        class Full:
            def __init__(self, handle):
                self._handle = handle

            def write(self, data):
                raise OSError(28, "No space left on device")

            def close(self):
                self._handle.close()

        monkeypatch.setattr("background_services.update.downloader.os.fdopen",
                            lambda fd, mode: Full(real_open(fd, mode)))
        with pytest.raises(DownloadError) as caught:
            _fetch()
        assert "could not be saved to disk" in caught.value.message
        assert list(updates_dir().iterdir()) == []

    def test_a_download_stopped_by_shutdown_is_discarded(self, scratch, production, monkeypatch):
        _network(monkeypatch, lambda r: httpx.Response(200, content=BODY))
        with pytest.raises(DownloadError) as caught:
            _fetch(should_stop=lambda: True)
        assert "cancelled" in caught.value.message
        assert list(updates_dir().iterdir()) == []

    def test_an_older_installer_already_present_is_replaced_not_reused(self, scratch, production, monkeypatch):
        stale = updates_dir() / "Monitra-9.9.9.exe"
        stale.write_bytes(b"an earlier, different file")
        _network(monkeypatch, lambda r: httpx.Response(200, content=BODY))
        assert _fetch().path.read_bytes() == BODY


# ════════════════════════════════════════════════════════════════════════════
# Malformed answers from the backend
# ════════════════════════════════════════════════════════════════════════════


class TestMalformedAnswers:

    @pytest.mark.parametrize("payload", [
        None, [], ["update_available"], "oops", 7, True,
        {"foo": "bar"},
        {"update_available": "yes", "latest_version": "9.9.9"},
        {"update_available": 1, "latest_version": "9.9.9"},
        {"update_available": True, "latest_version": 123},
        {"update_available": False, "latest_version": ["9.9.9"]},
    ])
    def test_an_unusable_answer_is_a_failed_check_that_changes_nothing(self, payload):
        cache = FakeCache()
        service, _api = checked_service(payload, cache=cache)
        offers, announcements = [], []
        service.update_offered.connect(lambda r, m: offers.append(r))
        service.update_available.connect(lambda v, u: announcements.append(v))

        delay = service.tick()      # must not raise

        assert service.update_state == UpdateState.IDLE
        assert service.is_busy is False, "a malformed answer must not leave the updater 'checking'"
        assert "updates.last_check_at" not in cache.state, "a failed check must not count as a success"
        assert service.latest_release is None
        assert offers == [] and announcements == []
        assert service.pending_count == 0
        # Retried soon (the hold interval), not postponed by the full ten hours.
        assert delay < service.CHECK_INTERVAL_MS / 4

    def test_a_manual_check_still_works_after_a_malformed_answer(self):
        service, api = checked_service(None)
        service.tick()
        api.payload = offered()
        service.tick()
        assert service.pending_release is not None

    def test_the_update_service_keeps_running_after_a_malformed_answer(self):
        service, _api = checked_service([])
        service.tick()
        service.tick()   # and again: no exception, no stuck state
        assert service.update_state == UpdateState.IDLE

    def test_a_handler_that_raises_cannot_leave_the_updater_checking(self, monkeypatch):
        service, _api = checked_service(offered())

        def boom(payload):
            raise RuntimeError("handler bug")

        monkeypatch.setattr(service, "_announce", boom)
        with pytest.raises(RuntimeError):
            service.tick()
        assert service.update_state != UpdateState.CHECKING
        assert service.is_busy is False

    def test_a_good_answer_after_a_bad_one_is_honoured(self):
        service, api = checked_service(None)
        service.tick()
        api.payload = offered()
        offers = []
        service.update_offered.connect(lambda r, m: offers.append(r.version))
        service.tick()
        assert offers == ["9.9.9"]


class TestNotAnUpdate:
    """The server compares versions. So does the client: a wrong server must not
    be able to turn an old build into an 'update'."""

    @pytest.mark.parametrize("latest", [INSTALLED, "1.3.0", "1.2.9", "0.9.0", "1.0.10"])
    def test_an_equal_or_older_version_is_never_offered(self, latest):
        service, _api = checked_service(offered(latest_version=latest))
        offers, announcements = [], []
        service.update_offered.connect(lambda r, m: offers.append(r))
        service.update_available.connect(lambda v, u: announcements.append(v))
        service.tick()
        assert offers == [] and announcements == []
        assert service.pending_release is None
        assert service.pending_count == 0
        assert service.force_update_pending is False

    @pytest.mark.parametrize("latest", NEWER_THAN_INSTALLED)
    def test_a_newer_version_is_offered_across_the_semver_boundaries(self, latest):
        service, _api = checked_service(offered(latest_version=latest))
        offers = []
        service.update_offered.connect(lambda r, m: offers.append(r.version))
        service.tick()
        assert offers == [latest]

    @pytest.mark.parametrize("latest", ["v1.3.2", "1.3", "1.3.2.1", "1.3.2-beta.1", "latest", ""])
    def test_a_version_that_is_not_major_minor_patch_is_never_offered(self, latest):
        service, _api = checked_service(offered(latest_version=latest))
        offers = []
        service.update_offered.connect(lambda r, m: offers.append(r))
        service.tick()
        assert offers == []

    def test_a_mandatory_flag_on_a_non_update_cannot_lock_anyone_out(self):
        service, _api = checked_service(offered(latest_version=INSTALLED, force_update=True))
        service.tick()
        assert service.force_update_pending is False

    def test_another_platforms_artifact_is_never_offered(self):
        other = "darwin" if sys.platform != "darwin" else "win32"
        service, _api = checked_service(offered(platform=other))
        offers = []
        service.update_offered.connect(lambda r, m: offers.append(r))
        service.tick()
        assert offers == []

    def test_another_architectures_artifact_is_never_offered(self, monkeypatch):
        monkeypatch.setattr("background_services.update.update_service.MACHINE_ARCH", "AMD64")
        service, _api = checked_service(offered(architecture="arm64"))
        offers = []
        service.update_offered.connect(lambda r, m: offers.append(r))
        service.tick()
        assert offers == []

    def test_the_machines_own_artifact_is_offered(self, monkeypatch):
        monkeypatch.setattr("background_services.update.update_service.MACHINE_ARCH", "AMD64")
        service, _api = checked_service(offered(architecture="x86_64", platform=sys.platform))
        offers = []
        service.update_offered.connect(lambda r, m: offers.append(r))
        service.tick()
        assert len(offers) == 1


class TestTrustBoundary:

    def test_a_release_on_an_unapproved_host_is_announced_but_never_installable(self, production):
        service, _api = checked_service(offered(download_url="https://evil.example.com/Monitra-Setup-9.9.9.exe"))
        offers, announcements = [], []
        service.update_offered.connect(lambda r, m: offers.append(r))
        service.update_available.connect(lambda v, u: announcements.append(v))
        service.tick()
        assert offers == [], "no dialog, so no 'Update Now' for an artifact this build would not fetch"
        assert announcements == ["9.9.9"]
        assert service.pending_release is None

    def test_a_plain_http_artifact_is_never_installable(self, production):
        assert ReleaseInfo.from_payload(offered(
            download_url="http://github.com/o/r/releases/download/v9/Monitra-Setup-9.9.9.exe")) is None

    def test_a_missing_or_malformed_checksum_is_never_installable(self):
        for sha in (None, "", "abc", "g" * 64, "A" * 63):
            assert ReleaseInfo.from_payload(offered(sha256=sha)) is None

    def test_only_https_is_ever_opened_in_a_browser(self):
        for url in ("file:///C:/Windows/System32/calc.exe", "javascript:alert(1)",
                    "http://example.com/x.exe", "ms-msdt:/id", "https://"):
            service, _api = checked_service(offered(download_url=url, sha256=None))
            service.tick()
            assert service.download_url() is None, url
            assert service.manual_download_url() is None, url

    def test_the_manual_link_prefers_the_verified_artifact(self):
        service, _api = checked_service(offered())
        service.tick()
        assert service.manual_download_url() == GH_SETUP

    def test_an_announce_only_release_still_has_a_manual_link(self):
        service, _api = checked_service(offered(sha256=None))
        service.tick()
        assert service.manual_download_url() == GH_SETUP


# ════════════════════════════════════════════════════════════════════════════
# Postponing, repeating, offline, mandatory  (behaviour that must not regress)
# ════════════════════════════════════════════════════════════════════════════


class TestUserChoicesAndRepetition:

    def test_a_check_while_the_user_has_not_decided_does_not_raise_a_second_dialog(self):
        service, _api = checked_service(offered())
        offers = []
        service.update_offered.connect(lambda r, m: offers.append(r.version))
        service.tick()
        service.tick()
        service.tick()
        assert offers == ["9.9.9"]

    def test_later_leaves_the_release_on_offer_and_the_badge_in_place(self):
        service, _api = checked_service(offered())
        service.tick()
        # "Later" is a dialog-only act: the service is told nothing, so nothing is
        # forgotten -- the release stays pending and the badge stays up.
        assert service.pending_release is not None
        assert service.pending_count == 1
        assert service.update_state == UpdateState.UPDATE_AVAILABLE

    def test_a_restart_after_later_offers_it_again_at_the_next_due_check(self):
        cache = FakeCache()
        first, _ = checked_service(offered(), cache=cache)
        first.tick()
        # A new process (same persisted state): the toast gate is per session.
        second, _ = checked_service(offered(), cache=cache)
        offers = []
        second.update_offered.connect(lambda r, m: offers.append(r.version))
        second.tick()
        assert offers == ["9.9.9"]
        assert second.pending_count == 1, "the badge came back from the persisted record"

    def test_a_mandatory_update_is_re_offered_on_every_check(self):
        service, _api = checked_service(offered(force_update=True))
        offers = []
        service.update_offered.connect(lambda r, m: offers.append(m))
        service.tick()
        service.tick()
        assert offers == [True, True]

    def test_nothing_is_checked_or_downloaded_while_offline(self):
        from background_services.network import NetworkState

        service, api = make_service(offered(), cache=FakeCache(),
                                    network_state=NetworkState.NO_NETWORK)
        service.tick()
        delay = service.tick()
        assert api.calls == 0
        assert service.update_state == UpdateState.IDLE
        assert delay == service.HOLD_INTERVAL_MS

    def test_nothing_is_checked_while_signed_out(self):
        service, api = make_service(offered(), cache=FakeCache(), signed_in=False)
        service.tick()
        service.tick()
        assert api.calls == 0

    def test_an_api_error_holds_quietly_and_does_not_move_the_schedule(self):
        from app.api.exceptions import ApiError

        cache = FakeCache()
        service, _api = make_service(error=ApiError("boom"), cache=cache)
        service.tick()
        delay = service.tick()
        assert "updates.last_check_at" not in cache.state
        assert delay < service.CHECK_INTERVAL_MS / 4
        assert service.update_state == UpdateState.IDLE

    def test_repeated_update_now_starts_one_download_and_one_installer(self, scratch, monkeypatch):
        launches = []
        monkeypatch.setattr("background_services.update.update_service.can_install", lambda: None)
        monkeypatch.setattr("background_services.update.update_service.verify_installer", lambda p: None)
        monkeypatch.setattr("background_services.update.update_service.launch_installer",
                            lambda path, v: launches.append(v))
        _network(monkeypatch, lambda r: httpx.Response(200, content=BODY))
        tasks = FakeTasks()
        service, _api = make_service(
            offered(sha256=GOOD, file_size=len(BODY)), cache=FakeCache(), tasks=tasks)
        service.tick()
        service.tick()
        assert service.start_update() is True
        assert service.start_update() is False
        assert service.start_update() is False
        assert launches == ["9.9.9"] and tasks.submitted == 1


# ════════════════════════════════════════════════════════════════════════════
# The signature check
# ════════════════════════════════════════════════════════════════════════════


def _result(trusted=True, status=0, signer="Monitra Ltd"):
    return signature.SignatureResult(trusted=trusted, status=status, signer=signer)


class TestSignaturePolicy:
    """`evaluate` is the rule, independent of any file."""

    def test_an_untrusted_signature_is_refused(self):
        assert "not signed" in signature.evaluate(_result(False, 0x800B0100))
        assert "tampered" in signature.evaluate(_result(False, 0x80096010))

    def test_a_trusted_signature_passes_when_no_publisher_is_pinned(self, monkeypatch):
        monkeypatch.setattr(policy, "WINDOWS_SIGNER_PINS", ())
        assert signature.evaluate(_result()) is None

    def test_a_pinned_publisher_must_match_case_insensitively(self, monkeypatch):
        monkeypatch.setattr(policy, "WINDOWS_SIGNER_PINS", ("Monitra Ltd",))
        assert signature.evaluate(_result(signer="monitra ltd")) is None

    def test_a_validly_signed_installer_from_someone_else_is_refused(self, monkeypatch):
        monkeypatch.setattr(policy, "WINDOWS_SIGNER_PINS", ("Monitra Ltd",))
        problem = signature.evaluate(_result(signer="Some Other Company"))
        assert "not an approved publisher" in problem

    def test_a_signature_with_no_readable_signer_cannot_satisfy_a_pin(self, monkeypatch):
        monkeypatch.setattr(policy, "WINDOWS_SIGNER_PINS", ("Monitra Ltd",))
        assert signature.evaluate(_result(signer=None)) is not None


def _signed_binary():
    """A real, validly Authenticode-signed executable present on this machine."""
    import glob

    candidates = [sys.executable]
    candidates += sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.*\x64\signtool.exe"))
    for candidate in candidates:
        if Path(candidate).is_file() and signature.verify_authenticode(Path(candidate)).trusted:
            return Path(candidate)
    return None


@WINDOWS_ONLY
class TestRealAuthenticode:
    """WinVerifyTrust against real files. Nothing here is mocked."""

    def test_a_validly_signed_binary_is_trusted_and_its_signer_is_read(self):
        binary = _signed_binary()
        if binary is None:
            pytest.skip("no validly signed executable available on this machine")
        result = signature.verify_authenticode(binary)
        assert result.trusted is True and result.status == 0
        assert result.signer, "the signer's display name must be readable for pinning"
        assert result.cert_sha256 and len(result.cert_sha256) == 64

    def test_an_unsigned_file_is_not_trusted(self, tmp_path):
        unsigned = tmp_path / "setup.exe"
        unsigned.write_bytes(b"MZ" + b"\0" * 2000)
        result = signature.verify_authenticode(unsigned)
        assert result.trusted is False
        assert result.status & 0xFFFFFFFF in (0x800B0100, 0x800B0003, 0x80096011)

    def test_a_tampered_signed_binary_is_not_trusted(self, tmp_path):
        binary = _signed_binary()
        if binary is None:
            pytest.skip("no validly signed executable available on this machine")
        copy = tmp_path / "tampered.exe"
        shutil.copy(binary, copy)
        data = bytearray(copy.read_bytes())
        data[len(data) // 2] ^= 0xFF
        copy.write_bytes(bytes(data))
        result = signature.verify_authenticode(copy)
        assert result.trusted is False

    def test_a_missing_file_is_not_trusted(self, tmp_path):
        assert signature.verify_authenticode(tmp_path / "nope.exe").trusted is False

    def test_production_refuses_an_unsigned_installer(self, production, tmp_path):
        unsigned = tmp_path / "Monitra-Setup-9.9.9.exe"
        unsigned.write_bytes(b"MZ" + b"\0" * 2000)
        with pytest.raises(DownloadError) as caught:
            signature.verify_installer(unsigned)
        assert "digital signature" in caught.value.message
        assert "download the new version manually" in caught.value.message

    def test_production_ignores_the_unsigned_test_hook(self, production, tmp_path, monkeypatch):
        monkeypatch.setenv("MONITRA_UPDATE_ALLOW_UNSIGNED", "1")
        unsigned = tmp_path / "Monitra-Setup-9.9.9.exe"
        unsigned.write_bytes(b"MZ" + b"\0" * 2000)
        with pytest.raises(DownloadError):
            signature.verify_installer(unsigned)

    def test_a_development_build_can_opt_in_to_an_unsigned_installer(self, development, tmp_path, monkeypatch):
        monkeypatch.setenv("MONITRA_UPDATE_ALLOW_UNSIGNED", "1")
        unsigned = tmp_path / "Monitra-Setup-9.9.9.exe"
        unsigned.write_bytes(b"MZ" + b"\0" * 2000)
        signature.verify_installer(unsigned)   # does not raise

    def test_a_signed_installer_passes_production_policy(self, production, monkeypatch):
        binary = _signed_binary()
        if binary is None:
            pytest.skip("no validly signed executable available on this machine")
        monkeypatch.setattr(policy, "WINDOWS_SIGNER_PINS", ())
        signature.verify_installer(binary)

    def test_a_pin_for_a_different_publisher_refuses_a_real_signed_binary(self, production, monkeypatch):
        binary = _signed_binary()
        if binary is None:
            pytest.skip("no validly signed executable available on this machine")
        monkeypatch.setattr(policy, "WINDOWS_SIGNER_PINS", ("Definitely Not This Publisher",))
        with pytest.raises(DownloadError):
            signature.verify_installer(binary)

    def test_an_unsigned_download_never_reaches_the_installer_through_the_service(self, scratch, production, monkeypatch):
        launches = []
        failures = []
        monkeypatch.setattr("background_services.update.update_service.can_install", lambda: None)
        monkeypatch.setattr("background_services.update.update_service.launch_installer",
                            lambda *a: launches.append(a))
        _network(monkeypatch, lambda r: httpx.Response(200, content=BODY))
        service, _api = make_service(
            offered(sha256=GOOD, file_size=len(BODY)), cache=FakeCache(), tasks=FakeTasks())
        service.update_failed.connect(lambda m: failures.append(m))
        service.tick()
        service.tick()

        service.start_update()

        assert launches == [], "an unsigned installer must never be handed to the helper"
        assert len(failures) == 1 and "digital signature" in failures[0]
        assert service.update_state == UpdateState.IDLE
        assert list(updates_dir().iterdir()) == [], "the unverified artifact must be deleted"
        # ...and there is still a way forward for the user.
        assert service.manual_download_url() == GH_SETUP


# ════════════════════════════════════════════════════════════════════════════
# The helper: what runs after Monitra quits
# ════════════════════════════════════════════════════════════════════════════


def _csc():
    path = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    return path if path.is_file() else None


FAKE_INSTALLER = r"""
using System; using System.IO; using System.Threading;
class P { static int Main(string[] a) {
  string log = Environment.GetEnvironmentVariable("FAKE_LOG");
  long ms = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalMilliseconds;
  File.AppendAllText(log, "INSTALLER " + ms + " " + string.Join(" ", a) + "\r\n");
  Thread.Sleep(300);
  int code = 0; int.TryParse(Environment.GetEnvironmentVariable("FAKE_EXIT"), out code);
  return code; } }
"""

FAKE_APP = r"""
using System; using System.IO;
class P { static void Main(string[] a) {
  File.AppendAllText(Environment.GetEnvironmentVariable("FAKE_LOG"), "RELAUNCHED\r\n"); } }
"""


@pytest.fixture(scope="module")
def fake_binaries(tmp_path_factory):
    csc = _csc()
    if csc is None or sys.platform != "win32":
        pytest.skip("needs csc.exe (.NET Framework) to build stand-in executables")
    folder = tmp_path_factory.mktemp("fakebin")
    built = {}
    for name, source in (("installer", FAKE_INSTALLER), ("app", FAKE_APP)):
        src = folder / f"{name}.cs"
        src.write_text(source, encoding="utf-8")
        out = folder / f"{name}.exe"
        subprocess.run([str(csc), "-nologo", f"-out:{out}", str(src)], check=True,
                       capture_output=True)
        built[name] = out
    return built


def _run_helper(monkeypatch, data_dir, fake_binaries, *, exit_code=0, victim_seconds=1.5,
                version_name="9.9.9"):
    """Launch the real Windows helper with stand-in installer/app/process and
    wait for it to finish. Returns (event log text, result dict, victim exit time,
    installer start time)."""
    monkeypatch.setenv("MONITRA_DATA_DIR", str(data_dir))
    from core import paths

    paths.reset_cache()
    log = data_dir / "events.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_EXIT", str(exit_code))

    artifact = updates_dir() / f"Monitra-Setup-{version_name}.exe"
    shutil.copy(fake_binaries["installer"], artifact)

    victim = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({victim_seconds})"])
    monkeypatch.setattr(installer, "_current_pid", lambda: victim.pid)
    monkeypatch.setattr(installer, "can_install", lambda: None)
    monkeypatch.setattr(installer, "_relaunch_target", lambda: fake_binaries["app"])

    started = time.time()
    installer.launch_installer(artifact, version_name)
    victim.wait()
    victim_exit = time.time()

    deadline = time.time() + 30
    while time.time() < deadline and (artifact.exists() or list(updates_dir().glob("apply-update-*"))):
        time.sleep(0.2)
    # The relaunch is `start ""` -- asynchronous -- so it can land a moment after
    # the helper has deleted itself.
    deadline = time.time() + 15
    while time.time() < deadline and not (log.exists() and "RELAUNCHED" in log.read_text()):
        time.sleep(0.2)
    result = installer.read_and_clear_result()
    text = log.read_text() if log.exists() else ""
    return text, result, victim_exit, artifact


@WINDOWS_ONLY
class TestWindowsHelper:
    """The real cmd.exe helper, against stand-in executables."""

    def test_the_installer_runs_with_the_expected_flags_then_the_app_relaunches(self, tmp_path, monkeypatch, fake_binaries):
        text, result, _victim_exit, artifact = _run_helper(monkeypatch, tmp_path, fake_binaries)
        assert "/SILENT /SP- /NORESTART /CLOSEAPPLICATIONS" in text
        assert text.index("INSTALLER") < text.index("RELAUNCHED")
        assert result["target"] == "9.9.9" and result["exit_code"] == "0"
        assert not artifact.exists(), "the installer artifact is deleted afterwards"
        assert list(updates_dir().glob("apply-update-*")) == [], "the helper deletes itself"

    def test_the_helper_waits_for_monitra_to_exit_before_installing(self, tmp_path, monkeypatch, fake_binaries):
        text, _result, victim_exit, _ = _run_helper(monkeypatch, tmp_path, fake_binaries, victim_seconds=12)
        stamp = [line for line in text.splitlines() if line.startswith("INSTALLER ")][0].split()[1]
        installer_started = int(stamp) / 1000.0
        # Allow a moment of clock-resolution slack; the point is that the
        # installer did not start while the process was still alive.
        # The victim outlives the helper's grace period (3 s) by a wide margin, so a helper
        # that does not really wait cannot pass by coincidence -- an earlier version of
        # this test used a 3 s victim and passed while the helper never ran tasklist at all
        # (a stray TAB had replaced the backslash in `System32\tasklist.exe`).
        assert installer_started >= victim_exit - 0.5

    def test_a_failing_installer_is_recorded_not_hidden(self, tmp_path, monkeypatch, fake_binaries):
        text, result, _victim_exit, _ = _run_helper(monkeypatch, tmp_path, fake_binaries, exit_code=5)
        assert result["exit_code"] == "5"
        assert result["target"] == "9.9.9"
        # The previous version is still relaunched -- a failed update must not
        # cost the user the application -- and the record is what stops that
        # relaunch reading as success.
        assert "RELAUNCHED" in text

    def test_a_username_with_space_accent_ampersand_and_percent_is_survived(self, tmp_path, monkeypatch, fake_binaries):
        awkward = tmp_path / "Jos\u00e9 M\u00fcller & Sons 100%"
        awkward.mkdir()
        text, result, _victim_exit, artifact = _run_helper(monkeypatch, awkward, fake_binaries)
        assert "INSTALLER" in text and "RELAUNCHED" in text
        assert result["exit_code"] == "0"
        assert not artifact.exists()

    def test_the_helper_script_has_no_control_characters_so_every_command_path_is_intact(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path))
        from core import paths

        paths.reset_cache()
        raw = installer._write_windows_helper("9.9.9").read_bytes()
        stray = sorted({b for b in raw if b < 32 and b not in (10, 13)})
        assert stray == [], f"control characters in the helper: {stray}"
        backslash = bytes([92])
        assert b"System32" + backslash + b"tasklist.exe" in raw and b"System32" + backslash + b"ping.exe" in raw
        assert bytes([13, 13]) not in raw, "line endings were doubled"

    def test_the_helper_script_is_pure_ascii_and_contains_no_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path / "Jos\u00e9"))
        from core import paths

        paths.reset_cache()
        script = installer._write_windows_helper("9.9.9")
        raw = script.read_bytes()
        raw.decode("ascii")   # raises on any non-ASCII byte
        assert str(tmp_path).encode() not in raw
        for name in ("MONITRA_UPDATE_ARTIFACT", "MONITRA_UPDATE_RELAUNCH", "MONITRA_UPDATE_RESULT"):
            assert name.encode() in raw


class TestHelperLaunchErrors:

    def test_a_failure_preparing_the_helper_is_an_install_error_not_a_crash(self, scratch, monkeypatch):
        artifact = scratch / ("Monitra-Setup-9.9.9.exe" if sys.platform == "win32" else "Monitra-9.9.9.dmg")
        artifact.write_bytes(b"x")
        monkeypatch.setattr(installer, "can_install", lambda: None)

        def explode(*a, **k):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(installer, "_windows_launch", explode)
        monkeypatch.setattr(installer, "_macos_launch", explode)
        with pytest.raises(installer.InstallError) as caught:
            installer.launch_installer(artifact, "9.9.9")
        assert "unaffected" in caught.value.message

    def test_a_missing_artifact_is_refused(self, scratch, monkeypatch):
        monkeypatch.setattr(installer, "can_install", lambda: None)
        with pytest.raises(installer.InstallError):
            installer.launch_installer(scratch / "nope.exe", "9.9.9")

    def test_an_artifact_of_the_wrong_type_is_refused(self, scratch, monkeypatch):
        monkeypatch.setattr(installer, "can_install", lambda: None)
        wrong = scratch / "Monitra-9.9.9.bin"
        wrong.write_bytes(b"x")
        with pytest.raises(installer.InstallError) as caught:
            installer.launch_installer(wrong, "9.9.9")
        assert "not an installer for this system" in caught.value.message

    def test_a_portable_or_source_build_cannot_auto_update_and_says_so(self):
        # The test suite runs from source, which is the case `can_install` refuses.
        assert "installed build" in installer.can_install()


class TestMacOSHelperText:
    """Generated text only: this project has not run it on a real Mac."""

    def _script(self, scratch, monkeypatch, pins=()):
        monkeypatch.setattr(policy, "MACOS_TEAM_ID_PINS", pins)
        monkeypatch.setattr(installer, "_relaunch_target", lambda: Path("/Applications/Monitra.app"))
        self.artifact = Path("/tmp") / "it's a dmg" / "Monitra-9.9.9.dmg"
        return installer._write_macos_helper(self.artifact, "9.9.9")

    def test_it_verifies_the_signature_before_touching_the_old_bundle(self, scratch, production, monkeypatch):
        text = self._script(scratch, monkeypatch).read_text()
        assert text.index("codesign --verify --deep --strict") < text.index('mv "$BUNDLE" "$BACKUP"')
        assert text.index("spctl --assess --type execute") < text.index('mv "$BUNDLE" "$BACKUP"')
        assert "REQUIRE_SIGNED=1" in text

    def test_a_pinned_team_is_enforced(self, scratch, production, monkeypatch):
        text = self._script(scratch, monkeypatch, pins=("ABCDE12345",)).read_text()
        assert "TEAM_PINS='ABCDE12345'" in text
        assert "TeamIdentifier=" in text

    def test_a_failed_copy_restores_the_previous_bundle(self, scratch, production, monkeypatch):
        text = self._script(scratch, monkeypatch).read_text()
        assert 'mv "$BACKUP" "$BUNDLE"' in text

    def test_every_failure_reports_a_code_and_restarts_the_old_app(self, scratch, production, monkeypatch):
        text = self._script(scratch, monkeypatch).read_text()
        for code in (installer.MAC_EXIT_MOUNT, installer.MAC_EXIT_NO_BUNDLE, installer.MAC_EXIT_CODESIGN,
                     installer.MAC_EXIT_GATEKEEPER, installer.MAC_EXIT_MOVE, installer.MAC_EXIT_COPY):
            assert str(code) in text
        assert "abort()" in text and 'open "$BUNDLE"' in text

    def test_a_path_with_a_quote_in_it_is_quoted_safely(self, scratch, production, monkeypatch):
        text = self._script(scratch, monkeypatch).read_text()
        assert f"DMG={installer._sh_quote(str(self.artifact))}" in text
        # The embedded quote is closed, escaped and reopened -- never left to end
        # the string early.
        assert installer._sh_quote("it's") == "'it'\\''s'"

    def test_a_production_build_cannot_have_signature_checking_switched_off(self, scratch, production, monkeypatch):
        monkeypatch.setenv("MONITRA_UPDATE_ALLOW_UNSIGNED", "1")
        assert "REQUIRE_SIGNED=1" in self._script(scratch, monkeypatch).read_text()

    def test_the_script_is_valid_shell(self, scratch, production, monkeypatch):
        bash = shutil.which("bash")
        if sys.platform == "win32" or not bash:
            pytest.skip("syntax check needs a POSIX bash")
        script = self._script(scratch, monkeypatch)
        assert subprocess.run([bash, "-n", str(script)]).returncode == 0

    def test_no_unsubstituted_placeholder_survives(self, scratch, production, monkeypatch):
        import re

        text = self._script(scratch, monkeypatch).read_text()
        assert re.findall(r"@[A-Z_]+@", text) == []


# ════════════════════════════════════════════════════════════════════════════
# A failed installer is never reported as success
# ════════════════════════════════════════════════════════════════════════════


def _record(scratch, **values):
    from core import paths

    paths.reset_cache()
    installer.result_path().write_text(
        "\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")


class TestInstallOutcomeReporting:

    def _start(self, monkeypatch):
        service, _api = make_service(cache=FakeCache())
        told = []
        service.runtime.notifications.notify = lambda message, level, **kw: told.append((message, level, kw))
        monkeypatch.setattr("background_services.update.update_service.clear_stale_downloads", lambda: 0)
        # `on_start` starts the service's real loop thread; it must be stopped
        # again or the thread outlives the test and takes the interpreter with it.
        try:
            service.on_start()
        finally:
            service.on_stop(2000)
        return service, told

    def test_success_is_reported_when_the_running_version_is_the_target(self, scratch, monkeypatch):
        _record(scratch, target=INSTALLED, stage="started", exit_code=0)
        service, told = self._start(monkeypatch)
        assert service.last_install_outcome == ("success", INSTALLED)
        assert "updated to version" in told[0][0]

    def test_a_failed_installer_is_reported_as_a_failure(self, scratch, monkeypatch):
        _record(scratch, target="9.9.9", stage="started", exit_code=5)
        service, told = self._start(monkeypatch)
        assert service.last_install_outcome == ("failed", "9.9.9")
        assert "could not be installed" in told[0][0] and "still on version" in told[0][0]
        assert "manually" in told[0][0]

    def test_an_installer_that_exited_zero_but_left_the_old_version_is_not_success(self, scratch, monkeypatch):
        _record(scratch, target="9.9.9", stage="started", exit_code=0)
        service, told = self._start(monkeypatch)
        assert service.last_install_outcome == ("not-applied", "9.9.9")
        assert "still on version" in told[0][0]

    def test_an_install_that_never_reported_a_code_is_reported_as_unfinished(self, scratch, monkeypatch):
        _record(scratch, target="9.9.9", stage="started")
        service, told = self._start(monkeypatch)
        assert service.last_install_outcome == ("interrupted", "9.9.9")
        assert "did not finish" in told[0][0]

    def test_a_stale_record_from_before_a_later_update_is_ignored(self, scratch, monkeypatch):
        _record(scratch, target="1.0.0", stage="started", exit_code=0)
        service, told = self._start(monkeypatch)
        assert service.last_install_outcome is None and told == []

    def test_a_result_is_reported_once_and_then_forgotten(self, scratch, monkeypatch):
        _record(scratch, target="9.9.9", stage="started", exit_code=5)
        self._start(monkeypatch)
        assert not installer.result_path().exists()
        _service, told = self._start(monkeypatch)
        assert told == []

    def test_no_record_means_no_notification(self, scratch, monkeypatch):
        service, told = self._start(monkeypatch)
        assert service.last_install_outcome is None and told == []

    def test_a_garbage_record_does_not_break_startup(self, scratch, monkeypatch):
        from core import paths

        paths.reset_cache()
        installer.result_path().write_bytes(b"\xff\xfe\x00 not a record \x00")
        service, told = self._start(monkeypatch)   # must not raise
        assert told == []


# ════════════════════════════════════════════════════════════════════════════
# The update dialog
# ════════════════════════════════════════════════════════════════════════════


def _release(**kw):
    return ReleaseInfo(version="9.9.9", download_url=GH_SETUP, sha256="a" * 64,
                       file_size=1234, release_notes="Faster startup.", **kw)


class TestUpdateDialog:

    def test_an_optional_update_offers_later_and_update_now(self, qapp):
        from ui.update_dialog import UpdateDialog

        dialog = UpdateDialog(_release(), mandatory=False)
        assert dialog._later_button is not None
        assert dialog._update_button.text() == "Update Now"

    def test_a_mandatory_update_has_no_later(self, qapp):
        from ui.update_dialog import UpdateDialog

        dialog = UpdateDialog(_release(), mandatory=True)
        assert dialog._later_button is None

    def test_later_closes_and_says_so(self, qapp):
        from ui.update_dialog import UpdateDialog

        dialog = UpdateDialog(_release(), mandatory=False)
        postponed = []
        dialog.postponed.connect(lambda: postponed.append(True))
        dialog._later_button.click()
        assert postponed == [True]
        assert dialog.result() == dialog.DialogCode.Accepted

    def test_escape_and_close_cannot_dismiss_the_prompt(self, qapp):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent
        from ui.update_dialog import UpdateDialog

        for mandatory in (False, True):
            dialog = UpdateDialog(_release(), mandatory=mandatory)
            dialog.show()
            dialog.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
            dialog.close()
            dialog.reject()
            assert dialog.isVisible(), f"mandatory={mandatory}"
            dialog.force_close()

    def test_update_now_is_a_single_request_however_often_it_is_clicked(self, qapp):
        from ui.update_dialog import UpdateDialog

        dialog = UpdateDialog(_release(), mandatory=False)
        requests = []
        dialog.update_requested.connect(lambda: requests.append(True))
        dialog._update_button.click()
        dialog._on_update()
        dialog._on_update()
        assert requests == [True]
        assert dialog._update_button.isEnabled() is False

    def test_a_failed_update_offers_try_again_and_a_manual_download(self, qapp):
        from ui.update_dialog import UpdateDialog

        dialog = UpdateDialog(_release(), mandatory=True)
        dialog.show()
        dialog._on_update()
        dialog.show_error("The update could not be verified.", can_download_manually=True)
        assert dialog._update_button.text() == "Try Again" and dialog._update_button.isEnabled()
        assert dialog._manual_button.isVisible()
        wanted = []
        dialog.manual_download_requested.connect(lambda: wanted.append(True))
        dialog._manual_button.click()
        assert wanted == [True]
        dialog.force_close()

    def test_the_manual_button_is_not_offered_without_a_link(self, qapp):
        from ui.update_dialog import UpdateDialog

        dialog = UpdateDialog(_release(), mandatory=False)
        dialog.show()
        dialog.show_error("failed", can_download_manually=False)
        assert not dialog._manual_button.isVisible()
        dialog.force_close()

    def test_the_manual_button_is_not_there_before_anything_has_failed(self, qapp):
        from ui.update_dialog import UpdateDialog

        dialog = UpdateDialog(_release(), mandatory=False)
        dialog.show()
        assert not dialog._manual_button.isVisible()
        dialog.force_close()

    def test_release_notes_are_shown_as_plain_text_never_markup(self, qapp):
        from ui.update_dialog import UpdateDialog

        release = ReleaseInfo(version="9.9.9", download_url=GH_SETUP, sha256="a" * 64,
                              release_notes="<img src=x onerror=alert(1)><b>bold</b>")
        dialog = UpdateDialog(release, mandatory=False)
        assert dialog._notes.toPlainText().startswith("<img")
        assert "<b>" in dialog._notes.toPlainText()


# ════════════════════════════════════════════════════════════════════════════
# Timer and session recovery across the restart
# ════════════════════════════════════════════════════════════════════════════


class TestRestartKeepsTheSession:
    """Real runtime coverage lives in test_timer_lifecycle_reliability
    (`test_an_update_restart_leaves_the_session_for_recovery`). What is pinned
    here is the wiring that makes an *update* take that path and an ordinary
    quit not."""

    def _main_source(self):
        return (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")

    def test_the_installer_handoff_exits_as_a_restart_not_a_quit(self):
        source = self._main_source()
        assert "self._dashboard.quit_requested.connect(self.exit_for_restart)" in source
        assert "quit_requested.connect(self.quit_application)" not in source.split(
            "self._dashboard.quit_requested")[1].split("\n")[0]

    def test_a_restart_does_not_stop_the_timer_and_a_quit_does(self):
        source = self._main_source()
        assert "stop_timer = not self._exit_is_restart" in source

    def test_the_updater_asks_the_window_to_quit_only_after_the_helper_is_running(self, scratch, monkeypatch):
        order = []
        monkeypatch.setattr("background_services.update.update_service.can_install", lambda: None)
        monkeypatch.setattr("background_services.update.update_service.verify_installer", lambda p: None)
        monkeypatch.setattr("background_services.update.update_service.launch_installer",
                            lambda path, v: order.append("helper started"))
        _network(monkeypatch, lambda r: httpx.Response(200, content=BODY))
        service, _api = make_service(
            offered(sha256=GOOD, file_size=len(BODY)), cache=FakeCache(), tasks=FakeTasks())
        service.install_started.connect(lambda v: order.append("quit requested"))
        service.tick()
        service.tick()
        service.start_update()
        assert order == ["helper started", "quit requested"]
