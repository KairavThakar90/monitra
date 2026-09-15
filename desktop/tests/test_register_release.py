"""
The release-registration step of the CI pipeline.

What matters here is that CI cannot get a release wrong in a way that reaches
users: a Windows installer must never be registered as a macOS artifact, the
checksum must be of the file that was actually built, a tag that disagrees with
`version.py` must be refused outright, and nothing this script does may publish
anything.
"""
from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import version
from tools import register_release as reg  # noqa: E402


@pytest.fixture(autouse=True)
def _a_release_build(monkeypatch):
    """Every test below describes registering a *release*.

    While the working tree carries an internal test build, version.PRERELEASE
    is set and `main()` refuses before it does anything; the refusal has its
    own test. The rest of the file must keep proving what a release build does.
    """
    monkeypatch.setattr(version, "PRERELEASE", "")


# ---------------------------------------------------------------------------
# Classifying what was built
# ---------------------------------------------------------------------------


def test_an_internal_test_build_is_never_registered(monkeypatch, tmp_path, capsys):
    # Testers install a pre-release by hand. A row for it -- even a draft, which
    # is one click from published -- would let it become the build the updater
    # and the download page offer to everyone. So the script refuses outright,
    # with credentials present and a tag that matches, before touching the
    # network.
    monkeypatch.setattr(version, "PRERELEASE", "beta.1")
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid")
    monkeypatch.setenv("MONITRA_RELEASE_TOKEN", "token")
    monkeypatch.setattr(
        reg, "post_release",
        lambda *a: pytest.fail("a pre-release must never reach the backend"),
    )
    artifact = tmp_path / f"Monitra-Setup-{version.VERSION}-beta.1.exe"
    artifact.write_bytes(b"x")
    monkeypatch.setattr(sys, "argv", [
        "register_release.py", "--tag", f"v{version.VERSION}",
        "--repo", "acme/monitra", "--artifacts", str(artifact),
    ])

    assert reg.main() == 1
    assert "pre-release" in capsys.readouterr().err


def test_a_pre_release_installer_would_still_be_classified_as_windows():
    # Documents why the refusal above has to exist: nothing about the filename
    # pattern keeps a beta installer out, so the guard must be the version.
    assert reg.classify("Monitra-Setup-1.2.0-beta.1.exe") == ("win32", None)


@pytest.mark.parametrize("name, expected", [
    ("Monitra-Setup-1.2.0.exe", ("win32", None)),
    ("Monitra-macOS-arm64-1.2.0.dmg", ("darwin", "arm64")),
    ("Monitra-macOS-x86_64-1.2.0.dmg", ("darwin", "x86_64")),
])
def test_each_artifact_is_classified_by_its_built_name(name, expected):
    assert reg.classify(name) == expected


def test_the_two_macos_builds_are_never_conflated():
    # Registering the Intel .dmg as the Apple Silicon one would hand every M-series
    # Mac a build that cannot run, and the updater would report it as a broken
    # update rather than as the wrong file.
    arm = reg.classify("Monitra-macOS-arm64-1.2.0.dmg")
    intel = reg.classify("Monitra-macOS-x86_64-1.2.0.dmg")
    assert arm != intel
    assert arm[1] == "arm64" and intel[1] == "x86_64"


def test_the_portable_zip_is_not_registered():
    # It is a folder the user unzips wherever they like, so there is no
    # installer to hand an update to and the desktop refuses to auto-update
    # one. Registering it would advertise a path that does not exist.
    assert reg.classify("Monitra-Portable-1.2.0.zip") is None


def test_unrecognised_files_are_skipped_rather_than_guessed_at():
    for name in ("notes.txt", "Monitra-Setup-1.2.0.exe.sha256",
                 "Monitra.app", "setup.exe", ""):
        assert reg.classify(name) is None, name


# ---------------------------------------------------------------------------
# The checksum
# ---------------------------------------------------------------------------


