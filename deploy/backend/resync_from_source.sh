#!/usr/bin/env bash
# Re-seed Cloud SQL from another PostgreSQL database (the pre-migration Neon
# instance), without deleting anything: the current database is renamed to
# <name>_old_<timestamp>, a fresh dump is restored into a new database owned
# by the application role, and the backend is restarted against it.
#
# Runs ON the backend VM, as a user with sudo:
#   SOURCE_URL='postgresql://...' ADMIN_PASSWORD='...' sudo -E bash resync_from_source.sh
#
# SOURCE_URL      the database to copy from (use a direct, non-pooled endpoint)
# ADMIN_PASSWORD  password of the Cloud SQL `postgres` user
# APP_ROLE        application role that will own the data (default monitra_app)
# DB_HOST/DB_NAME Cloud SQL address and database name (defaults below)
set -euo pipefail
: "${SOURCE_URL:?SOURCE_URL is required}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required}"
DB_HOST="${DB_HOST:-35.200.162.243}"
DB_NAME="${DB_NAME:-monitra}"
APP_ROLE="${APP_ROLE:-monitra_app}"
ENV_FILE=/etc/monitra/backend.env
DUMP=/tmp/${DB_NAME}-resync.dump
OLD="${DB_NAME}_old_$(date -u +%Y%m%d%H%M%S)"

export PGSSLMODE=require PGCONNECT_TIMEOUT=10
APP_PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

# If anything fails after the rename, put the previous database back under
# its name and restart the backend against it. The env file is only changed
# after a verified restore, so it still points at the previous credentials.
RENAMED=0
rollback() {
  echo "!! failed -- rolling back"
  export PGPASSWORD="$ADMIN_PASSWORD"
  if [ "$RENAMED" = 1 ]; then
    psql -h "$DB_HOST" -U postgres -d postgres -q -o /dev/null <<SQL || true
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB_NAME' AND pid<>pg_backend_pid();
ALTER DATABASE "$DB_NAME" RENAME TO "${DB_NAME}_failed_$(date -u +%Y%m%d%H%M%S)";
ALTER DATABASE "$OLD" RENAME TO "$DB_NAME";
SQL
  fi
  systemctl start monitra-backend || true
  echo "previous database restored as $DB_NAME; backend restarted"
}
trap rollback ERR

echo "== stop backend =="
systemctl stop monitra-backend

echo "== dump source =="
pg_dump "$SOURCE_URL" --no-owner --no-privileges -Fc -f "$DUMP"
ls -la "$DUMP"

echo "== rename $DB_NAME -> $OLD; create $DB_NAME owned by $APP_ROLE =="
export PGPASSWORD="$ADMIN_PASSWORD"
RENAMED=1
psql -h "$DB_HOST" -U postgres -d postgres -v ON_ERROR_STOP=1 -q -o /dev/null <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB_NAME' AND pid<>pg_backend_pid();
ALTER DATABASE "$DB_NAME" RENAME TO "$OLD";
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='$APP_ROLE') THEN CREATE ROLE "$APP_ROLE" LOGIN; END IF;
END \$\$;
ALTER ROLE "$APP_ROLE" WITH LOGIN PASSWORD '$APP_PW';
GRANT "$APP_ROLE" TO postgres;
CREATE DATABASE "$DB_NAME" OWNER "$APP_ROLE";
SQL

echo "== restore as $APP_ROLE =="
export PGPASSWORD="$APP_PW"
pg_restore -h "$DB_HOST" -U "$APP_ROLE" -d "$DB_NAME" --no-owner --no-privileges "$DUMP" 2>"$DUMP.err" || true
echo "restore errors: $(grep -ci error "$DUMP.err" || true)"
grep -i error "$DUMP.err" | sort | uniq -c | head -5 || true

q() { psql -h "$DB_HOST" -U "$APP_ROLE" -d "$DB_NAME" -Atc "$1"; }
echo "tables: $(q "select count(*) from information_schema.tables where table_schema='public'")"
echo "alembic_version: $(q "select version_num from alembic_version")"
echo "users: $(q "select count(*) from users")"

echo "== point backend at $APP_ROLE and start =="
trap - ERR
sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://${APP_ROLE}:${APP_PW}@${DB_HOST}:5432/${DB_NAME}?sslmode=require|" "$ENV_FILE"
systemctl start monitra-backend
for _ in $(seq 1 30); do curl -fs -o /dev/null http://127.0.0.1:8000/health && break; sleep 1; done
curl -s http://127.0.0.1:8000/health; echo
echo "Done. Previous database kept as $OLD -- drop it once you are satisfied."
