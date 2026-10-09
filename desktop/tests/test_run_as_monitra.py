"""
`tools/run_as_monitra.py`: running the source under the name "Monitra".

Windows names a notification after the raising program's file description, so
`python main.py` says "Python" on every toast. The launcher stamps a copy of the
interpreter. What is pinned: the version resource it writes is well formed and
carries the description; stamping replaces what was there and can be repeated; the
real interpreter is never modified; and a stamped copy really runs Python and
reports itself as Monitra to Windows.
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DESKTOP))
sys.path.insert(0, str(DESKTOP / "tools"))

import run_as_monitra as tool  # noqa: E402

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows version resources")


def test_the_version_resource_is_well_formed():
    blob = tool.build_version_resource("Monitra", "Monitra", "1.2.3")

    assert struct.unpack_from("<H", blob, 0)[0] == len(blob)               # the root says how long it is
    key = "VS_VERSION_INFO\0".encode("utf-16-le")
    assert blob[6:6 + len(key)] == key
    fixed_at = (6 + len(key) + 3) // 4 * 4
    assert struct.unpack_from("<I", blob, fixed_at)[0] == 0xFEEF04BD       # the fixed-info signature, where it must be
    for text in ("FileDescription", "Monitra", "1.2.3", "ProductName", "StringFileInfo", "VarFileInfo", "Translation"):
        assert (text + "\0").encode("utf-16-le") in blob, text


def test_the_description_is_the_one_asked_for():
    blob = tool.build_version_resource("Some Other Name")
    assert ("Some Other Name\0").encode("utf-16-le") in blob
    assert ("Python\0").encode("utf-16-le") not in blob


@pytest.mark.parametrize("version,expected", [("1.3.1", (1, 3, 1, 0)), ("2", (2, 0, 0, 0)), ("1.2.3.4", (1, 2, 3, 4)), ("1.x.3", (1, 0, 3, 0))])
def test_a_version_string_becomes_four_numbers(version, expected):
    assert tool._version_parts(version) == expected


@windows_only
def test_stamping_replaces_the_description_and_can_be_repeated(tmp_path):
    import shutil
    copy = tmp_path / "Copy.exe"
    shutil.copy2(sys.executable, copy)
    before = tool.read_file_description(copy)

    tool.stamp(copy, "Monitra")
    assert tool.read_file_description(copy) == "Monitra"
    tool.stamp(copy, "Monitra again")
    assert tool.read_file_description(copy) == "Monitra again"

    assert before != "Monitra"
    assert tool.read_file_description(Path(sys.executable)) == before      # the real interpreter is untouched


@windows_only
def test_a_stamped_copy_runs_python_and_reports_itself_as_monitra(tmp_path):
    runner = tool.prepare_runner(tmp_path / "runner", description="Monitra")

    assert runner.name == "Monitra.exe"
    assert tool.read_file_description(runner) == "Monitra"
    done = subprocess.run(
        [str(runner), "-c", "import sys, PySide6; print('ran', sys.version_info[0])"],
        capture_output=True, text=True, timeout=60, env=tool.runner_environment(), cwd=str(DESKTOP),
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ran 3"                                  # a real interpreter, with the app's packages


@windows_only
def test_preparing_the_runner_again_changes_nothing(tmp_path):
    first = tool.prepare_runner(tmp_path / "runner", description="Monitra")
    stamped = first.stat().st_mtime_ns

    second = tool.prepare_runner(tmp_path / "runner", description="Monitra")

    assert second == first and second.stat().st_mtime_ns == stamped        # no copy, no new stamp


@windows_only
def test_a_wrongly_stamped_runner_is_corrected(tmp_path):
    runner = tool.prepare_runner(tmp_path / "runner", description="Monitra")
    tool.stamp(runner, "Python")

    tool.prepare_runner(tmp_path / "runner", description="Monitra")

    assert tool.read_file_description(runner) == "Monitra"


def test_the_environment_lets_the_copy_find_the_library_and_the_apps_packages():
    # A host-native path: "C:/extra" contains the POSIX path separator, so on
    # the macOS release runners it split into "C" and "/extra".
    extra = str(DESKTOP.parent / "extra")
    env = tool.runner_environment([extra])
    assert env["PYTHONHOME"] == sys.base_prefix
    paths = env["PYTHONPATH"].split(os.pathsep)
    assert paths[0] == str(DESKTOP) and extra in paths
    assert len(paths) == len(set(paths))


def test_elsewhere_than_windows_it_says_so_and_does_nothing(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert tool.main([]) == 2
    assert "Windows" in capsys.readouterr().out


def test_what_the_launcher_stamps_by_default_is_the_notification_header_name():
    # ("Monitra — Staff Management": the product's full name, beside the Monitra logo)
    """`main()` calls `prepare_runner()` with no arguments: whatever it defaults to is what
    every notification says at the top."""
    import inspect
    default = inspect.signature(tool.prepare_runner).parameters["description"].default
    assert default == tool.NOTIFICATION_HEADER_NAME == "Monitra — Staff Management"


# ── `python main.py` runs through the stamped copy by itself ─────────────────

from core import dev_identity  # noqa: E402


@pytest.fixture
def source_run(monkeypatch):
    """The conditions of `python main.py` on Windows: not frozen, not the runner, nothing set."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Python\python.exe")
    monkeypatch.delenv(dev_identity.RELAUNCHED_ENV, raising=False)
    monkeypatch.delenv(dev_identity.DISABLE_ENV, raising=False)


