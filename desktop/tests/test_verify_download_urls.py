"""
The published-download check (`tools/verify_download_urls.py`).

What it guards: a backend row that points at a file the web server will not
serve. Found for real on 2026-10-27 -- the website answered 200 for the Windows
installer and an HTML 404 for both .dmg files (no MIME type for .dmg on IIS),
so every Mac download button had been broken since the files were put there.
The judgement is tested here against canned fetch results; the network half is
exercised by running the tool against the real host (see the release runbook).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import verify_download_urls as vdu  # noqa: E402

BODY = b"MZ" + b"\x00" * 100
SHA = hashlib.sha256(BODY).hexdigest()


def _ok(url):
    return vdu.Fetched(200, "application/octet-stream", len(BODY), SHA, url)


def test_the_three_installers_a_version_needs_are_checked():
    names = vdu.artifact_names("1.3.3")
    assert set(names.values()) == {
        "Monitra-Setup-1.3.3.exe", "Monitra-macOS-arm64-1.3.3.dmg", "Monitra-macOS-x86_64-1.3.3.dmg",
    }


def test_a_complete_set_of_real_downloads_passes(tmp_path):
    for name in vdu.artifact_names("1.3.3").values():
        (tmp_path / name).write_bytes(BODY)
    assert vdu.verify("https://h.example/d/", "1.3.3", tmp_path, fetcher=_ok) == []


def test_an_html_404_for_a_dmg_names_the_mime_type_cause():
    def fetcher(url):
        if url.endswith(".dmg"):
            return vdu.Fetched(404, "text/html", 1245, None, url, error="HTTP 404")
        return _ok(url)

    problems = vdu.verify("https://h.example/d", "1.3.3", None, fetcher=fetcher)
    assert len([p for p in problems if "HTTP 404" in p]) == 2
    assert any("MIME" in p and "mimeMap" in p for p in problems)


def test_an_html_page_served_as_200_is_not_an_installer():
    def fetcher(url):
        return vdu.Fetched(200, "text/html; charset=utf-8", 512, hashlib.sha256(b"<html>").hexdigest(), url)

    problems = vdu.verify("https://h.example/d", "1.3.3", None, fetcher=fetcher)
    assert len(problems) == 3 and all("HTML page" in p for p in problems)


def test_a_file_that_differs_from_the_build_is_refused(tmp_path):
    for name in vdu.artifact_names("1.3.3").values():
        (tmp_path / name).write_bytes(BODY + b"x")
    problems = vdu.verify("https://h.example/d", "1.3.3", tmp_path, fetcher=_ok)
    assert len(problems) == 6, problems          # size and digest, per artifact
    assert any("bytes served" in p for p in problems) and any("SHA-256" in p for p in problems)


def test_a_sidecar_alone_checks_the_digest(tmp_path):
    for name in vdu.artifact_names("1.3.3").values():
        (tmp_path / (name + ".sha256")).write_text(f"{'0' * 64}  {name}\n")
    problems = vdu.verify("https://h.example/d", "1.3.3", tmp_path, fetcher=_ok)
    assert len(problems) == 3 and all("SHA-256" in p for p in problems)


def test_a_non_https_hop_is_a_problem():
    def fetcher(url):
        return vdu.Fetched(None, "", 0, None, "http://h.example/d/x", error="not https: http://h.example/d/x")

    problems = vdu.verify("https://h.example/d", "1.3.3", None, fetcher=fetcher)
    assert len(problems) == 3 and all("not https" in p for p in problems)


def test_the_cli_refuses_a_non_https_base(capsys):
    assert vdu.main(["--base-url", "http://h.example/d"]) == 2


def test_the_website_version_is_read_from_the_frontend_source():
    linked = vdu.frontend_download_version(Path(__file__).resolve().parent.parent.parent)
    assert linked is not None and linked.count(".") == 2
