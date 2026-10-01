#!/usr/bin/env bash
# One-time preparation of the frontend VM. Idempotent; safe to re-run.
#   sudo BACKEND_UPSTREAM=10.160.0.2:80 SITE_DOMAIN=staff.example.com bash setup_vm.sh
#
# With SITE_DOMAIN set and a Let's Encrypt certificate present for it, the
# TLS site is installed. Otherwise the plain HTTP site is, which also serves
# the ACME challenge so the certificate can be obtained (README, "TLS").
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_UPSTREAM="${BACKEND_UPSTREAM:-10.160.0.2:80}"
SITE_DOMAIN="${SITE_DOMAIN:-}"

install -d -o root -g root -m 755 /var/www/monitra /var/www/monitra/releases /var/www/monitra/acme

SERVER_NAME="${SITE_DOMAIN:-_}"
TEMPLATE="$HERE/nginx-app.conf"
TLS=no
if [ -n "$SITE_DOMAIN" ] && [ -f "/etc/letsencrypt/live/$SITE_DOMAIN/fullchain.pem" ]; then
  TEMPLATE="$HERE/nginx-app-tls.conf"
  TLS=yes
fi

sed -e "s|__BACKEND_UPSTREAM__|$BACKEND_UPSTREAM|" -e "s|__SERVER_NAME__|$SERVER_NAME|g" \
  "$TEMPLATE" > /etc/nginx/sites-available/monitra-app
ln -sfn /etc/nginx/sites-available/monitra-app /etc/nginx/sites-enabled/monitra-app
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "Frontend VM prepared (site: $SERVER_NAME, tls: $TLS, API upstream: $BACKEND_UPSTREAM). Next: sudo bash deploy.sh /path/to/dist.tgz"
