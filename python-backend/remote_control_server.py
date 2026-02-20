#!/usr/bin/env python3
import datetime as dt
import hmac
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as url_error
from urllib import request as url_request
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8765
DEFAULT_DB_PATH = BASE_DIR / "data" / "health.sqlite3"
MAX_AUDIT_LIMIT = 500


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def env_int(name: str, default_value: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default_value
    try:
        return int(raw)
    except ValueError:
        return default_value


def env_float(name: str, default_value: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default_value
    try:
        return float(raw)
    except ValueError:
        return default_value


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Config file must contain a JSON object.")
    return data


def run_cmd(command: list[str], timeout: int = 20) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "return_code": proc.returncode,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "command": command,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "return_code": -1,
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "command": command,
        }
    except Exception as ex:
        return {
            "ok": False,
            "return_code": -1,
            "stdout": "",
            "stderr": str(ex),
            "command": command,
        }


def mem_snapshot() -> dict[str, Any]:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return {"available": False}
    values: dict[str, int] = {}
    for line in meminfo.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        amount = value.strip().split(" ", 1)[0]
        try:
            values[key] = int(amount)
        except ValueError:
            continue
    total = values.get("MemTotal", 0)
    free = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(total - free, 0)
    used_pct = (used / total * 100.0) if total else 0.0
    return {
        "available": True,
        "total_kb": total,
        "free_kb": free,
        "used_kb": used,
        "used_pct": round(used_pct, 2),
    }


def uptime_seconds() -> int | None:
    p = Path("/proc/uptime")
    if not p.exists():
        return None
    try:
        return int(float(p.read_text(encoding="utf-8").split()[0]))
    except Exception:
        return None


def tcp_check(host: str, port: int, timeout: float = 1.5) -> dict[str, Any]:
    started = dt.datetime.now(dt.timezone.utc)
    ok = False
    error = ""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
    except Exception as ex:
        error = str(ex)
    elapsed_ms = (dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000.0
    return {
        "host": host,
        "port": port,
        "ok": ok,
        "latency_ms": round(elapsed_ms, 2),
        "error": error,
    }


def http_probe(
    url: str,
    timeout_seconds: float = 3.0,
    method: str = "GET",
    expected_status: list[int] | None = None,
    allow_4xx: bool = True,
) -> dict[str, Any]:
    started = dt.datetime.now(dt.timezone.utc)
    status = 0
    body = ""
    error_message = ""
    ok = False
    try:
        req = url_request.Request(url=url, method=method.upper())
        with url_request.urlopen(req, timeout=timeout_seconds) as resp:
            status = int(resp.status)
            body = resp.read(512).decode("utf-8", errors="replace")
    except url_error.HTTPError as ex:
        status = int(ex.code)
        body = ex.read(512).decode("utf-8", errors="replace")
    except Exception as ex:  # noqa: BLE001
        error_message = str(ex)

    if not error_message:
        if expected_status:
            ok = status in expected_status
        elif allow_4xx:
            ok = status < 500
        else:
            ok = 200 <= status < 400

    elapsed_ms = (dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000.0
    return {
        "url": url,
        "method": method.upper(),
        "status_code": status,
        "ok": ok,
        "error": error_message,
        "latency_ms": round(elapsed_ms, 2),
        "sample": body[:200],
    }


def parse_url_host_port(url: str) -> tuple[str, int]:
    p = urlparse(url)
    host = p.hostname or "127.0.0.1"
    if p.port:
        return host, int(p.port)
    return host, (443 if p.scheme == "https" else 80)


class SQLiteStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS probe_definitions (
                    probe_key TEXT PRIMARY KEY,
                    probe_type TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    timeout_seconds INTEGER NOT NULL,
                    stale_after_seconds INTEGER NOT NULL,
                    enabled INTEGER NOT NULL,
                    probe_config_json TEXT NOT NULL,
                    next_run_at TEXT,
                    last_run_at TEXT
                );

                CREATE TABLE IF NOT EXISTS probe_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    probe_key TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    error TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_probe_runs_probe_key_id
                    ON probe_runs(probe_key, id DESC);

                CREATE TABLE IF NOT EXISTS action_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    actor TEXT,
                    remote_ip TEXT,
                    target_type TEXT,
                    target TEXT,
                    action TEXT,
                    reason TEXT,
                    ok INTEGER NOT NULL,
                    return_code INTEGER,
                    stderr TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_action_audit_time
                    ON action_audit(timestamp_utc DESC);
                """
            )
            self.conn.commit()

    @staticmethod
    def _normalize_probe_definition(defn: dict[str, Any]) -> dict[str, Any]:
        key = str(defn.get("key", "")).strip()
        probe_type = str(defn.get("type", "")).strip()
        if not key:
            raise ValueError("Probe key is required.")
        if not probe_type:
            raise ValueError(f"Probe '{key}' is missing type.")

        interval_seconds = int(defn.get("interval_seconds", 60))
        timeout_seconds = int(defn.get("timeout_seconds", 5))
        stale_after_seconds = int(defn.get("stale_after_seconds", max(interval_seconds * 2, 120)))
        enabled = bool(defn.get("enabled", True))
        cfg = defn.get("config", {})
        if not isinstance(cfg, dict):
            cfg = {}

        return {
            "probe_key": key,
            "probe_type": probe_type,
            "interval_seconds": max(5, interval_seconds),
            "timeout_seconds": max(1, timeout_seconds),
            "stale_after_seconds": max(10, stale_after_seconds),
            "enabled": 1 if enabled else 0,
            "probe_config_json": json.dumps(cfg, ensure_ascii=True),
        }

    def sync_probe_definitions(self, definitions: list[dict[str, Any]]) -> None:
        now = now_utc()
        normed = [self._normalize_probe_definition(d) for d in definitions]
        desired_keys = {n["probe_key"] for n in normed}

        with self._lock:
            for n in normed:
                row = self.conn.execute(
                    "SELECT probe_key FROM probe_definitions WHERE probe_key = ?",
                    (n["probe_key"],),
                ).fetchone()
                if row is None:
                    self.conn.execute(
                        """
                        INSERT INTO probe_definitions (
                            probe_key, probe_type, interval_seconds, timeout_seconds,
                            stale_after_seconds, enabled, probe_config_json, next_run_at, last_run_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            n["probe_key"],
                            n["probe_type"],
                            n["interval_seconds"],
                            n["timeout_seconds"],
                            n["stale_after_seconds"],
                            n["enabled"],
                            n["probe_config_json"],
                            now,
                        ),
                    )
                else:
                    self.conn.execute(
                        """
                        UPDATE probe_definitions
                        SET probe_type = ?, interval_seconds = ?, timeout_seconds = ?,
                            stale_after_seconds = ?, enabled = ?, probe_config_json = ?
                        WHERE probe_key = ?
                        """,
                        (
                            n["probe_type"],
                            n["interval_seconds"],
                            n["timeout_seconds"],
                            n["stale_after_seconds"],
                            n["enabled"],
                            n["probe_config_json"],
                            n["probe_key"],
                        ),
                    )

            if desired_keys:
                placeholders = ",".join(["?"] * len(desired_keys))
                self.conn.execute(
                    f"UPDATE probe_definitions SET enabled = 0 WHERE probe_key NOT IN ({placeholders})",
                    tuple(sorted(desired_keys)),
                )
            self.conn.commit()

    def get_probe_definition(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM probe_definitions WHERE probe_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_probe(row)

    def list_due_probes(self, now_iso: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM probe_definitions
                WHERE enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?)
                ORDER BY COALESCE(next_run_at, '') ASC, probe_key ASC
                """,
                (now_iso,),
            ).fetchall()
        return [self._row_to_probe(r) for r in rows]

    def _row_to_probe(self, row: sqlite3.Row) -> dict[str, Any]:
        cfg: dict[str, Any] = {}
        try:
            parsed = json.loads(row["probe_config_json"] or "{}")
            if isinstance(parsed, dict):
                cfg = parsed
        except json.JSONDecodeError:
            cfg = {}
        return {
            "key": row["probe_key"],
            "type": row["probe_type"],
            "interval_seconds": int(row["interval_seconds"]),
            "timeout_seconds": int(row["timeout_seconds"]),
            "stale_after_seconds": int(row["stale_after_seconds"]),
            "enabled": bool(int(row["enabled"])),
            "next_run_at": row["next_run_at"],
            "last_run_at": row["last_run_at"],
            "config": cfg,
        }

    def set_probe_next_run(self, key: str, next_run_at: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE probe_definitions SET next_run_at = ? WHERE probe_key = ?",
                (next_run_at, key),
            )
            self.conn.commit()

    def save_probe_run(self, key: str, run: dict[str, Any], next_run_at: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO probe_runs (
                    probe_key, started_at, ended_at, ok, status, latency_ms, error, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    run["started_at"],
                    run["ended_at"],
                    1 if run["ok"] else 0,
                    run["status"],
                    float(run["latency_ms"]),
                    run.get("error", ""),
                    json.dumps(run.get("payload", {}), ensure_ascii=True),
                ),
            )
            self.conn.execute(
                """
                UPDATE probe_definitions
                SET last_run_at = ?, next_run_at = ?
                WHERE probe_key = ?
                """,
                (run["ended_at"], next_run_at, key),
            )
            self.conn.commit()

    def get_latest_probes(self, now_iso: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT
                    d.probe_key,
                    d.probe_type,
                    d.interval_seconds,
                    d.timeout_seconds,
                    d.stale_after_seconds,
                    d.enabled,
                    d.next_run_at,
                    d.last_run_at,
                    r.id AS run_id,
                    r.started_at,
                    r.ended_at,
                    r.ok,
                    r.status,
                    r.latency_ms,
                    r.error,
                    r.payload_json
                FROM probe_definitions d
                LEFT JOIN probe_runs r ON r.id = (
                    SELECT rr.id
                    FROM probe_runs rr
                    WHERE rr.probe_key = d.probe_key
                    ORDER BY rr.id DESC
                    LIMIT 1
                )
                ORDER BY d.probe_key
                """
            ).fetchall()

        out: list[dict[str, Any]] = []
        now_dt = parse_iso(now_iso) or dt.datetime.now(dt.timezone.utc)
        for row in rows:
            payload: dict[str, Any] = {}
            if row["payload_json"]:
                try:
                    parsed = json.loads(row["payload_json"])
                    if isinstance(parsed, dict):
                        payload = parsed
                except json.JSONDecodeError:
                    payload = {}
            ended = parse_iso(row["ended_at"]) if row["ended_at"] else None
            stale = True
            age_seconds = None
            if ended is not None:
                age_seconds = int((now_dt - ended).total_seconds())
                stale = age_seconds > int(row["stale_after_seconds"])
            out.append(
                {
                    "key": row["probe_key"],
                    "type": row["probe_type"],
                    "enabled": bool(int(row["enabled"])),
                    "interval_seconds": int(row["interval_seconds"]),
                    "stale_after_seconds": int(row["stale_after_seconds"]),
                    "next_run_at": row["next_run_at"],
                    "last_run_at": row["last_run_at"],
                    "latest_run": {
                        "run_id": row["run_id"],
                        "started_at": row["started_at"],
                        "ended_at": row["ended_at"],
                        "ok": bool(int(row["ok"])) if row["ok"] is not None else None,
                        "status": row["status"],
                        "latency_ms": row["latency_ms"],
                        "error": row["error"],
                        "payload": payload,
                    },
                    "age_seconds": age_seconds,
                    "is_stale": stale,
                }
            )
        return out

    def get_probe_history(self, key: str, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, probe_key, started_at, ended_at, ok, status, latency_ms, error, payload_json
                FROM probe_runs
                WHERE probe_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (key, limit),
            ).fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            payload: dict[str, Any] = {}
            if row["payload_json"]:
                try:
                    parsed = json.loads(row["payload_json"])
                    if isinstance(parsed, dict):
                        payload = parsed
                except json.JSONDecodeError:
                    payload = {}
            out.append(
                {
                    "id": row["id"],
                    "probe_key": row["probe_key"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "ok": bool(int(row["ok"])),
                    "status": row["status"],
                    "latency_ms": row["latency_ms"],
                    "error": row["error"],
                    "payload": payload,
                }
            )
        return out

    def add_action_audit(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO action_audit (
                    timestamp_utc, actor, remote_ip, target_type, target, action, reason, ok, return_code, stderr
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("timestamp_utc", now_utc()),
                    row.get("actor", "unknown"),
                    row.get("remote_ip", ""),
                    row.get("target_type", ""),
                    row.get("target", ""),
                    row.get("action", ""),
                    row.get("reason", ""),
                    1 if row.get("ok") else 0,
                    row.get("return_code"),
                    row.get("stderr", ""),
                ),
            )
            self.conn.commit()

    def read_action_audit(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT timestamp_utc, actor, remote_ip, target_type, target, action, reason, ok, return_code, stderr
                FROM action_audit
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "timestamp_utc": r["timestamp_utc"],
                "actor": r["actor"],
                "remote_ip": r["remote_ip"],
                "target_type": r["target_type"],
                "target": r["target"],
                "action": r["action"],
                "reason": r["reason"],
                "ok": bool(int(r["ok"])),
                "return_code": r["return_code"],
                "stderr": r["stderr"],
            }
            for r in rows
        ]


class ProbeRunner:
    def run_probe(self, probe: dict[str, Any]) -> dict[str, Any]:
        started = dt.datetime.now(dt.timezone.utc)
        timeout_seconds = int(probe.get("timeout_seconds", 5))
        probe_type = str(probe.get("type", ""))
        cfg = probe.get("config", {})
        if not isinstance(cfg, dict):
            cfg = {}
        result: dict[str, Any]
        try:
            if probe_type == "sms_health":
                result = self._probe_sms_health(cfg, timeout_seconds)
            elif probe_type == "nid_health":
                result = self._probe_nid_health(cfg, timeout_seconds)
            elif probe_type == "tcp_check":
                result = self._probe_tcp(cfg, timeout_seconds)
            elif probe_type == "http_check":
                result = self._probe_http(cfg, timeout_seconds)
            else:
                result = {
                    "ok": False,
                    "status": "error",
                    "error": f"Unsupported probe type: {probe_type}",
                    "payload": {"probe_type": probe_type},
                }
        except Exception as ex:  # noqa: BLE001
            result = {
                "ok": False,
                "status": "error",
                "error": str(ex),
                "payload": {"probe_type": probe_type},
            }
        ended = dt.datetime.now(dt.timezone.utc)
        latency_ms = (ended - started).total_seconds() * 1000.0
        return {
            "started_at": started.isoformat(),
            "ended_at": ended.isoformat(),
            "latency_ms": round(latency_ms, 2),
            "ok": bool(result.get("ok", False)),
            "status": str(result.get("status", "unknown")),
            "error": str(result.get("error", "")),
            "payload": result.get("payload", {}),
        }

    @staticmethod
    def _step_ok(step: dict[str, Any]) -> bool:
        if step.get("skipped"):
            return True
        return bool(step.get("ok"))

    def _probe_tcp(self, cfg: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        host = str(cfg.get("host", "127.0.0.1"))
        port = int(cfg.get("port", 0))
        if port <= 0:
            return {
                "ok": False,
                "status": "error",
                "error": "tcp_check requires positive port",
                "payload": {"host": host, "port": port},
            }
        check = tcp_check(host, port, timeout=float(min(timeout_seconds, 10)))
        return {
            "ok": check["ok"],
            "status": "healthy" if check["ok"] else "degraded",
            "error": check["error"],
            "payload": check,
        }

    def _probe_http(self, cfg: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        url = str(cfg.get("url", "")).strip()
        if not url:
            return {
                "ok": False,
                "status": "error",
                "error": "http_check requires url",
                "payload": {},
            }
        method = str(cfg.get("method", "GET")).upper()
        allow_4xx = bool(cfg.get("allow_4xx", True))
        expected_status_raw = cfg.get("expected_status", [])
        expected_status = []
        if isinstance(expected_status_raw, list):
            for x in expected_status_raw:
                try:
                    expected_status.append(int(x))
                except Exception:  # noqa: BLE001
                    continue
        check = http_probe(
            url=url,
            timeout_seconds=float(min(timeout_seconds, 20)),
            method=method,
            expected_status=expected_status or None,
            allow_4xx=allow_4xx,
        )
        return {
            "ok": check["ok"],
            "status": "healthy" if check["ok"] else "degraded",
            "error": check["error"],
            "payload": check,
        }

    def _psql_scalar(self, dsn: str, query: str, timeout_seconds: int) -> tuple[bool, int | None, str]:
        cmd = ["psql", dsn, "-At", "-v", "ON_ERROR_STOP=1", "-c", query]
        out = run_cmd(cmd, timeout=max(5, timeout_seconds))
        if not out["ok"]:
            return False, None, out["stderr"] or out["stdout"]
        first = (out["stdout"].splitlines() or [""])[0].strip()
        if first == "":
            return False, None, "No scalar result"
        try:
            return True, int(first), ""
        except ValueError:
            return False, None, f"Non-integer scalar result: {first}"

    def _probe_sms_health(self, cfg: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []

        base_url = (
            str(cfg.get("afro_base_url", "")).strip()
            or os.environ.get(str(cfg.get("afro_base_url_env", "AFRO_SMS_BASE_URL")).strip(), "").strip()
            or "https://api.afromessage.com/api"
        )
        host, port = parse_url_host_port(base_url)
        tcp = tcp_check(host, port, timeout=min(float(timeout_seconds), 5.0))
        steps.append({"name": "provider_tcp", "required": True, **tcp})

        http = http_probe(
            url=base_url,
            timeout_seconds=min(float(timeout_seconds), 8.0),
            method="GET",
            allow_4xx=True,
        )
        steps.append(
            {
                "name": "provider_http",
                "required": True,
                "ok": bool(http["ok"]),
                "status_code": http["status_code"],
                "latency_ms": http["latency_ms"],
                "error": http["error"],
            }
        )

        dsn = (
            str(cfg.get("pg_dsn", "")).strip()
            or os.environ.get(str(cfg.get("pg_dsn_env", "RC_PG_DSN")).strip(), "").strip()
        )
        if dsn:
            outbox_limit = int(cfg.get("max_outbox", 200))
            failed_limit = int(cfg.get("max_failed_recent", 20))
            failed_window_rows = int(cfg.get("failed_recent_rows", 200))
            outbox_query = str(
                cfg.get(
                    "outbox_count_query",
                    "SELECT COUNT(*) FROM cis_messaging.cis_sms WHERE status='Outbox';",
                )
            )
            failed_query = str(
                cfg.get(
                    "failed_recent_query",
                    f"""
                    SELECT COALESCE(SUM(CASE WHEN q.success = false THEN 1 ELSE 0 END), 0)
                    FROM (
                      SELECT r.success
                      FROM cis_messaging.cis_sms_result r
                      JOIN cis_messaging.cis_sms s ON s.id = r.sms_id
                      ORDER BY s.create_time DESC
                      LIMIT {failed_window_rows}
                    ) q;
                    """,
                )
            )
            ok_outbox, outbox_count, outbox_error = self._psql_scalar(dsn, outbox_query, timeout_seconds)
            steps.append(
                {
                    "name": "db_outbox_backlog",
                    "required": True,
                    "ok": bool(ok_outbox and outbox_count is not None and outbox_count <= outbox_limit),
                    "value": outbox_count,
                    "threshold": outbox_limit,
                    "error": outbox_error,
                }
            )
            ok_failed, failed_count, failed_error = self._psql_scalar(dsn, failed_query, timeout_seconds)
            steps.append(
                {
                    "name": "db_failed_recent",
                    "required": True,
                    "ok": bool(ok_failed and failed_count is not None and failed_count <= failed_limit),
                    "value": failed_count,
                    "threshold": failed_limit,
                    "error": failed_error,
                }
            )
        else:
            steps.append(
                {
                    "name": "db_checks",
                    "required": False,
                    "ok": True,
                    "skipped": True,
                    "error": "pg_dsn not provided",
                }
            )

        failed = [s for s in steps if not self._step_ok(s)]
        ok = len(failed) == 0
        return {
            "ok": ok,
            "status": "healthy" if ok else "degraded",
            "error": "; ".join(str(s.get("name")) for s in failed),
            "payload": {
                "probe": "sms_health",
                "afro_base_url": base_url,
                "steps": steps,
            },
        }

    def _probe_nid_health(self, cfg: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []

        base_url = (
            str(cfg.get("base_url", "")).strip()
            or os.environ.get(str(cfg.get("base_url_env", "NID_BASE_URL")).strip(), "").strip()
            or "http://196.188.240.67/gateway"
        )
        request_data_url = str(cfg.get("request_data_url", "")).strip() or f"{base_url}/nid/requestData"
        get_data_url = str(cfg.get("get_data_url", "")).strip() or f"{base_url}/nid/getData"

        host, port = parse_url_host_port(base_url)
        tcp = tcp_check(host, port, timeout=min(float(timeout_seconds), 5.0))
        steps.append({"name": "gateway_tcp", "required": True, **tcp})

        base_http = http_probe(
            url=base_url,
            timeout_seconds=min(float(timeout_seconds), 8.0),
            method="GET",
            allow_4xx=True,
        )
        steps.append(
            {
                "name": "gateway_http_base",
                "required": True,
                "ok": bool(base_http["ok"]),
                "status_code": base_http["status_code"],
                "latency_ms": base_http["latency_ms"],
                "error": base_http["error"],
            }
        )

        req_http = http_probe(
            url=request_data_url,
            timeout_seconds=min(float(timeout_seconds), 8.0),
            method="GET",
            allow_4xx=True,
        )
        steps.append(
            {
                "name": "gateway_http_requestData_endpoint",
                "required": True,
                "ok": bool(req_http["ok"]),
                "status_code": req_http["status_code"],
                "latency_ms": req_http["latency_ms"],
                "error": req_http["error"],
            }
        )

        get_http = http_probe(
            url=get_data_url,
            timeout_seconds=min(float(timeout_seconds), 8.0),
            method="GET",
            allow_4xx=True,
        )
        steps.append(
            {
                "name": "gateway_http_getData_endpoint",
                "required": True,
                "ok": bool(get_http["ok"]),
                "status_code": get_http["status_code"],
                "latency_ms": get_http["latency_ms"],
                "error": get_http["error"],
            }
        )

        failed = [s for s in steps if not self._step_ok(s)]
        ok = len(failed) == 0
        return {
            "ok": ok,
            "status": "healthy" if ok else "degraded",
            "error": "; ".join(str(s.get("name")) for s in failed),
            "payload": {
                "probe": "nid_health",
                "base_url": base_url,
                "request_data_url": request_data_url,
                "get_data_url": get_data_url,
                "steps": steps,
            },
        }


class RemoteControlApi:
    def __init__(self):
        config_path = Path(os.environ.get("RC_CONFIG_PATH", str(DEFAULT_CONFIG_PATH))).resolve()
        self.config_path = config_path
        self.config = read_json_file(config_path)
        self.admin_token = os.environ.get("RC_ADMIN_TOKEN", "").strip()
        self.db_path = Path(os.environ.get("RC_DB_PATH", str(DEFAULT_DB_PATH))).resolve()
        self.store = SQLiteStore(self.db_path)
        self.probe_runner = ProbeRunner()
        self.store.sync_probe_definitions(self._configured_scheduled_probes())

    def require_token(self, provided: str) -> bool:
        if not self.admin_token:
            return False
        return hmac.compare_digest(provided, self.admin_token)

    def _configured_services(self) -> list[str]:
        return list(self.config.get("targets", {}).get("services", []))

    def _configured_containers(self) -> list[str]:
        return list(self.config.get("targets", {}).get("containers", []))

    def _configured_tcp_checks(self) -> list[dict[str, Any]]:
        checks = self.config.get("targets", {}).get("tcp_checks", [])
        if isinstance(checks, list):
            return [c for c in checks if isinstance(c, dict)]
        return []

    def _configured_scheduled_probes(self) -> list[dict[str, Any]]:
        items = self.config.get("scheduled_probes", [])
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
        return []

    def _service_status(self, service_name: str) -> dict[str, Any]:
        out = run_cmd(
            [
                "systemctl",
                "show",
                service_name,
                "--property=ActiveState,SubState,UnitFileState",
                "--value",
            ]
        )
        if not out["ok"]:
            return {
                "name": service_name,
                "status": "unknown",
                "sub_status": "",
                "enabled": "",
                "error": out["stderr"] or out["stdout"],
            }
        lines = out["stdout"].splitlines()
        active = lines[0] if len(lines) > 0 else "unknown"
        sub = lines[1] if len(lines) > 1 else ""
        enabled = lines[2] if len(lines) > 2 else ""
        return {
            "name": service_name,
            "status": active,
            "sub_status": sub,
            "enabled": enabled,
            "error": "",
        }

    def _container_status_map(self) -> tuple[dict[str, dict[str, str]], str]:
        out = run_cmd(
            [
                "sudo",
                "-n",
                "docker",
                "ps",
                "-a",
                "--format",
                "{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}",
            ]
        )
        if not out["ok"]:
            return {}, out["stderr"] or out["stdout"]
        result: dict[str, dict[str, str]] = {}
        for line in out["stdout"].splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            result[parts[0]] = {
                "status": parts[1],
                "image": parts[2],
                "ports": parts[3],
            }
        return result, ""

    def collect_status(self) -> dict[str, Any]:
        uptime = uptime_seconds()
        disk = shutil.disk_usage("/")
        container_map, container_error = self._container_status_map()
        now_iso = now_utc()

        payload: dict[str, Any] = {
            "timestamp_utc": now_iso,
            "host": socket.gethostname(),
            "uptime_seconds": uptime,
            "load_avg": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            "memory": mem_snapshot(),
            "disk_root": {
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
                "used_pct": round((disk.used / disk.total * 100.0), 2) if disk.total else 0.0,
            },
            "sqlite_db_path": str(self.db_path),
            "targets": {
                "services": [],
                "containers": [],
                "tcp_checks": [],
            },
            "scheduled_probes": self.store.get_latest_probes(now_iso),
        }

        for name in self._configured_services():
            payload["targets"]["services"].append(self._service_status(name))

        for name in self._configured_containers():
            item = container_map.get(name)
            if item:
                payload["targets"]["containers"].append(
                    {
                        "name": name,
                        "status": item["status"],
                        "image": item["image"],
                        "ports": item["ports"],
                        "error": "",
                    }
                )
            else:
                payload["targets"]["containers"].append(
                    {
                        "name": name,
                        "status": "not_found",
                        "image": "",
                        "ports": "",
                        "error": container_error if container_error else "Container not found",
                    }
                )

        for check in self._configured_tcp_checks():
            host = str(check.get("host", "127.0.0.1"))
            port = int(check.get("port", 0))
            if port <= 0:
                continue
            result = tcp_check(host, port, float(check.get("timeout_seconds", 1.5)))
            result["name"] = str(check.get("name", f"{host}:{port}"))
            payload["targets"]["tcp_checks"].append(result)
        return payload

    def _allowed_actions(self, target_type: str) -> list[str]:
        return list(self.config.get("actions", {}).get(target_type, []))

    def execute_action(self, actor: str, remote_ip: str, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        target_type = str(req.get("target_type", "")).strip()
        action = str(req.get("action", "")).strip()
        target = str(req.get("target", "")).strip()
        reason = str(req.get("reason", "")).strip()

        if target_type not in ("service", "container"):
            return 400, {"ok": False, "error": "Invalid target_type. Use service|container."}
        if not target:
            return 400, {"ok": False, "error": "Target is required."}
        if action not in self._allowed_actions(target_type):
            return 403, {"ok": False, "error": f"Action '{action}' is not allowed for {target_type}."}

        if target_type == "service":
            allowed = set(self._configured_services())
            if target not in allowed:
                return 403, {"ok": False, "error": f"Service '{target}' is not in allowlist."}
            command = ["sudo", "-n", "systemctl", action, target]
        else:
            allowed = set(self._configured_containers())
            if target not in allowed:
                return 403, {"ok": False, "error": f"Container '{target}' is not in allowlist."}
            command = ["sudo", "-n", "docker", action, target]

        result = run_cmd(command, timeout=45)
        response = {
            "ok": result["ok"],
            "target_type": target_type,
            "target": target,
            "action": action,
            "reason": reason,
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "return_code": result["return_code"],
            "timestamp_utc": now_utc(),
        }
        self.store.add_action_audit(
            {
                "timestamp_utc": response["timestamp_utc"],
                "actor": actor,
                "remote_ip": remote_ip,
                "target_type": target_type,
                "target": target,
                "action": action,
                "reason": reason,
                "ok": result["ok"],
                "return_code": result["return_code"],
                "stderr": result["stderr"],
            }
        )
        return (200 if result["ok"] else 500), response

    def read_audit(self, limit: int) -> list[dict[str, Any]]:
        return self.store.read_action_audit(limit)

    def run_probe_once(self, key: str) -> tuple[int, dict[str, Any]]:
        probe = self.store.get_probe_definition(key)
        if not probe:
            return 404, {"ok": False, "error": f"Probe '{key}' not found."}
        run = self.probe_runner.run_probe(probe)
        next_time = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=probe["interval_seconds"])).isoformat()
        self.store.save_probe_run(key, run, next_time)
        return 200, {"ok": True, "probe_key": key, "run": run}


class ProbeScheduler(threading.Thread):
    def __init__(self, api: RemoteControlApi):
        super().__init__(daemon=True, name="rc-probe-scheduler")
        self.api = api
        self.stop_event = threading.Event()
        self.tick_seconds = max(1.0, env_float("RC_PROBE_TICK_SECONDS", 2.0))

    def run(self) -> None:
        while not self.stop_event.is_set():
            now_iso = now_utc()
            due = self.api.store.list_due_probes(now_iso)
            for probe in due:
                key = str(probe["key"])
                interval_seconds = int(probe.get("interval_seconds", 60))
                tentative_next = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=interval_seconds)).isoformat()
                self.api.store.set_probe_next_run(key, tentative_next)
                run = self.api.probe_runner.run_probe(probe)
                self.api.store.save_probe_run(key, run, tentative_next)
            self.stop_event.wait(self.tick_seconds)

    def stop(self) -> None:
        self.stop_event.set()


API = RemoteControlApi()
SCHEDULER = ProbeScheduler(API)


class Handler(BaseHTTPRequestHandler):
    server_version = "DireRemoteControl/0.2"

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _token_ok(self) -> bool:
        if self.path.startswith("/api/v1/health"):
            return True
        token = self.headers.get("X-RC-Token", "").strip()
        return API.require_token(token)

    def _forbidden(self) -> None:
        self._send(401, {"ok": False, "error": "Unauthorized"})

    def do_GET(self):  # noqa: N802
        if not self._token_ok():
            self._forbidden()
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query or "")

        if path == "/api/v1/health":
            self._send(200, {"ok": True, "timestamp_utc": now_utc()})
            return

        if path == "/api/v1/status":
            try:
                self._send(200, {"ok": True, "data": API.collect_status()})
            except Exception as ex:  # noqa: BLE001
                self._send(500, {"ok": False, "error": str(ex)})
            return

        if path == "/api/v1/audit":
            raw_limit = query.get("limit", ["100"])[0]
            try:
                limit = max(1, min(MAX_AUDIT_LIMIT, int(raw_limit)))
            except ValueError:
                limit = 100
            self._send(200, {"ok": True, "rows": API.read_audit(limit)})
            return

        if path == "/api/v1/probes/history":
            key = query.get("key", [""])[0].strip()
            if not key:
                self._send(400, {"ok": False, "error": "Query parameter 'key' is required."})
                return
            raw_limit = query.get("limit", ["50"])[0]
            try:
                limit = max(1, min(500, int(raw_limit)))
            except ValueError:
                limit = 50
            rows = API.store.get_probe_history(key, limit)
            self._send(200, {"ok": True, "probe_key": key, "rows": rows})
            return

        if path == "/api/v1/config":
            safe_config = {
                "targets": API.config.get("targets", {}),
                "actions": API.config.get("actions", {}),
                "scheduled_probes": API.config.get("scheduled_probes", []),
            }
            self._send(200, {"ok": True, "config": safe_config})
            return

        self._send(404, {"ok": False, "error": "Not found"})

    def do_POST(self):  # noqa: N802
        if not self._token_ok():
            self._forbidden()
            return

        parsed = urlparse(self.path)
        if parsed.path not in ("/api/v1/action", "/api/v1/probes/run"):
            self._send(404, {"ok": False, "error": "Not found"})
            return

        body_len = env_int("RC_MAX_BODY_BYTES", 16384)
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            self._send(400, {"ok": False, "error": "Invalid content-length"})
            return
        if length <= 0 or length > body_len:
            self._send(400, {"ok": False, "error": "Invalid request body size"})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"ok": False, "error": "Invalid JSON payload"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"ok": False, "error": "Payload must be an object"})
            return

        actor = self.headers.get("X-RC-Actor", "").strip() or "unknown"
        ip = self.client_address[0]
        if parsed.path == "/api/v1/action":
            status, response = API.execute_action(actor=actor, remote_ip=ip, req=payload)
            self._send(status, response)
            return
        key = str(payload.get("key", "")).strip()
        if not key:
            self._send(400, {"ok": False, "error": "Probe key is required."})
            return
        status, response = API.run_probe_once(key)
        self._send(status, response)

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    host = os.environ.get("RC_BIND_HOST", DEFAULT_BIND_HOST).strip() or DEFAULT_BIND_HOST
    port = env_int("RC_BIND_PORT", DEFAULT_BIND_PORT)
    SCHEDULER.start()
    srv = ThreadingHTTPServer((host, port), Handler)
    print(
        f"[rc-control] listening on {host}:{port} config={API.config_path} sqlite={API.db_path}",
        flush=True,
    )
    try:
        srv.serve_forever()
    finally:
        SCHEDULER.stop()
        SCHEDULER.join(timeout=5)


if __name__ == "__main__":
    main()
