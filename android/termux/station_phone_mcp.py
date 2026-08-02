#!/usr/bin/env python3
"""Minimal authenticated file transport for the Audience of One phone receiver."""

from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import os
import secrets
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

MAX_BODY_BYTES = 512_000
MAX_READ_LINES = 1_000


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


class StationMCPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, root: Path, token: str,
                 location_id: str):
        super().__init__(address, StationMCPHandler)
        self.root = root.resolve()
        self.token = token
        self.location_id = location_id
        self.sessions: set[str] = set()


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
        path = safe_path(self.server.root, arguments.get("path", ""))
        if tool == "android_write_file":
            content = arguments.get("content")
            if not isinstance(content, str):
                raise TransportError("content must be a string")
            atomic_write(path, content)
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
        )
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
