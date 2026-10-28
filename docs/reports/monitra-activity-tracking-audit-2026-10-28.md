# Monitra activity tracking audit — macOS and Windows, 2026-10-28

**Trigger.** A Mac user's October activity read 1–6 % on most days and 32–41 %
on three. Shipping version 1.3.2; this audit changes no version number.

**Verdict: PASS WITH VERIFICATION GAPS.** Every automated check passes, the
Windows listener was proved against real injected input on a Windows 11
machine, and the one failure mode the data points at is now detected, recovered
from where possible, and named to the user. Nothing in this audit ran on a Mac.

---

## 1. The formula (unchanged; now written down in one place)

Measured by the desktop, once per second, flushed once per 60-second window
(`desktop/background_services/activity/activity_service.py`):

```
scale   = window_seconds / 60
k_score = min(keyboard_strokes / (120 x scale), 1)
c_score = min(mouse_clicks     / ( 30 x scale), 1)
m_score = min(mouse_movements  / (400 x scale), 1)
window_percent = round(100 x (0.40 k_score + 0.30 c_score + 0.30 m_score))
```

| Input | Meaning | Source |
|---|---|---|
| `keyboard_strokes` | key-down events, one per physical press (OS auto-repeats dropped; on macOS every non-modifier key counts on each press) | `InputEventCounter` |
| `mouse_clicks` | button-down events, any button | `InputEventCounter` |
| `mouse_movements` | pointer-move / drag events as the OS delivers them (not sampled, not distance) | `InputEventCounter` |
| `window_seconds` | sampled seconds in the window (60, or the session's tail) | `ActivityService` |

Scrolling is never counted (chord partner only). `active_seconds` (presence) is
recorded beside the counts and is **not** in the percentage.

Every aggregate above a window is the same duration-weighted mean:

```
percent = SUM(window_percent x window_seconds) / SUM(window_seconds)
```

— desktop card (`today_summary.py`), `GET /time-entry-activities/today`,
the admin member page (`member_usage.daily_activity`), and the nightly
`daily_activity_summary` rollup. The denominator is **measured** seconds, never
tracked seconds: a minute with no window recorded does not count as 0 %.
`reports.py` still uses an unweighted `AVG(activity_percentage)` for the
per-entry and per-project report figures; identical while every window is 60 s,
slightly different for a session's tail window. Noted, not changed here.

Tests pinning the arithmetic: `tests/test_activity_percentage.py`,
`tests/test_today_activity.py`, `backend/tests/test_time_entry_activity.py`,
`backend/tests/test_daily_activity_rollup.py`.

## 2. What the October data says

Back-solving each day against the formula (input spread evenly over the day):

| Day | Shown | Implied measured span | Pointer events / min |
|---|---:|---:|---:|
| 06–08 Oct | 32–41 % | 12–20 h of windows | 470–860 |
| 02, 05, 09–14, 21, 27, 28 Oct | 1–6 % | 3–17 h of windows | 11–73 |
| 26 Oct | 14 % | ~5 min | 170 |

A trackpad or mouse in motion produces 60–125 pointer events **per second**.
Eleven to seventy-three per **minute**, for hours, is not a slower working day;
it is the volume a person sends to Monitra's own window (start, stop, pick a
task). 26 October is a five-minute session and the formula reads it correctly.

**Confirmed from this machine:** the low days are consistent with a listener
that was installed and delivered only the events aimed at Monitra itself, and
nothing in 1.3.2 could notice that — each window was recorded, uploaded and
averaged as a quiet minute. **Not provable from this machine:** which of the
causes below applied, or that the user was not simply away with the timer
running. The per-window rows decide it (§6).

Candidate causes for the trickle on a Mac:

1. Input Monitoring granted to a *different build*. macOS keys the grant to the
   bundle's code signature; Monitra's builds are unsigned (ad hoc), so an update
   or reinstall is a new app to TCC. 1.3.2 was built on 8 October; the low days
   begin on 9 October. Fits the dates.
2. The grant toggled after launch and never applied (macOS: "Quit & Reopen").
3. Secure Keyboard Entry (Terminal, iTerm2, a password manager) — withholds
   keys only; would not explain 12,000 pointer events over a day.
4. The user away with the timer running — possible for any one day; unlikely
   for nine days with the same shape.

## 3. Defects found and fixed

| # | Defect | Platform | Reproduced by | Fix |
|---|---|---|---|---|
| 1 | Named keys (Space, Enter, Tab, Backspace, Esc, arrows) counted once per minute: the tap reports a release only for modifiers, and the shared counter waited for one | macOS | replaying the tap's events through the counter: 8 presses counted as 4 (`tests/test_mac_input_tap.py`) | hold tracked only for `RELEASABLE_KEY_NAMES` (commit 712be63) |
| 2 | A denied Input Monitoring permission recorded nothing and the card said "Not tracking yet" | macOS | code reading; `tests/test_activity_capture_status.py` | `input_access.py`: state named, user told once, retried each minute (712be63) |
| 3 | A listener that is installed but deaf (grant not in effect; hook silently removed) is indistinguishable from an idle user | macOS, Windows | the October shape; `tests/test_activity_listener_deaf.py` | per-window `os_active` vs `heard`; restart once; `not_in_effect` named; **this commit** |
| 4 | No presence probe on macOS, so `active_seconds` was counts-only and #3 could not be checked there | macOS | `tests/test_input_probe_macos.py` | `InputProbe` reads the window server's HID idle clock (no permission), trusted once seen to run; idle detection stays off unless `MONITRA_MAC_IDLE_DETECTION=1` |
| 5 | Scroll not a chord partner on macOS, so CMD/CTRL+scroll tallied as bare modifier presses | macOS | `tests/test_mac_input_tap.py` | scroll in the tap mask, reported, never counted (712be63) |

