# Remote Control Python Backend

Lightweight Python service (no third-party dependencies) that exposes a secure JSON API for monitoring and administrative actions.

Privileged actions are executed by a separate root helper (`privileged_helper.py`) via a local Unix socket.

## API

- `GET /api/v1/health` (no token)
- `GET /api/v1/status`
- `GET /api/v1/config`
- `GET /api/v1/audit?limit=100`
- `GET /api/v1/probes/history?key=<probe>&limit=50`
- `POST /api/v1/action`
- `POST /api/v1/probes/run` with `{ "key": "sms_health" }`

All endpoints except `health` require:

- Header: `X-RC-Token: <RC_ADMIN_TOKEN>`

## Runtime storage

The backend stores runtime state in SQLite:

- `probe_definitions`
- `probe_runs`
- `action_audit`

Default DB path: `./data/health.sqlite3` (WAL mode).

## Comprehensive disk report

`GET /api/v1/status` now includes:

- `disk_report.filesystems` (space and inode usage per mount)
- `disk_report.watch_paths` (du-based size for selected paths)
- `disk_report.alerts` (threshold breaches)

Configure behavior in `config.json` under `targets.disk_report`:

- refresh/caching interval
- alert thresholds
- filesystem-type exclusions
- watched paths and path-scan limits

## Scheduled probes

Configure probes in `config.json` under `scheduled_probes`.

Supported probe types:

- `sms_health`
  - AfroMessage network reachability (TCP + HTTP)
  - Optional PostgreSQL indicators from `cis_messaging` tables using `RC_PG_DSN`
- `nid_health`
  - National ID gateway network and endpoint reachability
- `tcp_check`
- `http_check`

## Environment

- `RC_ADMIN_TOKEN` (required)
- `RC_CONFIG_PATH` (default: `./config.json`)
- `RC_DB_PATH` (default: `./data/health.sqlite3`)
- `RC_BIND_HOST` (default: `127.0.0.1`)
- `RC_BIND_PORT` (default: `8765`)
- `RC_MAX_BODY_BYTES` (default: `16384`)
- `RC_PROBE_TICK_SECONDS` (default: `2`)
- `RC_HELPER_SOCKET` (default: `/run/rc-control/helper.sock`)
- `RC_HELPER_SOCKET_GROUP` (default: `tewelde`)
- `RC_HELPER_TIMEOUT_SECONDS` (default: `15`)
- `RC_HELPER_MAX_BODY_BYTES` (default: `16384`)
- `RC_PG_DSN` (optional, for DB-backed SMS probe checks)
- `AFRO_SMS_BASE_URL` (optional)
- `NID_BASE_URL` (optional)

## Run

```bash
cd deploy/site-config/dire/remot-control/python-backend
export RC_ADMIN_TOKEN='change-this'
python3 privileged_helper.py &
python3 remote_control_server.py
```
