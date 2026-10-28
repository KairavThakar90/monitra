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
| **E** | Built, but not on screen | `IDLE_POPUP_NOT_ACKNOWLEDGED`, `IDLE_POPUP_RECOVERED kind=…`, `IDLE_POPUP_STALE`, `IDLE_POPUP_REBUILT`; `event=popup_recovered` |
| **F** | The backend refused or could not be reached | `IDLE_REPORT_REFUSED status=…` / `IDLE_REPORT_FAILED kind=… attempt=…` (desktop); `IDLE_REPORT_REJECTED reason=…` (backend). `reason=future_timestamps` or `before_entry_start` with a large `skew_s` is a wrong client clock |
| **G** | The OS will not say | `IDLE_READING_UNAVAILABLE reason=…`; `reading_failure` — `unsupported_platform:darwin` is **macOS, where no inactivity reading exists yet** |
| **H** | The process stopped | no `IDLE_HEALTH` lines, no reports at all — the machine was off, or Monitra was not running |
| **I** | Sleep / resume | `IDLE_SYSTEM_RESUMED`, `IDLE_SUSPEND_GAP`, `event=system_resumed` — the sleep is reported as an idle period; absence of these after a known sleep points to the monitor not running (A) |

## "The popup is stuck and Monitra does not respond"

The popup is application-modal: while it is up, nothing else in Monitra takes
a click. So this report means the popup was somewhere the user could not see
it. The desktop now checks that once a second against the operating system
and puts the popup back; the log says what it found:

1. `IDLE_POPUP_RECOVERED kind=<minimised|offscreen|os_hidden|other_desktop|hidden>
   reason=<tick|tray|second-launch> … screens=N [WxH@x,y×scale …]` — what
   was wrong, who asked, and the display configuration at the time. Logged
   loudly once per 30 s per kind; `event=popup_recovered detail=kind=… total=N`
   reaches the backend once per ten minutes. `other_desktop` is a Windows
   virtual desktop the user had switched away from; `offscreen` with
   `screens=1` after a session that had `screens=2` is an unplugged monitor.
2. `IDLE_POPUP_RECOVERY_BACKOFF kind=…` — the same thing was undone five
   times in a minute; something on that machine is hiding or minimising
   windows, and Monitra retries every 30 s instead of fighting it.
3. `WINDOW_RESTORE_REQUESTED reason=tray|second-launch` followed by
   `IDLE_POPUP_BROUGHT_FORWARD reason=… recovered=…` — the user clicked the
   tray icon or launched Monitra again, and the popup was brought to them.
   `IDLE_POPUP_REBUILT` — the dialog object was gone while the period was
   still pending; a fresh one was built.
4. `SINGLE_INSTANCE_ACTIVATION sent and taken` (second launch) and
   `SINGLE_INSTANCE_ACTIVATION received` (the running instance) — the
   hand-over worked. `SINGLE_INSTANCE_ACTIVATION unanswered` — the running
   instance did not take the request within 3 s: its GUI thread is blocked,
   or it was still starting; the user saw the "already running" explanation.
5. `AUTH_CLEANUP idle_popup=closed|none` — what the sign-out actually closed.
   Signing out never resolves a pending period: it lives on the server and
   the popup returns at the next sign-in (`GET /idle-periods/active`), which
   is correct and is why "signing out did not fix it" was true.

## The decision itself: what the log says about an answer

1. `IDLE_DECISION_STARTED period=… action=… keep=… attempt=N request=idle-resolve:<period>:<attempt> new|repeat|reconciling-repeat`
   — every answer sent, with the task key that names the request.
2. `IDLE_DECISION_CONFIRMED period … resolved: action=… counted=… entry_adjustment=…`
   — the backend's verdict; `(reconciled after an uncertain attempt)` when a
   lost reply was confirmed by repeating the same `resolved_at`.
3. `IDLE_DECISION_UNCERTAIN` — the answer outlived the 45-second deadline; the
   server may have applied it. The next attempt repeats it; nothing is applied twice.
4. `IDLE_DUPLICATE_PREVENTED` — a second request while one was in flight
   (service or dialog). Expected after a double click; never a fault.
5. `IDLE_DECISION_DEFERRED period=…` — the user chose Decide later on a confirmed
   period: still pending on the backend, nothing counted or discarded, the
   dashboard banner is up (`IDLE_DECISION_PENDING_SHOWN`). `IDLE_INTERRUPTION_DEFERRED`
   is the same for a gap not yet confirmed; `IDLE_DECISION_PENDING_DEFERRED` says the
   backend confirmed such a gap while deferred and it was not raised.
6. `IDLE_DECISION_REOPEN_REQUESTED reopened=…` / `IDLE_POPUP_REOPENED` — the user
   clicked the banner or launched Monitra again; the popup was rebuilt.
7. `IDLE_RETRY_NOW reconciled: the backend already holds period …` — a locked
   provisional popup found its confirmation without sending anything (the
   2026-10-27 failure, self-healed).

## "It got stuck on Confirming with the server…"

The 2026-10-27 cause: the backend's period carries no `client_op`, the
provisional popup matched on it, and the confirmation never unlocked the
dialog. Look for `idle period <id> pending on entry <e>` followed by *no*
`IDLE_DECISION_STARTED` and a popup the user says was disabled; fixed in
1.3.3 (see ARCHITECTURE.md "The confirmation must find its popup").

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
