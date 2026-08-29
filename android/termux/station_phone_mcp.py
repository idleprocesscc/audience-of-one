#!/usr/bin/env python3
"""Minimal authenticated file transport for the Audience of One phone receiver."""

from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

MAX_BODY_BYTES = 512_000
MAX_READ_LINES = 1_000
MAX_WAIT_SECONDS = 30.0


class TransportError(ValueError):
    pass


def safe_path(root: Path, raw: str) -> Path:
    """Resolve a relative transport path without allowing root escape."""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise TransportError("path must be a non-empty relative string")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise TransportError("path must stay below the configured station root")
    candidate = (root / Path(*relative.parts)).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise TransportError("path escaped the configured station root") from error
    return candidate


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class EventBus:
    """One in-process edge trigger for sleepers on both sides of the tunnel."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.sequence = 0
        self.event = "startup"

    def publish(self, event: str) -> int:
        with self.condition:
            self.sequence += 1
            self.event = event
            self.condition.notify_all()
            return self.sequence

    def wait(self, after: int, timeout: float) -> dict[str, Any]:
        with self.condition:
            if self.sequence <= after:
                self.condition.wait(timeout)
            return {"sequence": self.sequence, "event": self.event}


class PowerLeaseManager:
    """Hold Termux's wakelock only while named, expiring work is active."""

    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.lock = threading.Lock()
        self.leases: dict[str, float] = {}
        self.timer: threading.Timer | None = None
        self.owns_wakelock = False
        self.supported = bool(
            shutil.which("termux-wake-lock") and shutil.which("termux-wake-unlock")
        )

    def _command(self, command: str) -> bool:
        if not self.supported:
            return False
        try:
            return subprocess.run(
                [command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _live(self, now: float | None = None) -> dict[str, float]:
        moment = time.time() if now is None else now
        return {name: expiry for name, expiry in self.leases.items() if expiry > moment}

    def _write_state_locked(self) -> None:
        atomic_write(self.state_path, json.dumps({
            "version": 1,
            "wakelock": self.owns_wakelock,
            "supported": self.supported,
            "updated_at": int(time.time()),
            "leases": self.leases,
        }, separators=(",", ":")) + "\n")

    def _schedule_locked(self) -> None:
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if not self.leases:
            return
        delay = max(0.05, min(self.leases.values()) - time.time())
        self.timer = threading.Timer(delay, self._expire)
        self.timer.daemon = True
        self.timer.start()

    def _expire(self) -> None:
        with self.lock:
            self.leases = self._live()
            if not self.leases and self.owns_wakelock:
                self._command("termux-wake-unlock")
                self.owns_wakelock = False
            self._write_state_locked()
            self._schedule_locked()

    def set(self, lease_id: str, action: str, ttl_seconds: float = 0) -> dict[str, Any]:
        if not re_full_safe_id(lease_id):
            raise TransportError("lease_id contains unsafe characters")
        if action not in {"acquire", "renew", "release", "status"}:
            raise TransportError("lease action must be acquire, renew, release, or status")
        with self.lock:
            self.leases = self._live()
            if action in {"acquire", "renew"}:
                if not 1 <= ttl_seconds <= 14_400:
                    raise TransportError("lease ttl_seconds must be from 1 to 14400")
                self.leases[lease_id] = time.time() + ttl_seconds
                if not self.owns_wakelock:
                    self.owns_wakelock = self._command("termux-wake-lock")
            elif action == "release":
                self.leases.pop(lease_id, None)
                if not self.leases and self.owns_wakelock:
                    self._command("termux-wake-unlock")
                    self.owns_wakelock = False
            self._write_state_locked()
            self._schedule_locked()
            return {
                "lease_id": lease_id,
                "action": action,
                "wakelock": self.owns_wakelock,
                "supported": self.supported,
                "expires_at": self.leases.get(lease_id, 0),
                "active_leases": sorted(self.leases),
            }

    def close(self) -> None:
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            if self.owns_wakelock:
                self._command("termux-wake-unlock")
                self.owns_wakelock = False
            self.leases.clear()
            self._write_state_locked()


def re_full_safe_id(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 180 and all(
        character.isalnum() or character in "._:-" for character in value
    )


class StationMCPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, root: Path, token: str,
                 location_id: str, event_socket: Path | None = None,
                 event_fifo: Path | None = None,
                 power_state: Path | None = None):
        super().__init__(address, StationMCPHandler)
        self.root = root.resolve()
        self.token = token
        self.location_id = location_id
        self.sessions: set[str] = set()
        self.events = EventBus()
        self.event_fifo = event_fifo or (self.root / ".player-events")
        self.event_socket = event_socket or (self.root / ".runtime-events.sock")
        self.power = PowerLeaseManager(power_state or (self.root / "power-leases.json"))
        self._event_stop = threading.Event()
        self._event_listener = threading.Thread(target=self._listen_events, daemon=True)
        self._event_listener.start()

    def _listen_events(self) -> None:
        self.event_socket.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.event_socket.unlink()
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            listener.bind(str(self.event_socket))
            listener.settimeout(1.0)
            while not self._event_stop.is_set():
                try:
                    payload = listener.recv(512).decode("utf-8", errors="replace").strip()
                except socket.timeout:
                    continue
                self.publish_event(payload or "local")
        finally:
            listener.close()
            try:
                self.event_socket.unlink()
            except FileNotFoundError:
                pass

    def publish_event(self, event: str) -> None:
        self.events.publish(event[:200])
        try:
            descriptor = os.open(self.event_fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            os.write(descriptor, (event[:200] + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)

    def server_close(self) -> None:
        self._event_stop.set()
        self.power.close()
        super().server_close()
        if self._event_listener.is_alive():
            self._event_listener.join(timeout=1.2)


class StationMCPHandler(BaseHTTPRequestHandler):
    server_version = "AudienceOfOnePhone/1"
    server: StationMCPServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: dict[str, Any], *, session: str = "") -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if session:
            self.send_header("mcp-session-id", session)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        supplied = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        return bool(supplied and hmac.compare_digest(supplied, self.server.token))

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._json(HTTPStatus.OK, {"status": "healthy", "version": 1})

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {
                "jsonrpc": "2.0", "error": {"code": -32001, "message": "unauthorized"},
            })
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 1 or length > MAX_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {
                "jsonrpc": "2.0", "error": {"code": -32600, "message": "invalid body"},
            })
            return
        try:
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise TransportError("request must be an object")
            response, session = self._dispatch(request)
        except (UnicodeDecodeError, json.JSONDecodeError, TransportError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {
                "jsonrpc": "2.0", "error": {"code": -32602, "message": str(error)},
            })
            return
        if response is None:
            self.send_response(HTTPStatus.ACCEPTED)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(HTTPStatus.OK, response, session=session)

    def _dispatch(self, request: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            session = secrets.token_urlsafe(24)
            self.server.sessions.add(session)
            return ({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "audience-of-one-phone", "version": "0.1"},
                },
            }, session)
        session = self.headers.get("mcp-session-id", "")
        if session not in self.server.sessions:
            raise TransportError("invalid or expired MCP session")
        if method == "notifications/initialized":
            return None, ""
        if method != "tools/call":
            raise TransportError("unsupported MCP method")
        params = request.get("params")
        if not isinstance(params, dict):
            raise TransportError("tools/call params must be an object")
        tool = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            raise TransportError("tool arguments must be an object")
        text = self._tool(str(tool or ""), arguments)
        return ({
            "jsonrpc": "2.0", "id": request_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": False},
        }, "")

    def _tool(self, tool: str, arguments: dict[str, Any]) -> str:
        if arguments.get("location_id") != self.server.location_id:
            raise TransportError("unknown storage location")
        if tool == "station_power_lease":
            result = self.server.power.set(
                str(arguments.get("lease_id") or ""),
                str(arguments.get("action") or "status"),
                float(arguments.get("ttl_seconds") or 0),
            )
            return json.dumps(result, separators=(",", ":"))
        if tool == "station_wait_event":
            after = arguments.get("after", 0)
            timeout = arguments.get("timeout_seconds", 25.0)
            if not isinstance(after, int) or after < 0:
                raise TransportError("after must be a non-negative integer")
            if not isinstance(timeout, (int, float)) or not 0 <= timeout <= MAX_WAIT_SECONDS:
                raise TransportError("timeout_seconds is outside the supported range")
            return json.dumps(self.server.events.wait(after, float(timeout)), separators=(",", ":"))
        path = safe_path(self.server.root, arguments.get("path", ""))
        if tool == "android_write_file":
            content = arguments.get("content")
            if not isinstance(content, str):
                raise TransportError("content must be a string")
            atomic_write(path, content)
            relative = path.relative_to(self.server.root)
            fader_ready = bool(
                relative.parts
                and relative.parts[0] == "station-fader-inbox"
                and path.suffix == ".request"
            )
            if fader_ready:
                remote_id = path.name.split(".", 1)[0]
                self.server.power.set(f"delivery:{remote_id}", "acquire", 180)
                self.server.publish_event(f"fader:{remote_id}")
            return "written"
        if tool == "android_append_file":
            content = arguments.get("content")
            if not isinstance(content, str):
                raise TransportError("content must be a string")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            relative = path.relative_to(self.server.root)
            queue_ready = bool(
                relative.parts
                and relative.parts[0] in {"station-inbox", "station-music-inbox"}
                and "END." in content
            )
            fader_ready = bool(
                relative.parts
                and relative.parts[0] == "station-fader-inbox"
                and path.suffix == ".request"
            )
            if queue_ready or fader_ready:
                remote_id = path.name.split(".", 1)[0]
                self.server.power.set(f"delivery:{remote_id}", "acquire", 180)
                self.server.publish_event(
                    f"{'fader' if fader_ready else 'queue'}:{remote_id}"
                )
            return "appended"
        if tool == "android_delete_file":
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return "deleted"
        if tool == "android_read_file":
            start = arguments.get("start_line", 1)
            limit = arguments.get("max_lines", 100)
            if not isinstance(start, int) or start < 1:
                raise TransportError("start_line must be a positive integer")
            if not isinstance(limit, int) or not 1 <= limit <= MAX_READ_LINES:
                raise TransportError("max_lines is outside the supported range")
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return ""
            return "\n".join(lines[start - 1:start - 1 + limit])
        if tool == "android_list_files":
            try:
                entries = sorted(path.iterdir(), key=lambda entry: entry.name)
            except FileNotFoundError:
                return json.dumps({"files": []})
            except NotADirectoryError as error:
                raise TransportError("path is not a directory") from error
            return json.dumps({"files": [
                {"name": entry.name, "is_directory": entry.is_dir()}
                for entry in entries
            ]})
        raise TransportError("unsupported phone transport tool")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--bind", default=os.environ.get("STATION_PHONE_BIND", "127.0.0.1"))
    result.add_argument("--port", type=int,
                        default=int(os.environ.get("STATION_PHONE_PORT", "8787")))
    result.add_argument("--root", type=Path,
                        default=Path(os.environ.get(
                            "STATION_PHONE_ROOT", "~/storage/shared/Download/AudienceOfOne"
                        )).expanduser())
    result.add_argument("--token-file", type=Path,
                        default=Path(os.environ.get(
                            "STATION_PHONE_TOKEN_FILE", "~/.config/audience-of-one/phone-token"
                        )).expanduser())
    result.add_argument("--location-id",
                        default=os.environ.get("STATION_PHONE_LOCATION_ID", "station"))
    result.add_argument("--pid-file", type=Path,
                        default=Path(os.environ.get(
                            "STATION_PHONE_MCP_PID_FILE",
                            "~/.local/state/audience-of-one-phone/mcp.pid",
                        )).expanduser())
    result.add_argument("--event-socket", type=Path,
                        default=Path(os.environ.get(
                            "STATION_PHONE_EVENT_SOCKET",
                            "~/.local/state/audience-of-one-phone/events.sock",
                        )).expanduser())
    result.add_argument("--event-fifo", type=Path,
                        default=Path(os.environ.get(
                            "STATION_PHONE_EVENT_FIFO",
                            "~/.local/state/audience-of-one-phone/player.events",
                        )).expanduser())
    result.add_argument("--power-state", type=Path,
                        default=Path(os.environ.get(
                            "STATION_PHONE_POWER_STATE",
                            "~/.local/state/audience-of-one-phone/power-leases.json",
                        )).expanduser())
    return result


def main() -> int:
    arguments = parser().parse_args()
    if not 1 <= arguments.port <= 65535:
        raise SystemExit("port must be from 1 to 65535")
    try:
        token = arguments.token_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise SystemExit(f"cannot read phone token: {error}") from error
    if len(token) < 32:
        raise SystemExit("phone token must contain at least 32 characters")
    if not arguments.location_id or any(char.isspace() for char in arguments.location_id):
        raise SystemExit("location id must be non-empty and contain no whitespace")
    arguments.root.mkdir(parents=True, exist_ok=True)
    arguments.pid_file.parent.mkdir(parents=True, exist_ok=True)
    with arguments.pid_file.open("a+", encoding="ascii") as pid_handle:
        try:
            fcntl.flock(pid_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit("phone MCP server is already running") from error
        pid_handle.seek(0)
        pid_handle.truncate()
        pid_handle.write(f"{os.getpid()}\n")
        pid_handle.flush()
        server = StationMCPServer(
            (arguments.bind, arguments.port), root=arguments.root,
            token=token, location_id=arguments.location_id,
            event_socket=arguments.event_socket,
            event_fifo=arguments.event_fifo,
            power_state=arguments.power_state,
        )
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
