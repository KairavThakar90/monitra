#!/usr/bin/env bash
# One-time preparation of the backend VM. Idempotent; safe to re-run.
#   sudo SITE_DOMAIN=api.example.com bash setup_vm.sh
#
# With SITE_DOMAIN set and a Let's Encrypt certificate present for it, the
# TLS site is installed. Otherwise the plain HTTP site is, which also serves
# the ACME challenge so the certificate can be obtained (README, "TLS").
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE_DOMAIN="${SITE_DOMAIN:-}"

if ! id monitra >/dev/null 2>&1; then
  useradd --system --home /opt/monitra --shell /usr/sbin/nologin monitra
fi

install -d -o root -g root -m 755 /opt/monitra /opt/monitra/releases
install -d -o root -g monitra -m 750 /etc/monitra

if [ ! -f /etc/monitra/backend.env ]; then
  install -o root -g monitra -m 640 "$HERE/backend.env.example" /etc/monitra/backend.env
  echo "!! /etc/monitra/backend.env created from the example. Fill it in before deploying."
fi

if [ ! -x /opt/monitra/venv/bin/python ]; then
  python3 -m venv /opt/monitra/venv
fi

install -o root -g root -m 644 "$HERE/monitra-backend.service" /etc/systemd/system/monitra-backend.service
systemctl daemon-reload
systemctl enable monitra-backend >/dev/null

install -d -o root -g root -m 755 /var/www/acme

SERVER_NAME="${SITE_DOMAIN:-_}"
TEMPLATE="$HERE/nginx-api.conf"
TLS=no
if [ -n "$SITE_DOMAIN" ] && [ -f "/etc/letsencrypt/live/$SITE_DOMAIN/fullchain.pem" ]; then
  TEMPLATE="$HERE/nginx-api-tls.conf"
  TLS=yes
fi

sed -e "s|__SERVER_NAME__|$SERVER_NAME|g" "$TEMPLATE" > /etc/nginx/sites-available/monitra-api
ln -sfn /etc/nginx/sites-available/monitra-api /etc/nginx/sites-enabled/monitra-api
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "Backend VM prepared (site: $SERVER_NAME, tls: $TLS). Next: sudo bash deploy.sh /path/to/backend.tgz"
