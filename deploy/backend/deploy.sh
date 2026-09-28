#!/usr/bin/env bash
# Deploy one backend release from a tarball of the backend/ directory.
#   sudo bash deploy.sh /path/to/backend.tgz
#
# Each release is unpacked into its own directory and /opt/monitra/backend is a
# symlink to the live one, so rolling back is re-pointing the symlink and
# restarting the service. Migrations run before the switch: a release whose
# migration fails never goes live.
set -euo pipefail

TARBALL="${1:?usage: deploy.sh /path/to/backend.tgz}"
ROOT=/opt/monitra
VENV="$ROOT/venv"
ENV_FILE=/etc/monitra/backend.env
RELEASE="$ROOT/releases/$(date -u +%Y%m%d%H%M%S)"
KEEP=3

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (run setup_vm.sh)"; exit 1; }

mkdir -p "$RELEASE"
tar -xzf "$TARBALL" -C "$RELEASE"
[ -f "$RELEASE/app/main.py" ] || { echo "tarball does not contain backend/app/main.py"; exit 1; }

"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$RELEASE/requirements.txt"

echo "Running migrations..."
set -a; . "$ENV_FILE"; set +a
(cd "$RELEASE" && "$VENV/bin/alembic" upgrade head)

chown -R monitra:monitra "$RELEASE"
ln -sfn "$RELEASE" "$ROOT/backend"
systemctl restart monitra-backend

echo "Waiting for /health..."
for _ in $(seq 1 30); do
  if curl -fs -o /dev/null http://127.0.0.1:8000/health; then
    echo "Deployed $RELEASE"
    ls -1dt "$ROOT"/releases/* | tail -n +$((KEEP + 1)) | xargs -r rm -rf
    exit 0
  fi
  sleep 1
done

echo "Backend did not become healthy. Recent log:"
journalctl -u monitra-backend -n 50 --no-pager
exit 1
