#!/usr/bin/env bash
# Deploy one frontend release from a tarball of the Vite dist/ directory.
#   sudo bash deploy.sh /path/to/dist.tgz
# /var/www/monitra/current is a symlink to the live release; rollback is
# re-pointing it and reloading nginx.
set -euo pipefail

TARBALL="${1:?usage: deploy.sh /path/to/dist.tgz}"
ROOT=/var/www/monitra
RELEASE="$ROOT/releases/$(date -u +%Y%m%d%H%M%S)"
KEEP=3

mkdir -p "$RELEASE"
tar -xzf "$TARBALL" -C "$RELEASE"
[ -f "$RELEASE/index.html" ] || { echo "tarball does not contain index.html"; exit 1; }
chown -R root:root "$RELEASE"
find "$RELEASE" -type d -exec chmod 755 {} + -o -type f -exec chmod 644 {} +

ln -sfn "$RELEASE" "$ROOT/current"
nginx -t
systemctl reload nginx

if curl -fsS -o /dev/null http://127.0.0.1/; then
  echo "Deployed $RELEASE"
  ls -1dt "$ROOT"/releases/* | tail -n +$((KEEP + 1)) | xargs -r rm -rf
else
  echo "nginx is not serving the new release"; exit 1
fi
