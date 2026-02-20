# DIRE Remote Control Board (`rc.dire.et`)

This directory contains a standalone system control board for DIRE deployment operations.

## Goals

- Independent from CIS application stack runtime.
- Runs directly on server metal as a small Python service.
- File-based storage only (JSON config + SQLite runtime DB).
- React frontend is built into static files and served by Nginx.

## Structure

- `python-backend/` - minimal Python API service.
- `react-frontend/` - React UI (Vite build).
- `nginx.rc.dire.et.conf` - Nginx virtual host for `rc.dire.et`.
- `rc-control.service` - systemd unit for backend API.
- `rc-control.env.example` - backend environment template.
- `rc-control.sudoers.example` - least-privilege sudo template.
- `deploy.sh` - build + install helper for server setup.

## Backend security model

- API bound to `127.0.0.1:8765` by default.
- Nginx is the only public entrypoint.
- API requires `X-RC-Token` for all endpoints except health.
- Actions are restricted by allowlist from `python-backend/config.json`.

## File-based storage

- Runtime config: `python-backend/config.json`
- Runtime database: `/opt/rc-control/backend/data/health.sqlite3` (SQLite, WAL mode)
  - probe definitions and execution history
  - action audit log

## Expensive probes (cached in SQLite)

- `sms_health`
  - AfroMessage endpoint TCP + HTTP reachability checks
  - Optional CIS messaging DB signal checks:
    - outbox backlog
    - recent failed message count
- `nid_health`
  - National ID gateway TCP + HTTP reachability checks
  - Endpoint checks for `/nid/requestData` and `/nid/getData`

Probe definitions are in `python-backend/config.json` under `scheduled_probes`, and results are surfaced via `/api/v1/status` so the UI reads cached state instead of running expensive checks on every refresh.

## Disk visibility

The status payload includes a comprehensive disk report with per-filesystem space/inode usage plus watched path sizes (configured under `targets.disk_report`).

## First-time setup on server

```bash
cd /home/tewelde/src/cis10/deploy/site-config/dire/remot-control
chmod +x deploy.sh
./deploy.sh
```

Then:

1. Edit `/etc/rc-control.env` and set a strong `RC_ADMIN_TOKEN`.
2. Restart service: `sudo systemctl restart rc-control`
3. Issue TLS cert: `sudo certbot --nginx -d rc.dire.et --agree-tos --redirect`
4. Access `https://rc.dire.et`

## Recommended hardening

- Keep `rc-control` API private on localhost only.
- Rotate `RC_ADMIN_TOKEN` periodically.
- Restrict `sudoers` commands to exact service/container names if needed.
- Add Nginx access control (IP allowlist and/or HTTP Basic auth) for `rc.dire.et`.
