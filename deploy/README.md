# Deploying Monitra to Google Cloud

Project `monitra-509314`, zone `asia-south1-c`.

| Role | VM | External IP | Internal IP | Serves |
|---|---|---|---|---|
| Frontend | `frontend` (e2-micro) | 8.234.122.232 | 10.160.0.3 | nginx: Vite build + `/api/` proxy to backend |
| Backend | `instance-20260921-144636` (e2-small) | 35.200.167.240 | 10.160.0.2 | nginx → uvicorn (`monitra-backend.service`) |
| Database | Cloud SQL `monitra-database` (PostgreSQL 18) | 35.200.162.243 | — | |

The web client is built with `VITE_API_BASE=/api/v1`, so browser requests are
same-origin and the frontend VM's nginx forwards `/api/` to the backend over
the VPC. No CORS entries are needed for the web client. The desktop app talks
to the backend host directly.

## One-time VM preparation

Base packages (already done on both VMs): `nginx git certbot python3-certbot-nginx`,
plus `python3-venv python3-pip libpq5` on the backend.

From a workstation with SSH access (`dell@<ip>`):

```bash
tar -czf deploy.tgz -C deploy .
for ip in 35.200.167.240 8.234.122.232; do scp deploy.tgz dell@$ip:/tmp/; done

# backend
ssh dell@35.200.167.240 'mkdir -p /tmp/deploy && tar -xzf /tmp/deploy.tgz -C /tmp/deploy && sudo bash /tmp/deploy/backend/setup_vm.sh'
# frontend
ssh dell@8.234.122.232 'mkdir -p /tmp/deploy && tar -xzf /tmp/deploy.tgz -C /tmp/deploy && sudo BACKEND_UPSTREAM=10.160.0.2:80 bash /tmp/deploy/frontend/setup_vm.sh'
```

`setup_vm.sh` creates the `monitra` system user, `/opt/monitra`, the venv, the
systemd unit, and the nginx site. It writes `/etc/monitra/backend.env` from
`backend.env.example` if none exists — **fill it in before the first deploy**
(`sudo nano /etc/monitra/backend.env`).

### Cloud SQL

The backend connects over the instance's public IP, so the backend VM's
external IP (`35.200.167.240/32`) must be listed under
Cloud SQL → `monitra-database` → Connections → Networking → Authorized networks.
Create an application user and database, and put them in `DATABASE_URL`.

The Alembic chain does not build a database from nothing: its first revision
assumes the base tables (`organizations`, `projects`, ...) already exist, so
`alembic upgrade head` on an empty database fails. Seed Cloud SQL by
restoring a dump of the existing database, which also carries the data and
the `alembic_version` row; deploys then apply only newer revisions.
`pg_dump` must be at least the source server's major version (Neon runs
PostgreSQL 18, so use `postgresql-client-18` from the PGDG repository).

`backend/resync_from_source.sh` does this end to end and is safe to repeat:
it renames the current database to `monitra_old_<timestamp>` rather than
dropping it, restores into a fresh `monitra` owned by the `monitra_app`
role, and restarts the backend against it.

```bash
# on the backend VM
SOURCE_URL='postgresql://...' ADMIN_PASSWORD='<postgres password>' \
  sudo -E bash /tmp/deploy/backend/resync_from_source.sh
```

## Deploying by hand

```bash
# backend
tar -czf backend.tgz -C backend --exclude=venv --exclude=__pycache__ --exclude='.env*' --exclude=tests .
scp backend.tgz dell@35.200.167.240:/tmp/
ssh dell@35.200.167.240 'sudo bash /tmp/deploy/backend/deploy.sh /tmp/backend.tgz'

# frontend
# MSYS_NO_PATHCONV matters in Git Bash on Windows: without it MSYS rewrites
# "/api/v1" into "C:/Program Files/Git/api/v1" and the build silently falls
# back to the old backend. Check: grep -c '`/api/v1`' frontend/dist/assets/index-*.js
(cd frontend && npm ci && MSYS_NO_PATHCONV=1 VITE_API_BASE=/api/v1 npm run build && tar -czf ../dist.tgz -C dist .)
scp dist.tgz dell@8.234.122.232:/tmp/
ssh dell@8.234.122.232 'sudo bash /tmp/deploy/frontend/deploy.sh /tmp/dist.tgz'
```

Each deploy is a new directory under `/opt/monitra/releases` (backend) or
`/var/www/monitra/releases` (frontend), with `current`/`backend` symlinks
pointing at the live one; the last three are kept.

**Rollback:** re-point the symlink and restart/reload.

```bash
sudo ln -sfn /opt/monitra/releases/<older> /opt/monitra/backend && sudo systemctl restart monitra-backend
sudo ln -sfn /var/www/monitra/releases/<older> /var/www/monitra/current && sudo systemctl reload nginx
```

