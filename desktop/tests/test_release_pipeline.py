"""
The release pipeline's own safeguards: the workflow file, the artifact gate, the
signature attestation, and what registration carries to the backend.

What is *not* tested here, and cannot be on a developer machine: that the
workflow runs on GitHub's runners, that signtool/codesign sign with real
credentials, and that notarization succeeds. Those need the repository's
secrets and runners. What is tested is everything about the pipeline that can be
wrong before it ever runs -- the structure of the workflow (an `if:` that can
never be true was how signing stayed silently skipped), the gate that refuses an
incomplete or unsigned set, and the verifier that reads a signature back from a
finished file.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

DESKTOP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DESKTOP))
sys.path.insert(0, str(DESKTOP / "tools"))

import version
from tools import attest_signature as attest  # noqa: E402
from tools import register_release as reg  # noqa: E402
from tools import verify_release_artifacts as verify  # noqa: E402
from _attestation import load_attestation, sidecar_path  # noqa: E402

WORKFLOW = DESKTOP.parent / ".github" / "workflows" / "desktop-release.yml"
VER = version.VERSION


@pytest.fixture(autouse=True)
def _a_release_build(monkeypatch):
    monkeypatch.setattr(version, "PRERELEASE", "")


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _names(job):
    return [step.get("name") or step.get("uses") for step in job["steps"]]


def _index(job, name):
    names = _names(job)
    assert name in names, f"step {name!r} is missing from {names}"
    return names.index(name)


# ════════════════════════════════════════════════════════════════════════════
# The workflow file
# ════════════════════════════════════════════════════════════════════════════


class TestWorkflowConditions:
    """A step's `if:` is evaluated before that step's own `env:` exists."""

    def test_the_workflow_is_valid_yaml_with_the_three_jobs(self, workflow):
        assert set(workflow["jobs"]) == {"windows", "macos", "release"}

    def test_no_step_condition_reads_an_env_var_only_its_own_step_defines(self, workflow):
        """The bug this guards: `if: env.WINDOWS_CERTIFICATE != ''` on a step whose
        own `env:` set the variable. It read an empty string, so the signing step
        never ran and the build stayed green and unsigned."""
        offenders = []
        for job_name, job in workflow["jobs"].items():
            visible = set(workflow.get("env", {})) | set(job.get("env", {}))
            for step in job["steps"]:
                condition = str(step.get("if", ""))
                for name in re.findall(r"\benv\.([A-Za-z_][A-Za-z0-9_]*)", condition):
                    if name not in visible:
                        offenders.append(f"{job_name}: {step.get('name')!r} reads env.{name}")
        assert offenders == [], offenders

    def test_no_step_condition_uses_the_secrets_context(self, workflow):
        # `secrets` is not available in a step `if`; the value has to be bound to
        # the environment of a step that runs, and carried forward as an output.
        offenders = [
            f"{job}: {step.get('name')!r}"
            for job, body in workflow["jobs"].items()
            for step in body["steps"]
            if "secrets." in str(step.get("if", ""))
        ]
        assert offenders == [], offenders

    def test_signing_is_decided_once_by_a_step_that_runs_and_read_by_outputs(self, workflow):
        windows, macos = workflow["jobs"]["windows"], workflow["jobs"]["macos"]
        for job in (windows, macos):
            detect = job["steps"][_index(job, "Detect the signing configuration")]
            assert detect["id"] == "signing"
            assert "run" in detect and detect.get("env"), "secrets must reach a step that RUNS"
        for name in ("Sign the application", "Sign the installer"):
            assert windows["steps"][_index(windows, name)]["if"] == "steps.signing.outputs.available == 'true'"
        assert macos["steps"][_index(macos, "Import signing certificate")]["if"] == \
            "steps.signing.outputs.certificate == 'true'"
        assert macos["steps"][_index(macos, "Create the notarization profile")]["if"] == \
            "steps.signing.outputs.notarize == 'true'"

    def test_every_output_a_condition_reads_is_defined_by_an_earlier_step(self, workflow):
        for job_name, job in workflow["jobs"].items():
            seen_ids = set()
            for step in job["steps"]:
                for step_id in re.findall(r"steps\.([A-Za-z_][\w-]*)\.outputs", str(step.get("if", ""))):
                    assert step_id in seen_ids, f"{job_name}: {step.get('name')!r} reads steps.{step_id} before it exists"
                if "id" in step:
                    seen_ids.add(step["id"])


class TestWindowsOrder:

    def test_the_application_is_signed_before_the_installer_is_built(self, workflow):
        job = workflow["jobs"]["windows"]
        assert _index(job, "Build application") < _index(job, "Sign the application") \
            < _index(job, "Build installer")

    def test_the_installer_is_signed_after_it_is_built_and_verified_after_that(self, workflow):
        job = workflow["jobs"]["windows"]
        assert _index(job, "Build installer") < _index(job, "Sign the installer") \
            < _index(job, "Verify the installer's signature and record it")

    def test_everything_is_signed_before_anything_is_hashed_or_zipped(self, workflow):
        job = workflow["jobs"]["windows"]
        verified = _index(job, "Verify the installer's signature and record it")
        assert verified < _index(job, "Checksums")
        assert verified < _index(job, "Build portable package")

    def test_the_application_signature_is_verified_in_the_same_step_that_makes_it(self, workflow):
        job = workflow["jobs"]["windows"]
        run = job["steps"][_index(job, "Sign the application")]["run"]
        assert "signtool.exe" in run and "verify /pa" in run

    def test_signing_never_leaves_the_certificate_on_disk(self, workflow):
        job = workflow["jobs"]["windows"]
        for name in ("Sign the application", "Sign the installer"):
            run = job["steps"][_index(job, name)]["run"]
            assert "Remove-Item $pfx" in run and "finally" in run


class TestMacOSOrder:

    def test_the_dmg_is_attested_before_it_is_hashed(self, workflow):
        job = workflow["jobs"]["macos"]
        assert _index(job, "Verify the dmg's signature and record it") < _index(job, "Checksums")

    def test_notarization_failure_fails_the_build_only_when_it_was_configured(self, workflow):
        job = workflow["jobs"]["macos"]
        step = job["steps"][_index(job, "Verify the dmg's signature and record it")]
        assert step["env"]["NOTARIZED"] == "${{ steps.signing.outputs.notarize }}"
        assert "--require-signed" in step["run"]


class TestReleaseJob:

    def test_the_whole_set_is_verified_before_anything_is_published(self, workflow):
        job = workflow["jobs"]["release"]
        verified = _index(job, "Verify the artifact set")
        assert verified < _index(job, "Publish")
        assert verified < _index(job, "Register the release with the backend")

    def test_the_gate_checks_the_tag_the_release_will_be_filed_under(self, workflow):
        job = workflow["jobs"]["release"]
        run = job["steps"][_index(job, "Verify the artifact set")]["run"]
        assert '--tag "${RELEASE_TAG}"' in run

    def test_signed_releases_can_be_required_by_a_repository_variable(self, workflow):
        job = workflow["jobs"]["release"]
        step = job["steps"][_index(job, "Verify the artifact set")]
        assert "REQUIRE_SIGNED_RELEASE" in step["env"]["REQUIRE_SIGNED"]
        assert "--require-signed" in step["run"]

    def test_the_github_release_is_still_a_draft_and_registration_still_cannot_publish(self, workflow):
        job = workflow["jobs"]["release"]
        publish = job["steps"][_index(job, "Publish")]
        assert publish["with"]["draft"] is True
        register = job["steps"][_index(job, "Register the release with the backend")]
        assert "/publish" not in register["run"]
        assert register["continue-on-error"] is True

    def test_registration_uses_only_the_service_credential_secrets(self, workflow):
        job = workflow["jobs"]["release"]
        env = job["steps"][_index(job, "Register the release with the backend")]["env"]
        assert set(env) == {"MONITRA_API_BASE_URL", "MONITRA_RELEASE_CREDENTIAL"}

    def test_the_attestations_travel_with_the_artifacts(self, workflow):
        for job_name in ("windows", "macos"):
            job = workflow["jobs"][job_name]
            upload = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact")][0]
            assert ".signature.json" in upload["with"]["path"]


# ════════════════════════════════════════════════════════════════════════════
# The artifact gate
# ════════════════════════════════════════════════════════════════════════════


def _build(tmp_path, *, skip=(), sidecars=True, signed=None):
    """A realistic `artifacts/` tree for VER. `signed` maps artifact name -> bool
    (or None for no attestation)."""
    names = {
        "win32": f"Monitra-Setup-{VER}.exe",
        "arm64": f"Monitra-macOS-arm64-{VER}.dmg",
        "x86_64": f"Monitra-macOS-x86_64-{VER}.dmg",
    }
    for key, name in names.items():
        if key in skip:
            continue
        folder = tmp_path / f"Monitra-{key}-{VER}"
        folder.mkdir(parents=True, exist_ok=True)
        artifact = folder / name
        artifact.write_bytes((name * 200).encode())
        if sidecars:
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            (folder / f"{name}.sha256").write_text(f"{digest}  {name}\n")
        if signed is not None and name in signed and signed[name] is not None:
            sidecar_path(artifact).write_text(json.dumps({
                "artifact": name,
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "signed": signed[name], "signer": "Monitra Ltd" if signed[name] else None,
                "reason": None if signed[name] else "the file is not signed",
            }))
    # The portable zip is shipped but is not an installer.
    (tmp_path / f"Monitra-Portable-{VER}.zip").write_bytes(b"zip")
    return names


class TestArtifactGate:

    def test_a_complete_intact_set_is_releasable(self, tmp_path):
        _build(tmp_path)
        assert verify.verify(tmp_path, f"v{VER}", require_signed=False) == []

    def test_a_missing_platform_stops_the_release(self, tmp_path):
        _build(tmp_path, skip=("x86_64",))
        problems = verify.verify(tmp_path, f"v{VER}", require_signed=False)
        assert len(problems) == 1 and "darwin/x86_64" in problems[0] and "missing" in problems[0]

    def test_an_empty_build_directory_reports_every_artifact(self, tmp_path):
        assert len(verify.verify(tmp_path, f"v{VER}", require_signed=False)) == 3

    def test_a_tag_that_disagrees_with_version_py_stops_the_release(self, tmp_path):
        _build(tmp_path)
        problems = verify.verify(tmp_path, "v9.9.9", require_signed=False)
        assert any("does not match version.py" in p for p in problems)

    def test_a_pre_release_build_is_never_releasable(self, tmp_path, monkeypatch):
        _build(tmp_path)
        monkeypatch.setattr(version, "PRERELEASE", "beta.1")
        assert any("pre-release" in p for p in verify.verify(tmp_path, f"v{VER}", require_signed=False))

    def test_an_installer_for_a_different_version_is_not_this_release(self, tmp_path):
        _build(tmp_path)
        win = next(tmp_path.rglob(f"Monitra-Setup-{VER}.exe"))
        wrong = win.with_name("Monitra-Setup-0.0.1.exe")
        win.rename(wrong)
        problems = verify.verify(tmp_path, f"v{VER}", require_signed=False)
        assert any("win32" in p for p in problems)

    def test_an_empty_installer_is_refused(self, tmp_path):
        _build(tmp_path)
        next(tmp_path.rglob("Monitra-macOS-arm64-*.dmg")).write_bytes(b"")
        assert any("empty" in p for p in verify.verify(tmp_path, f"v{VER}", require_signed=False))

    def test_a_checksum_that_is_not_the_files_is_refused(self, tmp_path):
        _build(tmp_path)
        sidecar = next(tmp_path.rglob("Monitra-Setup-*.exe.sha256"))
        sidecar.write_text("0" * 64 + f"  Monitra-Setup-{VER}.exe\n")
        assert any(".sha256 says" in p for p in verify.verify(tmp_path, f"v{VER}", require_signed=False))

    def test_a_missing_checksum_is_refused(self, tmp_path):
        _build(tmp_path, sidecars=False)
        problems = verify.verify(tmp_path, f"v{VER}", require_signed=False)
        assert sum(".sha256 is missing" in p for p in problems) == 3

    def test_two_files_claiming_one_artifact_are_refused(self, tmp_path):
        _build(tmp_path)
        win = next(tmp_path.rglob(f"Monitra-Setup-{VER}.exe"))
        other = tmp_path / "elsewhere"
        other.mkdir()
        shutil.copy(win, other / win.name)
        assert any("2 files claim" in p for p in verify.verify(tmp_path, f"v{VER}", require_signed=False))

    def test_the_portable_zip_cannot_stand_in_for_an_installer(self, tmp_path):
        _build(tmp_path, skip=("win32",))
        assert any("win32" in p for p in verify.verify(tmp_path, f"v{VER}", require_signed=False))

    def test_unsigned_is_acceptable_only_until_signing_is_required(self, tmp_path):
        names = _build(tmp_path, signed={})
        assert verify.verify(tmp_path, f"v{VER}", require_signed=False) == []
        problems = verify.verify(tmp_path, f"v{VER}", require_signed=True)
        assert sum("no signature attestation" in p for p in problems) == len(names)

    def test_an_attestation_that_says_unsigned_blocks_a_signed_release(self, tmp_path):
        names = _build(tmp_path, signed={n: (n.endswith(".exe") is False) for n in
                                         (f"Monitra-Setup-{VER}.exe", f"Monitra-macOS-arm64-{VER}.dmg",
                                          f"Monitra-macOS-x86_64-{VER}.dmg")})
        problems = verify.verify(tmp_path, f"v{VER}", require_signed=True)
        assert len(problems) == 1 and "not validly signed" in problems[0] and "Setup" in problems[0]

    def test_a_fully_signed_set_passes_the_signed_gate(self, tmp_path):
        every = {f"Monitra-Setup-{VER}.exe": True, f"Monitra-macOS-arm64-{VER}.dmg": True,
                 f"Monitra-macOS-x86_64-{VER}.dmg": True}
        _build(tmp_path, signed=every)
        assert verify.verify(tmp_path, f"v{VER}", require_signed=True) == []

    def test_an_attestation_left_over_from_another_build_is_not_trusted(self, tmp_path):
        every = {f"Monitra-Setup-{VER}.exe": True, f"Monitra-macOS-arm64-{VER}.dmg": True,
                 f"Monitra-macOS-x86_64-{VER}.dmg": True}
        _build(tmp_path, signed=every)
        # The installer is rebuilt (different bytes) but the old sidecar remains.
        win = next(tmp_path.rglob(f"Monitra-Setup-{VER}.exe"))
        win.write_bytes(b"a different, rebuilt installer")
        (win.with_name(win.name + ".sha256")).write_text(
            hashlib.sha256(win.read_bytes()).hexdigest() + f"  {win.name}\n")
        problems = verify.verify(tmp_path, f"v{VER}", require_signed=True)
        assert any("win32" in p and "no signature attestation" in p for p in problems)

    def test_the_cli_exit_code_follows_the_verdict(self, tmp_path, capsys):
        _build(tmp_path)
        assert verify.main(["--artifacts", str(tmp_path), "--tag", f"v{VER}"]) == 0
        assert verify.main(["--artifacts", str(tmp_path), "--tag", "v0.0.1"]) == 1
        assert "NOT releasable" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════════
# The signature attestation
# ════════════════════════════════════════════════════════════════════════════


CODESIGN_SAMPLE = """\
Executable=/Volumes/x/Monitra.app/Contents/MacOS/Monitra
Identifier=com.monitra.desktop
Format=app bundle with Mach-O thin (arm64)
CodeDirectory v=20500 size=1234 flags=0x10000(runtime) hashes=30+7 location=embedded
Authority=Developer ID Application: Monitra Ltd (ABCDE12345)
Authority=Developer ID Certification Authority
Authority=Apple Root CA
TeamIdentifier=ABCDE12345
"""


class TestAttestation:

    def test_the_signer_and_team_are_read_from_codesign_output(self):
        assert attest.parse_codesign_display(CODESIGN_SAMPLE) == (
            "Developer ID Application: Monitra Ltd (ABCDE12345)", "ABCDE12345")

    def test_an_ad_hoc_signature_has_no_team(self):
        assert attest.parse_codesign_display("Authority=(unavailable)\nTeamIdentifier=not set\n") \
            == ("(unavailable)", None)

    def test_nothing_parseable_is_none_not_a_guess(self):
        assert attest.parse_codesign_display("") == (None, None)

    def test_the_sidecar_is_named_for_the_artifact(self, tmp_path):
        assert sidecar_path(tmp_path / "Monitra-Setup-1.exe").name == "Monitra-Setup-1.exe.signature.json"

    def test_a_missing_or_garbled_sidecar_means_not_signed_never_signed(self, tmp_path):
        artifact = tmp_path / "Monitra-Setup-1.exe"
        artifact.write_bytes(b"x")
        assert load_attestation(artifact) is None
        sidecar_path(artifact).write_text("not json")
        assert load_attestation(artifact) is None
        sidecar_path(artifact).write_text("[1, 2]")
        assert load_attestation(artifact) is None

    def test_a_sidecar_made_for_different_bytes_is_ignored(self, tmp_path):
        artifact = tmp_path / "Monitra-Setup-1.exe"
        artifact.write_bytes(b"first build")
        sidecar_path(artifact).write_text(json.dumps(
            {"artifact_sha256": hashlib.sha256(b"first build").hexdigest(), "signed": True}))
        assert load_attestation(artifact)["signed"] is True
        artifact.write_bytes(b"rebuilt")
        assert load_attestation(artifact) is None

    @pytest.mark.skipif(sys.platform != "win32", reason="WinVerifyTrust is Windows-only")
    def test_an_unsigned_installer_is_attested_unsigned_and_fails_when_signing_is_required(self, tmp_path, capsys):
        artifact = tmp_path / f"Monitra-Setup-{VER}.exe"
        artifact.write_bytes(b"MZ" + b"\0" * 4000)
        assert attest.main(["--platform", "win32", "--artifact", str(artifact)]) == 0
        record = json.loads(sidecar_path(artifact).read_text())
        assert record["signed"] is False and record["signer"] is None
        assert record["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert attest.main(["--platform", "win32", "--artifact", str(artifact), "--require-signed"]) == 1

    @pytest.mark.skipif(sys.platform != "win32", reason="WinVerifyTrust is Windows-only")
    def test_a_validly_signed_binary_is_attested_signed_with_its_signer(self, tmp_path):
        from background_services.update import signature

        candidates = [Path(sys.executable)]
        signed = next((c for c in candidates if signature.verify_authenticode(c).trusted), None)
        if signed is None:
            pytest.skip("no validly signed executable on this machine")
        copy = tmp_path / f"Monitra-Setup-{VER}.exe"
        shutil.copy(signed, copy)
        assert attest.main(["--platform", "win32", "--artifact", str(copy), "--require-signed"]) == 0
        record = json.loads(sidecar_path(copy).read_text())
        assert record["signed"] is True and record["signer"]

    @pytest.mark.skipif(sys.platform != "win32", reason="WinVerifyTrust is Windows-only")
    def test_a_signature_from_an_unpinned_publisher_is_attested_unsigned(self, tmp_path, monkeypatch):
        from background_services.update import policy, signature

        if not signature.verify_authenticode(Path(sys.executable)).trusted:
            pytest.skip("no validly signed executable on this machine")
        monkeypatch.setattr(policy, "WINDOWS_SIGNER_PINS", ("Some Other Publisher",))
        copy = tmp_path / f"Monitra-Setup-{VER}.exe"
        shutil.copy(sys.executable, copy)
        assert attest.main(["--platform", "win32", "--artifact", str(copy), "--require-signed"]) == 1
        assert json.loads(sidecar_path(copy).read_text())["signed"] is False

    def test_a_missing_artifact_is_an_error_not_a_pass(self, tmp_path):
        assert attest.main(["--platform", "win32", "--artifact", str(tmp_path / "nope.exe")]) == 2


# ════════════════════════════════════════════════════════════════════════════
# What registration carries to the backend
# ════════════════════════════════════════════════════════════════════════════


def _register(monkeypatch, tmp_path, artifacts):
    posted = []
    monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid")
    monkeypatch.setenv("MONITRA_RELEASE_CREDENTIAL", "msk_abc_secret")
    monkeypatch.setattr(reg, "post_release", lambda base, token, payload: posted.append(payload) or True)
    monkeypatch.setattr(sys, "argv", ["register_release.py", "--tag", f"v{VER}",
                                      "--repo", "acme/monitra", "--artifacts", *map(str, artifacts)])
    return posted


class TestRegistrationCarriesSigning:

    def _artifact(self, tmp_path, name, attestation=None):
        path = tmp_path / name
        path.write_bytes(name.encode() * 100)
        if attestation is not None:
            sidecar_path(path).write_text(json.dumps(
                {"artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **attestation}))
        return path

    def test_a_signed_artifact_is_registered_as_signed_with_its_signer(self, monkeypatch, tmp_path):
        win = self._artifact(tmp_path, f"Monitra-Setup-{VER}.exe", {"signed": True, "signer": "Monitra Ltd"})
        posted = _register(monkeypatch, tmp_path, [win])
        assert reg.main() == 0
        assert posted[0]["signed"] is True and posted[0]["signer"] == "Monitra Ltd"

    def test_an_artifact_with_no_attestation_is_registered_unsigned(self, monkeypatch, tmp_path):
        win = self._artifact(tmp_path, f"Monitra-Setup-{VER}.exe")
        posted = _register(monkeypatch, tmp_path, [win])
        reg.main()
        assert posted[0]["signed"] is False and posted[0]["signer"] is None

    def test_an_attestation_for_a_different_file_is_registered_unsigned(self, monkeypatch, tmp_path):
        win = self._artifact(tmp_path, f"Monitra-Setup-{VER}.exe", {"signed": True, "signer": "Monitra Ltd"})
        win.write_bytes(b"rebuilt after the attestation was written")
        posted = _register(monkeypatch, tmp_path, [win])
        reg.main()
        assert posted[0]["signed"] is False

    def test_an_attestation_that_says_unsigned_is_registered_unsigned(self, monkeypatch, tmp_path):
        win = self._artifact(tmp_path, f"Monitra-Setup-{VER}.exe", {"signed": False, "signer": None})
        posted = _register(monkeypatch, tmp_path, [win])
        reg.main()
        assert posted[0]["signed"] is False

    def test_a_truthy_but_not_true_flag_is_not_signed(self, monkeypatch, tmp_path):
        win = self._artifact(tmp_path, f"Monitra-Setup-{VER}.exe", {"signed": "yes", "signer": "x"})
        posted = _register(monkeypatch, tmp_path, [win])
        reg.main()
        assert posted[0]["signed"] is False

    def test_a_missing_platform_is_an_error_annotation_but_registration_still_happens(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        win = self._artifact(tmp_path, f"Monitra-Setup-{VER}.exe")
        posted = _register(monkeypatch, tmp_path, [win])
        assert reg.main() == 0
        out = capsys.readouterr()
        assert len(posted) == 1
        assert "::error::This release is INCOMPLETE" in out.out
        assert "darwin/arm64" in out.out and "darwin/x86_64" in out.out

    def test_a_complete_set_raises_no_incomplete_warning(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        artifacts = [self._artifact(tmp_path, n) for n in (
            f"Monitra-Setup-{VER}.exe", f"Monitra-macOS-arm64-{VER}.dmg", f"Monitra-macOS-x86_64-{VER}.dmg")]
        posted = _register(monkeypatch, tmp_path, artifacts)
        reg.main()
        assert len(posted) == 3
        assert "INCOMPLETE" not in capsys.readouterr().out

    def test_registering_twice_is_idempotent_at_the_script_level(self, monkeypatch, tmp_path):
        # A 409 from the backend means "already registered", which re-running the
        # pipeline for the same tag must treat as success, never as a failure.
        import urllib.error

        win = self._artifact(tmp_path, f"Monitra-Setup-{VER}.exe")
        monkeypatch.setenv("MONITRA_API_BASE_URL", "https://api.invalid")
        monkeypatch.setenv("MONITRA_RELEASE_CREDENTIAL", "msk_abc_secret")

        def conflict(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, None)

        monkeypatch.setattr(reg.urllib.request, "urlopen", conflict)
        assert reg.post_release("https://api.invalid", "msk_abc_secret", {}) is True
