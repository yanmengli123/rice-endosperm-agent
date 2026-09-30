"""Expose the Windows BrowserSkill CLI to local Yuxi Docker containers.

This development-only bridge is authenticated with a generated shared secret.
It binds to a host TCP port because the BrowserSkill daemon itself uses a
Windows named pipe that Linux containers cannot access.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class BridgeError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class BrowserSkillRuntime:
    def __init__(self, bsk_path: Path, *, session_ttl_s: int = 900):
        self.bsk_path = bsk_path
        self._sessions: dict[str, str] = {}
        self._session_last_used: dict[str, float] = {}
        self._run_locks: dict[str, threading.RLock] = {}
        self._lock = threading.RLock()
        self._session_ttl_s = max(60, session_ttl_s)
        threading.Thread(
            target=self._reap_loop, name="bsk-session-reaper", daemon=True
        ).start()

    def health(self) -> dict[str, Any]:
        status = self._as_dict(self._run(["status", "--json"], timeout=15))
        browsers = (
            status.get("browsers") if isinstance(status.get("browsers"), list) else []
        )
        return {
            "ok": True,
            "daemon_version": status.get("daemon_version"),
            "protocol_version": status.get("protocol_version"),
            "browser_count": len(browsers),
            "active_bridge_sessions": len(self._sessions),
            "browsers": [
                {
                    "instance_id": item.get("instance_id"),
                    "browser_name": item.get("browser_name"),
                    "browser_version": item.get("browser_version"),
                    "extension_version": item.get("extension_version"),
                    "label": item.get("label"),
                    "connected_at_ms": item.get("connected_at_ms"),
                    "unresponsive": bool(item.get("unresponsive")),
                }
                for item in browsers
                if isinstance(item, dict)
            ],
        }

    def dispatch(
        self, run_key: str, op: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        run_lock = self._get_run_lock(run_key)
        try:
            with run_lock:
                return self._dispatch_locked(run_key, op, payload)
        finally:
            if op == "end_task":
                with self._lock:
                    if (
                        run_key not in self._sessions
                        and self._run_locks.get(run_key) is run_lock
                    ):
                        self._run_locks.pop(run_key, None)

    def _dispatch_locked(
        self, run_key: str, op: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if op == "end_task":
            return self._end_session(run_key)
        session_id = self._ensure_session(run_key)
        with self._lock:
            self._session_last_used[run_key] = time.monotonic()
        tab_id = self._tab_id(payload)
        common = ["--session", session_id, "--json"]
        if tab_id is not None:
            common.extend(["--tab-id", str(tab_id)])

        if op == "navigate":
            url = str(payload.get("url") or "")
            if payload.get("new_tab") is True and tab_id is None:
                result = self._run(
                    ["tab", "create", "--session", session_id, "--url", url, "--json"]
                )
            else:
                result = self._run(["navigate", url, *common, "--wait-until", "load"])
            return self._as_dict(result)
        if op == "read_page":
            data = self._as_dict(
                self._run(["observe", *common, "--max-tokens", "8000"])
            )
            return {
                "tab_id": data.get("tab_id"),
                "text": str(data.get("text") or ""),
                "truncated": bool(data.get("truncated")),
            }
        if op == "click":
            return self._as_dict(self._run(["click", self._selector(payload), *common]))
        if op == "type":
            selector = self._selector(payload)
            args = [
                "fill",
                selector,
                "--value",
                str(payload.get("text") or ""),
                *common,
            ]
            if payload.get("clear") is False:
                args.append("--no-clear")
            result = self._as_dict(self._run(args))
            if payload.get("submit") is True:
                focus_arg = (
                    ["--ref", selector]
                    if selector.startswith("@")
                    else ["--selector", selector]
                )
                self._run(["press", "Enter", *focus_arg, *common])
            return result
        if op == "screenshot":
            return self._screenshot(common)
        if op == "get_status":
            result = self._as_dict(
                self._run(
                    ["tab", "list", "--session", session_id, "--scope", "all", "--json"]
                )
            )
            tabs = result.get("tabs") if isinstance(result.get("tabs"), list) else []
            return {
                "session_id": session_id,
                "tabs": [
                    item
                    for item in tabs
                    if isinstance(item, dict) and item.get("scope") == "agent"
                ],
                "user_tabs": [
                    item
                    for item in tabs
                    if isinstance(item, dict) and item.get("scope") == "user"
                ],
            }
        raise BridgeError(
            "BROWSER_INVALID_OP",
            f"Unsupported BrowserSkill operation: {op}",
            status=400,
        )

    def _ensure_session(self, run_key: str) -> str:
        with self._lock:
            existing = self._sessions.get(run_key)
            if existing:
                return existing
            name = f"Yuxi {run_key[:32]}"
            result = self._as_dict(
                self._run(["session", "start", "--no-focus", "--name", name, "--json"])
            )
            session_id = str(result.get("session_id") or "")
            if not session_id:
                raise BridgeError(
                    "BROWSER_SESSION_FAILED", "BrowserSkill did not return a session_id"
                )
            self._sessions[run_key] = session_id
            self._session_last_used[run_key] = time.monotonic()
            return session_id

    def _end_session(self, run_key: str) -> dict[str, Any]:
        with self._lock:
            session_id = self._sessions.pop(run_key, None)
            self._session_last_used.pop(run_key, None)
        if not session_id:
            return {"ok": True, "already_ended": True}
        self._run(["session", "stop", session_id, "--json"])
        return {"ok": True, "session_id": session_id}

    def _get_run_lock(self, run_key: str) -> threading.RLock:
        with self._lock:
            return self._run_locks.setdefault(run_key, threading.RLock())

    def _reap_loop(self) -> None:
        interval = min(60, max(10, self._session_ttl_s // 4))
        while True:
            time.sleep(interval)
            threshold = time.monotonic() - self._session_ttl_s
            with self._lock:
                stale = [
                    run_key
                    for run_key, last_used in self._session_last_used.items()
                    if last_used < threshold
                ]
            for run_key in stale:
                try:
                    self.dispatch(run_key, "end_task", {})
                except BridgeError:
                    # The next sweep retries; a daemon outage must not kill the bridge.
                    pass

    def _screenshot(self, common: list[str]) -> dict[str, Any]:
        fd, raw_path = tempfile.mkstemp(prefix="yuxi-bsk-", suffix=".png")
        os.close(fd)
        path = Path(raw_path)
        path.unlink(missing_ok=True)
        try:
            result = self._as_dict(
                self._run(["screenshot", *common, "--out", str(path)], timeout=60)
            )
            payload = path.read_bytes()
            return {
                **result,
                "data_url": "data:image/png;base64,"
                + base64.b64encode(payload).decode("ascii"),
            }
        finally:
            path.unlink(missing_ok=True)

    def _run(self, args: list[str], *, timeout: float = 35) -> Any:
        env = {**os.environ, "BSK_AUTO_START": "0"}
        try:
            completed = subprocess.run(
                [str(self.bsk_path), *args],
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise BridgeError(
                "BROWSER_TIMEOUT", f"BrowserSkill command timed out: {args[0]}"
            ) from error
        if completed.returncode != 0:
            message = (
                completed.stderr or completed.stdout or "BrowserSkill command failed"
            ).strip()
            raise BridgeError("BROWSER_SKILL_ERROR", message[:500])
        output = completed.stdout.strip()
        if not output:
            return {}
        try:
            return json.loads(output)
        except json.JSONDecodeError as error:
            raise BridgeError(
                "BROWSER_SKILL_PROTOCOL_ERROR", "bsk returned invalid JSON"
            ) from error

    @staticmethod
    def _selector(payload: dict[str, Any]) -> str:
        selector = str(payload.get("selector") or "").strip()
        if not selector:
            raise BridgeError("EXT_BAD_REQUEST", "selector is required", status=400)
        return selector

    @staticmethod
    def _tab_id(payload: dict[str, Any]) -> int | None:
        value = payload.get("tab_id")
        if value is None:
            return None
        try:
            tab_id = int(value)
        except (TypeError, ValueError) as error:
            raise BridgeError(
                "EXT_BAD_REQUEST", "invalid tab_id", status=400
            ) from error
        if tab_id <= 0:
            raise BridgeError("EXT_BAD_REQUEST", "invalid tab_id", status=400)
        return tab_id

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}


class BridgeHandler(BaseHTTPRequestHandler):
    server: BrowserSkillHTTPServer

    def do_GET(self) -> None:
        if self.path != "/health":
            self._write(404, {"ok": False, "error": {"code": "NOT_FOUND"}})
            return
        if not self._authorized():
            self._write(401, {"ok": False, "error": {"code": "UNAUTHORIZED"}})
            return
        try:
            self._write(200, self.server.runtime.health())
        except BridgeError as error:
            self._bridge_error(error)

    def do_POST(self) -> None:
        if self.path != "/dispatch":
            self._write(404, {"ok": False, "error": {"code": "NOT_FOUND"}})
            return
        if not self._authorized():
            self._write(401, {"ok": False, "error": {"code": "UNAUTHORIZED"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 5 * 1024 * 1024:
                raise BridgeError("BAD_REQUEST", "invalid request size", status=400)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise BridgeError(
                    "BAD_REQUEST", "request body must be an object", status=400
                )
            run_key = str(body.get("run_id") or body.get("uid") or "default")
            op = str(body.get("op") or "")
            payload = (
                body.get("payload") if isinstance(body.get("payload"), dict) else {}
            )
            result = self.server.runtime.dispatch(run_key, op, payload)
            self._write(200, {"ok": True, "result": result})
        except (json.JSONDecodeError, TypeError, ValueError):
            self._write(400, {"ok": False, "error": {"code": "BAD_REQUEST"}})
        except BridgeError as error:
            self._bridge_error(error)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _authorized(self) -> bool:
        provided = self.headers.get("X-Yuxi-Browser-Secret") or ""
        return bool(provided) and hmac.compare_digest(
            provided, self.server.shared_secret
        )

    def _bridge_error(self, error: BridgeError) -> None:
        self._write(
            error.status,
            {"ok": False, "error": {"code": error.code, "message": error.message}},
        )

    def _write(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class BrowserSkillHTTPServer(ThreadingHTTPServer):
    def __init__(
        self, address: tuple[str, int], runtime: BrowserSkillRuntime, shared_secret: str
    ):
        super().__init__(address, BridgeHandler)
        self.runtime = runtime
        self.shared_secret = shared_secret


def ensure_secret(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if len(value) < 32:
            raise RuntimeError(f"Shared-secret file is too short: {path}")
        return value
    value = "ybs_" + secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=52801)
    parser.add_argument("--bsk", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    parser.add_argument("--session-ttl-s", type=int, default=900)
    args = parser.parse_args()

    secret = ensure_secret(args.secret_file.resolve())
    runtime = BrowserSkillRuntime(args.bsk.resolve(), session_ttl_s=args.session_ttl_s)
    server = BrowserSkillHTTPServer((args.host, args.port), runtime, secret)
    print(f"Yuxi BrowserSkill bridge listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