Not a defect: the thresholds, weights, red/amber/green bands, the
duration-weighted denominator, IST day bounds, idempotent uploads
(`client_event_id`), partial-window scaling. None changed.

## 4. Diagnostics added (counts only; never content)

| Line | When | Tells |
|---|---|---|
| `activity capture started ... counting=, blocked=, platform=` | timer start | listener state at start |
| `activity window: a/s active (p%, keys, clicks, moves, os_active, heard)` | every window | both sides of the deaf check |
| `ACTIVITY_CAPTURE_BLOCKED state= detail=` | once per state | no permission / unavailable / not in effect |
| `ACTIVITY_LISTENER_DEAF windows= os_active= heard= counter= probe=` | each deaf window | OS saw input the listener did not; `counter.received` vs counted separates "OS delivered nothing" from "counter discarded it" |
| `ACTIVITY_LISTENER_RESTARTED ok=` | first deaf window | recovery attempted |
| `ACTIVITY_LISTENER_HEARING` / `ACTIVITY_CAPTURE_RESTORED` | recovery | state cleared |
| `ACTIVITY_CAPTURE_SILENT` | whole window, zero counts | idle user vs deaf listener via `os_idle_seconds` |
| `macOS idle clock confirmed live` | once | the presence source is trustworthy |

The eight states the brief asked to separate: no permission (`BLOCKED
state=denied`); listener unavailable (`state=unavailable`); started but
receiving nothing (`LISTENER_DEAF`, `received` all zero); events received but
discarded (`received` > 0, counts 0 — only auto-repeats/chords do this);
counted but not persisted (`could not persist activity window`); persisted but
not synchronised (sync's `pending_activity` lines, rows remain in the local
queue); stored but not on the dashboard (compare `/members/{id}/activity-log`
rows with the member page); genuinely low (`os_active` low, `heard` ≈
`os_active`).

## 5. Verification

| Layer | Evidence | Result |
|---|---|---|
| Unit — counting rules, formula, permission states, deaf check, probe trust | `desktop/tests` (13 activity modules) | 261 passed |
| Integration — service → cache → sync → backend payload | `test_activity_sync_pipeline.py`, `test_activity_offline_windows.py`, `test_network_activity_recovery.py` | in the full suite (see run log) |
| OS listener, **real device, Windows 11** | `tools/input_capture_probe.py`: 20 F15 taps + 40 pointer moves via `SendInput` | received 20/20/40, counted 20/40, probe clock reset 79.8 s → 2.0 s |
| OS listener, macOS | none | **not run** |
| Backend/database | `backend/tests` activity, rollup, activity-log, member reports | see run log |
| Desktop → dashboard end to end | not run against a backend in this audit | gap |
| Long-running | `run_soak.py --duration 60`, `run_launch_cycles.py --cycles 10` | see run log |

## 6. What the owner should pull (read-only) to close the macOS question

For the Mac user and each of 9, 13, 27 October:

1. `GET /api/v1/members/{member_id}/activity-log?date=YYYY-MM-DD` — the
   per-window rows (`recorded_at`, `window_seconds`, counts, percentage). A
   trickle concentrated in the minutes around start/stop with hours of 0-count
   windows between confirms a deaf listener; evenly spread low counts over a
   short tracked span means a short or idle day.
2. The Mac's `monitra.log` for `macOS input tap installed`, `ACTIVITY_CAPTURE_`
   and, after this build, `ACTIVITY_LISTENER_DEAF`.
3. System Settings → Privacy & Security → Input Monitoring: is Monitra listed,
   on, and was the app updated or reinstalled around 8–9 October?

Then, on the Mac with this build: start a timer, type a known sentence in
another app for a minute, and read the `activity window:` line (`heard` should
approach `os_active`; `keys` should match the sentence).

## 7. Remaining risks

- No macOS run of any kind: the deaf check, the HID idle clock's permission-free
  behaviour, the trust threshold, and the `not_in_effect` wording are all
  simulated. The clock is believed only once it reads ≥ 5 s, so a wrong
  assumption degrades to "no presence" rather than to false alarms.
- A Mac user typing for two full minutes in a secure-input field (password
  prompt, Secure Keyboard Entry) without moving the pointer will be told the
  listener is not receiving input. True, and not fixable; the wording says so.
- A Windows user working two full minutes only in administrator windows sees
  the same notice; the text names that limitation.
- The real fix for the suspected cause (a grant keyed to an ad hoc signature) is
  Developer ID signing and notarization — the outstanding release blocker.
- `reports.py` per-entry/per-project averages are unweighted (§1).
- Idle detection on macOS is available behind `MONITRA_MAC_IDLE_DETECTION=1`
  and has never run on hardware.