## Continuous deployment

`.github/workflows/deploy-gcp.yml` deploys on every push to `main` that
touches `backend/`, `frontend/` or `deploy/`, and can be run by hand from the
Actions tab. It runs the backend tests and the frontend lint and build first,
then the same `deploy.sh` scripts over SSH.

One-time setup, by a repository admin, from a machine with SSH access to the
VMs:

```bash
ssh-keygen -t ed25519 -N "" -C github-actions-deploy -f gcp_deploy
for ip in 35.200.167.240 8.234.122.232; do
  ssh dell@$ip 'cat >> ~/.ssh/authorized_keys' < gcp_deploy.pub
done
gh secret set GCP_DEPLOY_SSH_KEY < gcp_deploy
rm gcp_deploy gcp_deploy.pub
```

The VMs' host keys are pinned in the workflow's `KNOWN_HOSTS`; refresh them
with `ssh-keyscan -t ed25519 35.200.167.240 8.234.122.232` if a VM is rebuilt.

### Cloud Build alternative

`cloudbuild-backend.yaml` and `cloudbuild-frontend.yaml` are the same
pipeline for Cloud Build, for a project that prefers to keep deployment inside
Google Cloud.

1. Connect the GitHub repository to Cloud Build (console → Cloud Build →
   Repositories → Connect).
2. Enable OS Login on both VMs and grant the Cloud Build service account
   (`<project-number>@cloudbuild.gserviceaccount.com`):
   `roles/compute.osAdminLogin`, `roles/compute.viewer`, and
   `roles/iam.serviceAccountUser` on the VMs' service account.
   ```bash
   gcloud compute instances add-metadata frontend --zone asia-south1-c --metadata enable-oslogin=TRUE
   gcloud compute instances add-metadata instance-20260921-144636 --zone asia-south1-c --metadata enable-oslogin=TRUE
   ```
3. Create the triggers:
   ```bash
   gcloud builds triggers create github --name=backend-main --repo-owner=<owner> --repo-name=staff-management-system \
     --branch-pattern='^main$' --build-config=cloudbuild-backend.yaml --included-files='backend/**,deploy/backend/**,cloudbuild-backend.yaml'
   gcloud builds triggers create github --name=frontend-main --repo-owner=<owner> --repo-name=staff-management-system \
     --branch-pattern='^main$' --build-config=cloudbuild-frontend.yaml --included-files='frontend/**,deploy/frontend/**,cloudbuild-frontend.yaml'
   ```

## TLS

The nginx site is rendered from the repository on every deploy, so the
certificate is obtained *outside* nginx's config (`certbot certonly`) and the
TLS server block lives in `deploy/frontend/nginx-app-tls.conf`. Do not use
`certbot --nginx`: its edits would be overwritten by the next deploy.

Once the DNS name points at the frontend VM (`staff.peakworkos.com` →
8.234.122.232):

```bash
# 1. plain HTTP site, which serves the ACME challenge
sudo SITE_DOMAIN=staff.peakworkos.com bash /tmp/deploy/frontend/setup_vm.sh
# 2. obtain the certificate (renews automatically via certbot's systemd timer)
sudo certbot certonly --webroot -w /var/www/monitra/acme -d staff.peakworkos.com \
  --non-interactive --agree-tos --register-unsafely-without-email \
  --deploy-hook 'systemctl reload nginx'
# 3. same command as step 1 -- now installs the TLS site and the 80 -> 443 redirect
sudo SITE_DOMAIN=staff.peakworkos.com bash /tmp/deploy/frontend/setup_vm.sh
```

`FRONTEND_DOMAIN` in `.github/workflows/deploy-gcp.yml` must match, so CI
keeps rendering the TLS site.

The backend works the same way with `deploy/backend/setup_vm.sh`, the
webroot `/var/www/acme`, and `BACKEND_DOMAIN` in the workflow:

```bash
sudo SITE_DOMAIN=api.peakworkos.com bash /tmp/deploy/backend/setup_vm.sh
sudo certbot certonly --webroot -w /var/www/acme -d api.peakworkos.com \
  --non-interactive --agree-tos --register-unsafely-without-email \
  --deploy-hook 'systemctl reload nginx'
sudo SITE_DOMAIN=api.peakworkos.com bash /tmp/deploy/backend/setup_vm.sh
```

The desktop client needs the API over HTTPS at a hostname (its production
URL must be `https://`, and it calls the API's bare paths, which the web
site's `/api/` proxy does not expose), so this is a prerequisite for pointing
the desktop at the new backend.

## Operations

```bash
sudo systemctl status monitra-backend
sudo journalctl -u monitra-backend -f
curl -s http://127.0.0.1:8000/health
```