def test_a_source_run_on_windows_relaunches(source_run):
    assert dev_identity.should_relaunch() is True


@pytest.mark.parametrize("change", [
    lambda mp: mp.setattr(sys, "platform", "linux"),
    lambda mp: mp.setattr(sys, "platform", "darwin"),
    lambda mp: mp.setattr(sys, "frozen", True, raising=False),                      # the installed build
    lambda mp: mp.setenv(dev_identity.RELAUNCHED_ENV, "1"),                         # already the child
    lambda mp: mp.setenv(dev_identity.DISABLE_ENV, "1"),                            # a developer opted out
    # already the copy -- spelt for the host: Path() on the macOS runners does
    # not split a backslash path and found no "Monitra.exe" in it
    lambda mp: mp.setattr(sys, "executable", str(Path("~/.monitra/dev-runner/Monitra.exe").expanduser())),
], ids=["linux", "macos", "installed build", "child", "opted out", "already the copy"])
def test_it_does_not_relaunch_when(source_run, monkeypatch, change):
    change(monkeypatch)
    assert dev_identity.should_relaunch() is False
    assert dev_identity.relaunch_if_needed(Path("main.py")) is None


class FakeChild:
    def __init__(self, waits):
        self.waits = list(waits)

    def wait(self):
        outcome = self.waits.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_it_runs_the_script_through_the_runner_with_its_arguments_and_returns_the_exit_code(source_run, monkeypatch):
    started = {}
    monkeypatch.setattr(dev_identity, "prepare_runner", lambda: Path(r"C:\runner\Monitra.exe"))

    def popen(command, env):
        started.update(command=command, env=env)
        return FakeChild([7])

    monkeypatch.setattr(dev_identity.subprocess, "Popen", popen)

    code = dev_identity.relaunch_if_needed(Path(r"C:\app\main.py"), ["--flag"])

    assert code == 7
    assert started["command"] == [r"C:\runner\Monitra.exe", r"C:\app\main.py", "--flag"]
    assert started["env"][dev_identity.RELAUNCHED_ENV] == "1"                     # so the child does not relaunch again
    assert started["env"]["PYTHONHOME"] == sys.base_prefix


def test_a_second_ctrl_c_does_not_cut_the_childs_shutdown_short(source_run, monkeypatch):
    """Ctrl+C reaches the child too, which shuts itself down; this process only waits."""
    monkeypatch.setattr(dev_identity, "prepare_runner", lambda: Path(r"C:\runner\Monitra.exe"))
    monkeypatch.setattr(dev_identity.subprocess, "Popen", lambda command, env: FakeChild([KeyboardInterrupt(), KeyboardInterrupt(), 0]))

    assert dev_identity.relaunch_if_needed(Path("main.py"), []) == 0


def test_if_the_runner_cannot_be_prepared_the_app_runs_in_place(source_run, monkeypatch, capsys):
    def broken():
        raise PermissionError("denied")

    monkeypatch.setattr(dev_identity, "prepare_runner", broken)
    monkeypatch.setattr(dev_identity.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start anything"))

    assert dev_identity.relaunch_if_needed(Path("main.py"), []) is None
    assert "running in place" in capsys.readouterr().err


def test_if_the_runner_cannot_be_started_the_app_runs_in_place(source_run, monkeypatch):
    monkeypatch.setattr(dev_identity, "prepare_runner", lambda: Path(r"C:\runner\Monitra.exe"))

    def refuse(*args, **kwargs):
        raise OSError("blocked")

    monkeypatch.setattr(dev_identity.subprocess, "Popen", refuse)

    assert dev_identity.relaunch_if_needed(Path("main.py"), []) is None


@windows_only
def test_a_real_relaunch_runs_the_script_in_the_stamped_copy_and_passes_the_exit_code_back(tmp_path, monkeypatch):
    for name in (dev_identity.RELAUNCHED_ENV, dev_identity.DISABLE_ENV):
        monkeypatch.delenv(name, raising=False)
    real_prepare = dev_identity.prepare_runner
    monkeypatch.setattr(dev_identity, "prepare_runner", lambda: real_prepare(tmp_path / "runner"))
    script = tmp_path / "fake_main.py"
    script.write_text(
        "import os, sys\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(sys.executable + '|' + os.environ.get('MONITRA_DEV_RELAUNCHED', ''))\n"
        "sys.exit(5)\n",
        encoding="utf-8",
    )
    record = tmp_path / "record.txt"

    code = dev_identity.relaunch_if_needed(script, [str(record)])

    assert code == 5
    ran_as, flag = record.read_text(encoding="utf-8").split("|")
    assert Path(ran_as).name == "Monitra.exe" and Path(ran_as).parent == tmp_path / "runner"
    assert dev_identity.read_file_description(Path(ran_as)) == "Monitra — Staff Management"
    assert flag == "1"


def test_main_py_hands_over_before_it_loads_anything_heavy():
    source = (DESKTOP / "main.py").read_text(encoding="utf-8")
    guard = source.index("dev_identity.relaunch_if_needed")
    assert guard < source.index("from PySide6"), "the relaunch must come before the heavy imports"
    assert 'if __name__ == "__main__":' in source[:guard][-400:]                  # and only when run as a script