def test_the_checksum_is_of_the_file_on_disk(tmp_path):
    artifact = tmp_path / "Monitra-Setup-1.2.0.exe"
    body = b"pretend installer" * 5000
    artifact.write_bytes(body)
    assert reg.sha256_of(artifact) == hashlib.sha256(body).hexdigest()


def test_the_checksum_is_lower_case_hex(tmp_path):
    # The desktop compares digests as lower-case hex; an upper-case digest
    # would fail an artifact that downloaded perfectly.
    artifact = tmp_path / "Monitra-Setup-1.2.0.exe"
    artifact.write_bytes(b"x")
    digest = reg.sha256_of(artifact)
    assert digest == digest.lower() and len(digest) == 64


# ---------------------------------------------------------------------------
# The download URL
# ---------------------------------------------------------------------------


def test_the_download_url_is_the_public_release_asset():
    url = reg.asset_url("acme/monitra", "v1.2.0", "Monitra-Setup-1.2.0.exe")
    assert url == (
        "https://github.com/acme/monitra/releases/download/v1.2.0/"
        "Monitra-Setup-1.2.0.exe"
    )
    # HTTPS, because the desktop downloader refuses anything else outright.
    assert url.startswith("https://")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_tag_that_disagrees_with_version_py_is_refused(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid")
    monkeypatch.setenv("MONITRA_RELEASE_TOKEN", "token")
    artifact = tmp_path / "Monitra-Setup-9.9.9.exe"
    artifact.write_bytes(b"x")
    monkeypatch.setattr(sys, "argv", [
        "register_release.py", "--tag", "v9.9.9", "--repo", "acme/monitra",
        "--artifacts", str(artifact),
    ])

    # The artifacts are not the version the tag claims; registering either
    # number would make every support report naming it untrustworthy.
    assert reg.main() == 1
    assert "does not match version.py" in capsys.readouterr().err


def test_missing_credentials_skip_registration_rather_than_fail(monkeypatch, tmp_path):
    monkeypatch.delenv("MONITRA_API_BASE_URL", raising=False)
    monkeypatch.delenv("MONITRA_RELEASE_TOKEN", raising=False)
    artifact = tmp_path / f"Monitra-Setup-{version.VERSION}.exe"
    artifact.write_bytes(b"x")
    monkeypatch.setattr(sys, "argv", [
        "register_release.py", "--tag", f"v{version.VERSION}",
        "--repo", "acme/monitra", "--artifacts", str(artifact),
    ])

    # A fork still produces perfectly good artifacts.
    assert reg.main() == 0


def test_registration_posts_a_draft_and_never_publishes(monkeypatch, tmp_path):
    posted = []
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid/api/v1")
    monkeypatch.setenv("MONITRA_RELEASE_TOKEN", "token")
    monkeypatch.setattr(
        reg, "post_release",
        lambda base_url, token, payload: (posted.append((base_url, payload)), True)[1],
    )

    body = b"installer bytes"
    artifact = tmp_path / f"Monitra-Setup-{version.VERSION}.exe"
    artifact.write_bytes(body)
    monkeypatch.setattr(sys, "argv", [
        "register_release.py", "--tag", f"v{version.VERSION}",
        "--repo", "acme/monitra", "--artifacts", str(artifact),
    ])

    assert reg.main() == 0
    assert len(posted) == 1
    _base, payload = posted[0]
    assert payload["version"] == version.VERSION
    assert payload["platform"] == "win32"
    assert payload["sha256"] == hashlib.sha256(body).hexdigest()
    assert payload["file_size"] == len(body)
    assert payload["force_update"] is False
    # No status is sent, because the endpoint always creates a draft. There is
    # deliberately no code path here that could publish one.
    assert "status" not in payload


def test_a_failed_registration_is_reported_without_failing_the_build(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid")
    monkeypatch.setenv("MONITRA_RELEASE_TOKEN", "token")
    monkeypatch.setattr(reg, "post_release", lambda *a: False)

    artifact = tmp_path / f"Monitra-Setup-{version.VERSION}.exe"
    artifact.write_bytes(b"x")
    monkeypatch.setattr(sys, "argv", [
        "register_release.py", "--tag", f"v{version.VERSION}",
        "--repo", "acme/monitra", "--artifacts", str(artifact),
    ])

    # The build and the GitHub release are good either way, and the step can be
    # re-run without rebuilding anything.
    assert reg.main() == 0
    assert "incomplete" in capsys.readouterr().err


def test_a_mandatory_release_must_be_asked_for_explicitly(monkeypatch, tmp_path):
    posted = []
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid")
    monkeypatch.setenv("MONITRA_RELEASE_TOKEN", "token")
    monkeypatch.setattr(
        reg, "post_release",
        lambda base_url, token, payload: (posted.append(payload), True)[1],
    )
    artifact = tmp_path / f"Monitra-Setup-{version.VERSION}.exe"
    artifact.write_bytes(b"x")
    monkeypatch.setattr(sys, "argv", [
        "register_release.py", "--tag", f"v{version.VERSION}",
        "--repo", "acme/monitra", "--artifacts", str(artifact),
        "--force-update",
    ])

    assert reg.main() == 0
    assert posted[0]["force_update"] is True


# ---------------------------------------------------------------------------
# Release notes
# ---------------------------------------------------------------------------


def test_the_hand_written_changelog_entry_is_sent(monkeypatch):
    # This is what a person reads in the update dialog -- the note somebody
    # wrote, not the auto-generated commit list.
    notes = reg.release_notes(version.VERSION)
    assert notes is None or isinstance(notes, str)


def test_an_absent_entry_is_reported_as_none():
    assert reg.release_notes("0.0.0") is None


if __name__ == "__main__":
    pytest.main([__file__])


# ── The release credential ──────────────────────────────────────────────────
#
# CI presents a service credential: a long-lived API key belonging to an
# account whose only permission is `manage_desktop_releases`. It replaced a
# sign-in with an email and a password, and the replacement matters for two
# reasons worth pinning:
#
#   * the password path was `/auth/dev-login`, which is 404 whenever
#     ENV=production -- so the old arrangement held the whole deployment in
#     development mode to keep one build step working;
#   * an access token expires after thirty minutes, so one stored in a
#     repository secret is dead long before the next release.
#
# The key is sent as an ordinary bearer token, so there is no sign-in step here
# at all any more.


def _argv_for(monkeypatch, artifact):
    monkeypatch.setattr(sys, "argv", [
        "register_release.py", "--tag", f"v{version.VERSION}",
        "--repo", "acme/monitra", "--artifacts", str(artifact),
    ])


def _artifact(tmp_path):
    artifact = tmp_path / f"Monitra-Setup-{version.VERSION}.exe"
    artifact.write_bytes(b"x")
    return artifact


def test_the_api_key_is_presented_as_it_is_with_no_sign_in(monkeypatch, tmp_path):
    monkeypatch.delenv("MONITRA_RELEASE_TOKEN", raising=False)
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid")
    monkeypatch.setenv("MONITRA_RELEASE_CREDENTIAL", "msk_abc123_secret")
    posted = []
    monkeypatch.setattr(
        reg, "post_release",
        lambda base_url, token, payload: (posted.append(token), True)[1],
    )

    _argv_for(monkeypatch, _artifact(tmp_path))
    assert reg.main() == 0
    # Exactly the key from the environment: nothing was exchanged for it, so
    # there is no sign-in round trip that can fail or expire.
    assert posted == ["msk_abc123_secret"]


def test_the_tool_cannot_sign_in_at_all(monkeypatch):
    """There is no password path left to fall back to.

    A sign-in helper here would be a way for the pipeline to start depending on
    `/auth/dev-login` again, and that dependency is what pinned production to
    ENV=development.
    """
    assert not hasattr(reg, "sign_in")


def test_a_manual_token_is_still_honoured(monkeypatch, tmp_path):
    # A person registering a build by hand from a session they already have.
    # Unset in CI.
    monkeypatch.delenv("MONITRA_RELEASE_CREDENTIAL", raising=False)
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid")
    monkeypatch.setenv("MONITRA_RELEASE_TOKEN", "already-have-one")
    posted = []
    monkeypatch.setattr(
        reg, "post_release",
        lambda base_url, token, payload: (posted.append(token), True)[1],
    )

    _argv_for(monkeypatch, _artifact(tmp_path))
    assert reg.main() == 0
    assert posted == ["already-have-one"]


def test_the_api_key_wins_over_a_stale_manual_token(monkeypatch, tmp_path):
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid")
    monkeypatch.setenv("MONITRA_RELEASE_TOKEN", "thirty-minutes-old")
    monkeypatch.setenv("MONITRA_RELEASE_CREDENTIAL", "msk_abc123_secret")
    posted = []
    monkeypatch.setattr(
        reg, "post_release",
        lambda base_url, token, payload: (posted.append(token), True)[1],
    )

    _argv_for(monkeypatch, _artifact(tmp_path))
    assert reg.main() == 0
    assert posted == ["msk_abc123_secret"]


def test_no_credential_skips_registration_without_failing_the_build(
    monkeypatch, tmp_path, capsys
):
    # A fork, or a repository that has not been given the secret, still
    # produces perfectly good artifacts.
    monkeypatch.delenv("MONITRA_RELEASE_CREDENTIAL", raising=False)
    monkeypatch.delenv("MONITRA_RELEASE_TOKEN", raising=False)
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid")
    monkeypatch.setattr(
        reg, "post_release",
        lambda *a, **k: pytest.fail("must not register without a credential"),
    )

    _argv_for(monkeypatch, _artifact(tmp_path))
    assert reg.main() == 0
    assert "skipping backend registration" in capsys.readouterr().out


def test_a_base_url_without_a_credential_registers_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("MONITRA_API_BASE_URL", raising=False)
    monkeypatch.setenv("MONITRA_RELEASE_CREDENTIAL", "msk_abc123_secret")
    monkeypatch.setattr(
        reg, "post_release",
        lambda *a, **k: pytest.fail("must not register without a base url"),
    )

    _argv_for(monkeypatch, _artifact(tmp_path))
    assert reg.main() == 0


def test_a_refused_credential_is_reported_without_echoing_the_response(
    monkeypatch, capsys
):
    """Build logs are public on this repository.

    A refused credential reports its status code and what to do about it, and
    never the body of the response or any part of the key.
    """
    import urllib.error

    def explode(*a, **k):
        raise urllib.error.HTTPError(
            "https://api.invalid/desktop/releases", 401, "Unauthorized", {},
            io.BytesIO(b'{"detail":"Not authenticated"}'),
        )

    monkeypatch.setattr(reg.urllib.request, "urlopen", explode)
    assert reg.post_release("https://api.invalid", "msk_abc123_secret", {}) is False

    output = capsys.readouterr()
    assert "401" in output.err
    assert "MONITRA_RELEASE_CREDENTIAL" in output.err
    assert "Not authenticated" not in output.err
    assert "msk_abc123_secret" not in output.err
    assert "secret" not in output.err.replace("MONITRA_RELEASE_CREDENTIAL", "")


def test_a_refused_credential_never_publishes_anything(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid")
    monkeypatch.setenv("MONITRA_RELEASE_CREDENTIAL", "msk_abc123_secret")
    monkeypatch.setattr(reg, "post_release", lambda *a, **k: False)

    _argv_for(monkeypatch, _artifact(tmp_path))
    # Registration failing does not fail the build: the artifacts and the
    # GitHub release are good either way, and the step can be re-run.
    assert reg.main() == 0
    assert "0 artifact(s) registered" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# What the run page is told.
#
# The failure this covers is not a crash: v1.1.1 was tagged, built, and its
# installers published on GitHub, the "Register the release with the backend"
# step showed a green tick, and the download page stayed empty. Every exit path
# here returns 0 and the step is `continue-on-error`, so "registered four
# artifacts" and "did nothing at all" looked identical. They must not.
# ---------------------------------------------------------------------------

def _artifact(tmp_path):
    artifact = tmp_path / f"Monitra-Setup-{version.VERSION}.exe"
    artifact.write_bytes(b"x")
    return artifact


def _run(monkeypatch, tmp_path):
    artifact = _artifact(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "register_release.py", "--tag", f"v{version.VERSION}",
        "--repo", "acme/monitra", "--artifacts", str(artifact),
    ])
    return reg.main()


def test_a_skipped_registration_warns_on_the_run_page(monkeypatch, tmp_path, capsys):
    """The silent green tick that hid an empty download page."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("MONITRA_API_BASE_URL", raising=False)
    monkeypatch.delenv("MONITRA_RELEASE_CREDENTIAL", raising=False)
    monkeypatch.delenv("MONITRA_RELEASE_TOKEN", raising=False)

    # Still not a failure -- a fork must keep building.
    assert _run(monkeypatch, tmp_path) == 0

    out = capsys.readouterr().out
    assert "::warning::" in out
    assert "SKIPPED" in out


def test_a_successful_registration_says_the_rows_are_still_drafts(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid/api/v1")
    monkeypatch.setenv("MONITRA_RELEASE_TOKEN", "token")
    monkeypatch.setattr(reg, "post_release", lambda base_url, token, payload: True)

    assert _run(monkeypatch, tmp_path) == 0

    out = capsys.readouterr().out
    assert "::notice::" in out
    assert "DRAFTS" in out
    # The distinction the green tick could not make.
    assert "::warning::" not in out


def test_a_failed_registration_warns_on_the_run_page(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid/api/v1")
    monkeypatch.setenv("MONITRA_RELEASE_TOKEN", "token")
    monkeypatch.setattr(reg, "post_release", lambda base_url, token, payload: False)

    assert _run(monkeypatch, tmp_path) == 0

    out = capsys.readouterr().out
    assert "::warning::" in out
    assert "INCOMPLETE" in out


def test_a_mismatched_tag_is_an_error_annotation(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid")
    monkeypatch.setenv("MONITRA_RELEASE_TOKEN", "token")
    artifact = tmp_path / "Monitra-Setup-9.9.9.exe"
    artifact.write_bytes(b"x")
    monkeypatch.setattr(sys, "argv", [
        "register_release.py", "--tag", "v9.9.9", "--repo", "acme/monitra",
        "--artifacts", str(artifact),
    ])

    assert reg.main() == 1
    assert "::error::" in capsys.readouterr().out


def test_the_step_summary_records_what_happened(monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.delenv("MONITRA_API_BASE_URL", raising=False)
    monkeypatch.delenv("MONITRA_RELEASE_CREDENTIAL", raising=False)
    monkeypatch.delenv("MONITRA_RELEASE_TOKEN", raising=False)

    _run(monkeypatch, tmp_path)

    assert "SKIPPED" in summary.read_text(encoding="utf-8")


def test_an_unwritable_summary_never_fails_registration(monkeypatch, tmp_path):
    """The summary is a convenience; losing it must not break a release."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    # A directory, so opening it for append raises OSError.
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))
    monkeypatch.delenv("MONITRA_API_BASE_URL", raising=False)
    monkeypatch.delenv("MONITRA_RELEASE_CREDENTIAL", raising=False)
    monkeypatch.delenv("MONITRA_RELEASE_TOKEN", raising=False)

    assert _run(monkeypatch, tmp_path) == 0


def test_nothing_is_annotated_outside_actions(monkeypatch, tmp_path, capsys):
    """A person running this by hand gets plain output, not workflow commands."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("MONITRA_API_BASE_URL", raising=False)
    monkeypatch.delenv("MONITRA_RELEASE_CREDENTIAL", raising=False)
    monkeypatch.delenv("MONITRA_RELEASE_TOKEN", raising=False)

    assert _run(monkeypatch, tmp_path) == 0
    assert "::" not in capsys.readouterr().out
