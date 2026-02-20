#!/usr/bin/env python3
import grp
import json
import os
import socketserver
import subprocess
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_SOCKET_PATH = Path("/run/rc-control/helper.sock")
DEFAULT_SOCKET_GROUP = "tewelde"
DEFAULT_MAX_BODY_BYTES = 16384


def env_int(name: str, default_value: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default_value
    try:
        return int(raw)
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
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "return_code": -1,
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
        }
    except Exception as ex:  # noqa: BLE001
        return {
            "ok": False,
            "return_code": -1,
            "stdout": "",
            "stderr": str(ex),
        }


class PrivilegedApi:
    def __init__(self):
        config_path = Path(os.environ.get("RC_CONFIG_PATH", str(DEFAULT_CONFIG_PATH))).resolve()
        self.config_path = config_path
        self.config = read_json_file(config_path)

    def _configured_services(self) -> set[str]:
        raw = self.config.get("targets", {}).get("services", [])
        if not isinstance(raw, list):
            return set()
        return {str(x).strip() for x in raw if str(x).strip()}

    def _configured_containers(self) -> set[str]:
        raw = self.config.get("targets", {}).get("containers", [])
        if not isinstance(raw, list):
            return set()
        return {str(x).strip() for x in raw if str(x).strip()}

    def _allowed_actions(self, target_type: str) -> set[str]:
        raw = self.config.get("actions", {}).get(target_type, [])
        if not isinstance(raw, list):
            return set()
        return {str(x).strip() for x in raw if str(x).strip()}

    def _container_status_map(self) -> dict[str, Any]:
        out = run_cmd(
            [
                "docker",
                "ps",
                "-a",
                "--format",
                "{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}",
            ]
        )
        if not out["ok"]:
            return out
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
        out["containers"] = result
        return out

    def execute(self, req: dict[str, Any]) -> dict[str, Any]:
        op = str(req.get("op", "")).strip()
        action = str(req.get("action", "")).strip()
        target = str(req.get("target", "")).strip()

        if op == "container_status_map":
            return self._container_status_map()

        if op not in {"service_action", "container_action"}:
            return {"ok": False, "return_code": -1, "stdout": "", "stderr": "Unsupported operation."}
        if not target:
            return {"ok": False, "return_code": -1, "stdout": "", "stderr": "Target is required."}

        if op == "service_action":
            if action not in self._allowed_actions("service"):
                return {
                    "ok": False,
                    "return_code": -1,
                    "stdout": "",
                    "stderr": f"Action '{action}' is not allowed for service.",
                }
            if target not in self._configured_services():
                return {
                    "ok": False,
                    "return_code": -1,
                    "stdout": "",
                    "stderr": f"Service '{target}' is not in allowlist.",
                }
            return run_cmd(["systemctl", action, target], timeout=45)

        if action not in self._allowed_actions("container"):
            return {
                "ok": False,
                "return_code": -1,
                "stdout": "",
                "stderr": f"Action '{action}' is not allowed for container.",
            }
        if target not in self._configured_containers():
            return {
                "ok": False,
                "return_code": -1,
                "stdout": "",
                "stderr": f"Container '{target}' is not in allowlist.",
            }
        return run_cmd(["docker", action, target], timeout=45)


class ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Handler(socketserver.StreamRequestHandler):
    def _send(self, payload: dict[str, Any]) -> None:
        blob = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.wfile.write(blob)

    def handle(self) -> None:
        max_body = max(1024, env_int("RC_HELPER_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES))
        raw = self.rfile.readline(max_body + 2)
        if not raw:
            return
        if len(raw) > (max_body + 1):
            self._send({"ok": False, "return_code": -1, "stdout": "", "stderr": "Request body too large."})
            return
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send({"ok": False, "return_code": -1, "stdout": "", "stderr": "Invalid JSON payload."})
            return
        if not isinstance(payload, dict):
            self._send({"ok": False, "return_code": -1, "stdout": "", "stderr": "Payload must be an object."})
            return
        self._send(API.execute(payload))


def apply_socket_permissions(socket_path: Path) -> None:
    group_name = os.environ.get("RC_HELPER_SOCKET_GROUP", DEFAULT_SOCKET_GROUP).strip() or DEFAULT_SOCKET_GROUP
    gid = None
    if group_name:
        try:
            gid = grp.getgrnam(group_name).gr_gid
        except KeyError:
            print(f"[rc-helper] warning: group '{group_name}' not found; socket group ownership unchanged", flush=True)
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if gid is not None and is_root:
        os.chown(socket_path.parent, 0, gid)
    elif gid is not None and not is_root:
        print("[rc-helper] warning: not running as root; cannot set socket group ownership", flush=True)
    try:
        os.chmod(socket_path.parent, 0o750)
    except PermissionError:
        pass
    if gid is not None and is_root:
        os.chown(socket_path, 0, gid)
    try:
        os.chmod(socket_path, 0o660)
    except PermissionError:
        pass


def main() -> None:
    socket_path = Path(os.environ.get("RC_HELPER_SOCKET", str(DEFAULT_SOCKET_PATH))).resolve()
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        if socket_path.is_socket():
            socket_path.unlink()
        else:
            raise RuntimeError(f"Socket path exists and is not a socket: {socket_path}")

    srv = ThreadedUnixServer(str(socket_path), Handler)
    apply_socket_permissions(socket_path)
    print(f"[rc-helper] listening on unix://{socket_path} config={API.config_path}", flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        try:
            if socket_path.exists() and socket_path.is_socket():
                socket_path.unlink()
        except Exception:
            pass


API = PrivilegedApi()


if __name__ == "__main__":
    main()
