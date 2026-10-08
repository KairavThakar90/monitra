# Idle popup diagnostics

How to answer, for one specific user and without access to their machine:

* **"Why did their idle popup not appear?"**
* **"Why did their idle popup get stuck on 'Confirming with the server…'?"**

Authoritative design: [desktop/ARCHITECTURE.md](../desktop/ARCHITECTURE.md)
("Idle reliability"), [TIMING_MODEL.md](TIMING_MODEL.md). Nothing below contains
a token, a URL, a window title, an application name or anything a user wrote.

## Where the evidence is

| Where | What | How to read it |
|---|---|---|
| Backend log, `app.services.time_entry_idle_period` | `IDLE_REPORT_REJECTED` — every refused report, with the rule and the figures | `grep IDLE_REPORT_REJECTED` |
| Backend log, same logger | `IDLE_CLIENT event=… user=<id> server_idle_enabled=… server_idle_minutes=… <fields>` — the desktop's own health reports | `grep "IDLE_CLIENT .* user=42 "` |
| Backend log | `idle period opened/resolved/reassigned` — the happy path | `grep "idle period" ` |
| Desktop log (`<data dir>\logs\monitra.log`, if the user can send it) | the `IDLE_*` / `SERVICE_LOOP_*` codes below | `findstr IDLE_ monitra.log` |

The desktop sends a report to the backend, at most once per ten minutes per
event, for: `reading_unavailable` / `reading_recovered`, `report_failed` (after
3, 10 and 30 consecutive failures), `inflight_stuck`, `popup_failed`,
`monitor_restarted`, `system_resumed`, and a `health` summary every six hours
while a timer runs. An older backend answers 404/405, which the desktop treats
as silence.

### Fields of an `IDLE_CLIENT` line

`state` (MONITORING / REPORTING / PENDING / RESOLVING / REASSIGNING),
`service_state`, `idle_enabled`, `idle_minutes`, `config_loaded` (the desktop's
view; compare with `server_idle_*`, which is the database's), `platform`,
`reading_supported`, `reading_failure` (why the OS could not say how long the
user had been idle), `reading_failures`, `seconds_since_tick` (how long since
the monitor last ran), `seconds_since_input`, `longest_idle_seconds` (the longest
inactivity the monitor has *seen* since start — see B below), `restarts` (monitor
restarts), `resumes` (sleeps noticed), `report_failures`, `last_error_kind` /
`last_error_status`, `last_api_latency_ms`, `pending_period_id`, `app_version`.

## "The popup never appeared" — find the letter

| | Cause | Evidence |
|---|---|---|
| **A** | Idle detection not running | `seconds_since_tick` large, `restarts` > 0, `SERVICE_LOOP_DIED` / `_STALLED` / `_REPLACED`; `IDLE_MONITOR_RESTARTED` |
| **B** | Running, but input is being seen | `longest_idle_seconds` stays below `idle_minutes × 60` while the user says they were away: something is generating input (a mouse jiggler, remote-control software, a USB device) — the OS reading is the OS's |
| **C** | Wrong threshold or disabled | `idle_enabled=False` or `idle_minutes` differs from `server_idle_*`; a `400 threshold_not_reached` in `IDLE_REPORT_REJECTED` shows the figures. (An administrator's setting used to be reverted at login — fixed.) |
| **D** | Reached, but the popup could not be built | `IDLE_POPUP_CREATE_FAILED` with a traceback; `event=popup_failed` |
| **E** | Built, but not on screen | `IDLE_POPUP_NOT_ACKNOWLEDGED`, `IDLE_POPUP_HIDDEN`, `IDLE_POPUP_STALE` |
| **F** | The backend refused or could not be reached | `IDLE_REPORT_REFUSED status=…` / `IDLE_REPORT_FAILED kind=… attempt=…` (desktop); `IDLE_REPORT_REJECTED reason=…` (backend). `reason=future_timestamps` or `before_entry_start` with a large `skew_s` is a wrong client clock |
| **G** | The OS will not say | `IDLE_READING_UNAVAILABLE reason=…`; `reading_failure` — `unsupported_platform:darwin` is **macOS, where no inactivity reading exists yet** |
| **H** | The process stopped | no `IDLE_HEALTH` lines, no reports at all — the machine was off, or Monitra was not running |
| **I** | Sleep / resume | `IDLE_SYSTEM_RESUMED`, `IDLE_SUSPEND_GAP`, `event=system_resumed` — the sleep is reported as an idle period; absence of these after a known sleep points to the monitor not running (A) |

## "It got stuck on Confirming with the server…"

That text belongs to the *provisional* popup shown for a crash, power-cut or
sleep gap. It now always has a way out (Retry now after 5 s, Decide later after
60 s) and a status line; if a user reports it anyway:

1. `IDLE_REPORT_FAILED origin=interruption|suspend|held kind=… status=… retry_in=…`
   — the reason and the backoff. `kind=auth` means the session needs renewing.
2. `IDLE_INTERRUPTION_WITHDRAWN reason=…` — why it was closed
   (`idle_disabled`, `session_changed`, `under_threshold`, `refused_<status>`,
   `timer_stopped`, `session_reset`).
3. `IDLE_INFLIGHT_TIMEOUT state=…` — a request outlived the 45-second deadline
   and was abandoned; the popup recovered by itself.
4. `IDLE_INTERRUPTION_DEFERRED` — the user chose Decide later.

## Sending a user's log

Neither the backend log nor the desktop log contains credentials: tokens and
authorization headers are never logged, URLs in error messages are redacted
(`app/api/exceptions.py`), and WFPM keys are scrubbed server-side. If you add a
log line, keep it that way.
