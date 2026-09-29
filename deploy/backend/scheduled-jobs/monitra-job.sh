#!/usr/bin/env bash
# Run one Monitra scheduled job by calling its internal endpoint on this VM.
#   monitra-job <job-name>
#
# Invoked by systemd (monitra-job@<job-name>.service, fired by the matching
# monitra-job-<job-name>.timer). The endpoint does the work; this only knocks.
# EMAIL_DISPATCH_TOKEN comes from /etc/monitra/backend.env via the unit's
# EnvironmentFile and is sent as a header -- it never appears in a process
# listing, a URL or the journal.
#
# Talks to uvicorn directly on 127.0.0.1:8000, not through nginx, so a TLS or
# proxy problem cannot stop a scheduled job. Exits non-zero on any failure so
# `systemctl status` / `journalctl` show it.
set -euo pipefail

case "${1:-}" in
  email-dispatch)          ENDPOINT=/internal/email/dispatch ;;
  budget-alerts)           ENDPOINT=/internal/project-budget-alerts/run ;;
  weekly-report)           ENDPOINT=/internal/reports/weekly/run ;;
  monthly-report)          ENDPOINT=/internal/reports/monthly/run ;;
  monthly-project-summary) ENDPOINT=/internal/reports/monthly-projects/run ;;
  daily-rollup)            ENDPOINT=/internal/activity/daily-rollup ;;
  *) echo "monitra-job: unknown job '${1:-}'" >&2; exit 2 ;;
esac

if [ -z "${EMAIL_DISPATCH_TOKEN:-}" ]; then
  echo "monitra-job: EMAIL_DISPATCH_TOKEN is not set in /etc/monitra/backend.env" >&2
  exit 3
fi

BASE="${MONITRA_JOB_BASE_URL:-http://127.0.0.1:8000}"
RESPONSE="$(mktemp)"
trap 'rm -f "$RESPONSE"' EXIT

STATUS="$(curl -sS -o "$RESPONSE" -w '%{http_code}' --max-time 840 -X POST \
  -H "X-Email-Dispatch-Token: ${EMAIL_DISPATCH_TOKEN}" "${BASE}${ENDPOINT}")" || {
  echo "monitra-job: $1 could not reach ${BASE}${ENDPOINT}" >&2
  exit 4
}

# The response bodies are tallies only (counts, periods) -- safe to log.
echo "monitra-job: $1 -> HTTP ${STATUS} $(head -c 600 "$RESPONSE")"
[ "$STATUS" -ge 200 ] && [ "$STATUS" -lt 300 ]
