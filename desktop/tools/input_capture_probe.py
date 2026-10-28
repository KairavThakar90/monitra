"""
input_capture_probe — does the real OS listener hear real input on this machine?

A unit test that calls the counter's callbacks proves the counting rules, not
the listener. This tool proves the listener: it starts the production
`InputEventCounter` and `InputProbe`, injects a known sequence of input events
through the operating system itself, and compares what the OS delivered
(`received`), what the counter kept (`snapshot_and_reset`) and what the
presence probe saw, against what was sent.

    python tools/input_capture_probe.py [--keys 20] [--moves 40] [--seconds 2]

Windows: events are injected with `SendInput` -- F15 key taps (a key no
application binds, so nothing is typed into the focused window) and small
relative pointer moves. Clicks are never injected: a synthetic click lands on
whatever is under the pointer.

macOS: injection would need `CGEventPost`, which itself needs Accessibility;
on a Mac run the tool with `--manual` instead and type/move by hand while it
listens, then compare the printed counts with what you did.

Exit status is 0 when every injected event was received and the counted
totals match (keys: one per tap, since F15 is never auto-repeated here; moves:
one per injected move), 1 otherwise. No key identity is ever recorded.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from background_services.activity.input_counter import InputEventCounter  # noqa: E402
from background_services.activity.input_probe import InputProbe  # noqa: E402

VK_F15 = 0x7E
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001


if sys.platform == "win32":
    from ctypes import wintypes

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    _user32 = ctypes.windll.user32
    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT

    def _send(inp: _INPUT) -> int:
        return _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    def tap_key(vk: int) -> int:
        down = _INPUT(type=INPUT_KEYBOARD)
        down.u.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0, time=0, dwExtraInfo=None)
        up = _INPUT(type=INPUT_KEYBOARD)
        up.u.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=None)
        return _send(down) + _send(up)

    def move_pointer(dx: int, dy: int) -> int:
        inp = _INPUT(type=INPUT_MOUSE)
        inp.u.mi = _MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=MOUSEEVENTF_MOVE, time=0, dwExtraInfo=None)
        return _send(inp)
else:
    def tap_key(vk: int) -> int:  # pragma: no cover - not Windows
        raise RuntimeError("injection is implemented for Windows only; use --manual")

    def move_pointer(dx: int, dy: int) -> int:  # pragma: no cover - not Windows
        raise RuntimeError("injection is implemented for Windows only; use --manual")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keys", type=int, default=20, help="F15 taps to inject")
    parser.add_argument("--moves", type=int, default=40, help="relative pointer moves to inject")
    parser.add_argument("--seconds", type=float, default=2.0, help="settle time before reading")
    parser.add_argument("--manual", action="store_true",
                        help="inject nothing; listen for --seconds and print what was heard")
    args = parser.parse_args()

    probe = InputProbe()
    counter = InputEventCounter(watch_keys={"ctrl"})
    started = counter.start()
    print(f"platform={sys.platform} listener_started={started} failure={counter.failure} "
          f"probe_supported={probe.supported}")
    if not started:
        print("FAIL: the listener did not start")
        return 1

    time.sleep(0.5)                 # let the hooks settle
    counter.snapshot_and_reset()    # discard anything heard while settling
    before = probe.last_input_age()

    sent_keys = sent_moves = 0
    if not args.manual:
        for _ in range(args.keys):
            if tap_key(VK_F15) == 2:
                sent_keys += 1
            time.sleep(0.01)
        for i in range(args.moves):
            if move_pointer(1 if i % 2 else -1, 0) == 1:
                sent_moves += 1
            time.sleep(0.01)
    else:
        print(f"listening for {args.seconds:.0f}s -- type and move the pointer now")

    time.sleep(args.seconds)
    counts = counter.snapshot_and_reset()
    diag = counter.diagnostics()
    after = probe.last_input_age()
    sample = probe.sample(1.5)
    counter.stop()

    print(f"injected: keys={sent_keys} moves={sent_moves}")
    print(f"received: {diag['received']}")
    print(f"counted:  {counts}")
    print(f"probe:    last_input_age before={before} after={after} sample={sample}")

    if args.manual:
        return 0
    ok = (
        diag["received"]["key_down"] >= sent_keys
        and diag["received"]["key_up"] >= sent_keys
        and counts["keystrokes"] == sent_keys
        and diag["received"]["move"] >= sent_moves
        and counts["movements"] >= sent_moves
    )
    print("PASS: the OS listener heard and the counter kept every injected event"
          if ok else "FAIL: injected events were not all received or counted")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
