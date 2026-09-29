#!/usr/bin/env bash
# Install (or refresh) Monitra's scheduled jobs on the backend VM. Idempotent.
#   sudo bash install.sh
#
# The production backend runs on a VM, so the "crons" in vercel.json do
# nothing there. These systemd timers are their VM equivalent -- same
# endpoints, same UTC schedules (tests/test_scheduled_jobs.py keeps the two in
# step). Re-run after pulling a change to anything in this directory.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOBS=(email-dispatch budget-alerts weekly-report monthly-report monthly-project-summary daily-rollup)

[ -f /etc/monitra/backend.env ] || { echo "missing /etc/monitra/backend.env (run setup_vm.sh first)"; exit 1; }
if ! grep -Eq '^EMAIL_DISPATCH_TOKEN=.+' /etc/monitra/backend.env; then
  echo "!! EMAIL_DISPATCH_TOKEN is empty in /etc/monitra/backend.env -- every job will fail until it is set."
fi

install -d -o root -g root -m 755 /opt/monitra/bin
install -o root -g root -m 755 "$HERE/monitra-job.sh" /opt/monitra/bin/monitra-job
install -o root -g root -m 644 "$HERE/monitra-job@.service" /etc/systemd/system/monitra-job@.service
for job in "${JOBS[@]}"; do
  install -o root -g root -m 644 "$HERE/monitra-job-$job.timer" "/etc/systemd/system/monitra-job-$job.timer"
done

systemctl daemon-reload
for job in "${JOBS[@]}"; do
  systemctl enable --now "monitra-job-$job.timer" >/dev/null
done

echo "Installed. Next runs:"
systemctl list-timers 'monitra-job-*' --no-pager
