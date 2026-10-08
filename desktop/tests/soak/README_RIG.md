# Stub-backend rig (resource measurement)

Runs the real desktop app -- from source or the packaged `Monitra.exe` -- signed in
and populated, against a local stdlib HTTP server. Test rig only: it lives under
`tests/soak/`, is never imported by product code, and talks to 127.0.0.1 only.

## 1. Start the server

    cd desktop\tests\soak
    python stub_backend_server.py --port 8765 --projects 40 --tasks-per-project 30 --screenshots 40

Takes ~5 s to start (it renders a pool of 1000x1000 WebPs of 200-230 KB, once).
Other options: `--user-email`, `--verbose`. Screenshots are dated today (IST) for the
signed-in user; uploaded ones are stored in memory and appear in the Activity listing.

Fault injection and counters (never faulted themselves):

    curl -X POST "http://127.0.0.1:8765/__admin/offline?on=1"        # 503 for everything else (&mode=drop hangs up)
    curl -X POST "http://127.0.0.1:8765/__admin/offline?on=0"
    curl -X POST "http://127.0.0.1:8765/__admin/fail_rate?p=0.3"     # random 500/502/503
    curl -X POST "http://127.0.0.1:8765/__admin/latency?ms=500"
    curl           http://127.0.0.1:8765/__admin/stats               # per-path counts, errors, uploads, active entries, connections

## 2. Seed a data directory (one per app under test)

    python seed_session.py --data-dir C:\scratch\data_src
    python seed_session.py --data-dir C:\scratch\data_exe

Uses the product's `StorageManager` + `LocalCache.save_session`; if the server is up it
signs in through it first. Refuses `~\.monitra` and any non-loopback `--api-base`.
The packaged build opens a DB written by the newer source schema without complaint.

## 3. Launch against the stub

`app/config.py` needs only a backend URL; the other variables keep the sign-in page and
the production-vs-localhost check honest. A real window needs `QT_QPA_PLATFORM` **unset**.
`MONITRA_DATA_DIR` also gives the instance its own single-instance lock, so it runs
beside an installed Monitra. A `desktop\.env` beside the app is overridden by the environment.

PowerShell:

    $env:SMS_API_BASE_URL = "http://127.0.0.1:8765"
    $env:MONITRA_AUTH_PROVIDER_LOGIN_URL = "http://127.0.0.1:8765/__portal/login"
    $env:MONITRA_DATA_DIR = "C:\scratch\data_src"
    $env:MONITRA_ENV = "development"          # required for the packaged exe: a frozen build refuses a loopback URL otherwise
    Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    python main.py                            # from desktop\
    # packaged: set MONITRA_DATA_DIR to the other dir, then
    dist\Monitra\Monitra.exe

Logs: `<data-dir>\logs\monitra.log`. The sign-in hops (`/__portal/login`, `/auth/sso/token`) are emulated and accept any credentials (checked with the product's `ApiClient`, not through the sign-in form).

## Notes

* `Start-Process python main.py -PassThru` returns a launcher PID whose child is the app;
  stop it with `taskkill /PID <pid> /T /F`.
* Not emulated (404/403, deliberately): task create/update/delete.
* `/desktop-notifications/schedule` answers version 0 with no reminders, so no wellbeing toasts fire.
