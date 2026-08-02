"""Android voice delivery over a small, receipt-driven MCP transport."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any


class PhoneError(RuntimeError):
    pass


class PhoneUncertainError(PhoneError):
    """The phone proved voice start but did not prove completion."""

    def __init__(self, message: str, receipts: dict[str, dict[str, Any]]):
        super().__init__(message)
        self.receipts = receipts


def _json_message(raw: str) -> dict[str, Any]:
    """Decode either a JSON response or MCP's SSE-shaped JSON response."""
    if raw.strip() == "null":
        return {}
    candidates = [
        line.removeprefix("data:").strip()
        for line in raw.splitlines()
        if line.startswith("data:")
    ]
    if not candidates:
        candidates = [raw.strip()]
    for candidate in reversed(candidates):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise PhoneError("phone MCP returned an invalid JSON response")


class MCPPhoneTransport:
    """Call the Android MCP filesystem tools through one reusable session."""

    def __init__(
        self,
        url: str,
        token: str,
        location_id: str,
        *,
        path_prefix: str = "",
        timeout: float = 10.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ):
        if not url.startswith(("http://", "https://")):
            raise PhoneError("android MCP URL must use http or https")
        if not token:
            raise PhoneError("android MCP bearer token is empty")
        if not location_id:
            raise PhoneError("android MCP storage location is empty")
        self.url = url
        self.token = token
        self.location_id = location_id
        prefix = PurePosixPath(path_prefix.strip()) if path_prefix.strip() else None
        if prefix and (prefix.is_absolute() or ".." in prefix.parts):
            raise PhoneError("android transport path prefix must stay below its location")
        self.path_prefix = prefix
        self.timeout = max(1.0, float(timeout))
        self.opener = opener
        self.session_id: str | None = None
        self.request_id = 0
        self.lock = threading.Lock()

    def _post(self, payload: dict[str, Any], *, session: bool = True) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "Audience-of-One/0.1",
        }
        if session and self.session_id:
            headers["mcp-session-id"] = self.session_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers=headers,
            method="POST",
        )
        try:
            response = self.opener(request, timeout=self.timeout)
            raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            # The phone server uses HTTP 400 for an expired MCP session. Keep
            # its JSON-RPC body so call() can discard the stale session and
            # initialize once more; treating this as a transport outage leaves
            # a healthy phone stuck until the Mac process is restarted.
            raw = error.read().decode("utf-8", errors="replace")
            if raw.strip():
                try:
                    return _json_message(raw)
                except PhoneError:
                    pass
            raise PhoneError(
                f"phone MCP request failed: HTTP {error.code}"
            ) from error
        except (OSError, urllib.error.URLError) as error:
            raise PhoneError(f"phone MCP request failed: {error}") from error
        returned_session = response.headers.get("mcp-session-id")
        if returned_session:
            self.session_id = returned_session.strip()
        if not raw.strip():
            return {}
        return _json_message(raw)

    def _ensure_session(self) -> None:
        if self.session_id:
            return
        initialized = self._post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "audience-of-one", "version": "0.1"},
            },
        }, session=False)
        if initialized.get("error") or not self.session_id:
            raise PhoneError("phone MCP initialization did not return a session")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    @staticmethod
    def _session_rejected(response: dict[str, Any]) -> bool:
        evidence = json.dumps(response, ensure_ascii=False).casefold()
        return "session" in evidence and any(
            token in evidence for token in ("invalid", "missing", "not found", "expired")
        )

    def call(self, tool: str, arguments: dict[str, Any]) -> str:
        with self.lock:
            self._ensure_session()
            self.request_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": self.request_id + 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
            response = self._post(payload)
            if self._session_rejected(response):
                self.session_id = None
                self._ensure_session()
                response = self._post(payload)
        if response.get("error"):
            raise PhoneError(f"phone MCP {tool} failed")
        result = response.get("result")
        if not isinstance(result, dict) or result.get("isError") is True:
            raise PhoneError(f"phone MCP {tool} failed")
        content = result.get("content") or []
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )

    def delete(self, path: str, *, missing_ok: bool = True) -> None:
        try:
            self.call("android_delete_file", {
                "location_id": self.location_id,
                "path": self._path(path),
            })
        except PhoneError:
            if not missing_ok:
                raise

    def write(self, path: str, content: str, *, append: bool = False) -> None:
        self.call("android_append_file" if append else "android_write_file", {
            "location_id": self.location_id,
            "path": self._path(path),
            "content": content,
        })

    def replace(self, path: str, content: str) -> None:
        self.delete(path)
        self.write(path, content)

    def read(self, path: str) -> str:
        raw = self.call("android_read_file", {
            "location_id": self.location_id,
            "path": self._path(path),
            "start_line": 1,
            "max_lines": 100,
        })
        numbered = []
        for line in raw.splitlines():
            match = re.match(r"^\s*\d+\|\s?(.*)$", line)
            if match:
                numbered.append(match.group(1))
        return "\n".join(numbered) if numbered else raw

    def list_names(self, path: str) -> list[str]:
        raw = self.call("android_list_files", {
            "location_id": self.location_id,
            "path": self._path(path),
        })
        start = raw.find("{")
        if start < 0:
            return []
        try:
            data = json.loads(raw[start:])
        except json.JSONDecodeError as error:
            raise PhoneError("phone MCP returned an invalid file listing") from error
        files = data.get("files")
        if not isinstance(files, list):
            return []
        return [
            str(entry.get("name") or "")
            for entry in files
            if isinstance(entry, dict) and entry.get("name") and not entry.get("is_directory")
        ]

    def read_lines(self, path: str, *, start_line: int = 1, max_lines: int = 100) -> list[str]:
        raw = self.call("android_read_file", {
            "location_id": self.location_id,
            "path": self._path(path),
            "start_line": start_line,
            "max_lines": max_lines,
        })
        return raw.splitlines()

    def _path(self, path: str) -> str:
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise PhoneError("phone transport path must stay below its location")
        return str(self.path_prefix / relative) if self.path_prefix else str(relative)


def _safe_remote_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise PhoneError("phone remote ID contains unsafe characters")
    return value


def _safe_remote_dir(value: str) -> str:
    directory = PurePosixPath(value.strip())
    if not value.strip() or directory.is_absolute() or ".." in directory.parts:
        raise PhoneError("phone remote directory must stay below its location")
    return str(directory)


def _events(raw: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for line in raw.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            row = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        event = row.get("event")
        if isinstance(event, str) and event not in found:
            found[event] = row
    return found


class PhoneVoicePlayer:
    """Stage one clip, trigger it once, then wait for phone-side evidence."""

    def __init__(
        self,
        transport: Any,
        *,
        start_timeout: float = 30.0,
        finish_margin: float = 20.0,
        poll_interval: float = 0.5,
        chunk_size: int = 100_000,
        duck_percent: int = 35,
        fade_down_seconds: float = 0.8,
        fade_up_seconds: float = 1.0,
        inbox_dir: str = "station-inbox",
        ack_dir: str = "station-acks",
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.transport = transport
        self.start_timeout = max(1.0, float(start_timeout))
        self.finish_margin = max(1.0, float(finish_margin))
        self.poll_interval = max(0.05, float(poll_interval))
        self.chunk_size = max(1024, int(chunk_size))
        self.duck_percent = max(0, min(100, int(duck_percent)))
        self.fade_down_seconds = max(0.0, float(fade_down_seconds))
        self.fade_up_seconds = max(0.0, float(fade_up_seconds))
        self.inbox_dir = _safe_remote_dir(inbox_dir)
        self.ack_dir = _safe_remote_dir(ack_dir)
        self.monotonic = monotonic
        self.sleep = sleep

    def _stage_and_trigger(
        self, path: Path, remote_id: str, *, duck: bool
    ) -> dict[str, Any]:
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise PhoneError(f"cannot read phone voice audio: {error}") from error
        if not raw:
            raise PhoneError("phone voice audio is empty")
        encoded = base64.b64encode(raw).decode("ascii")
        inbox = f"{self.inbox_dir}/{remote_id}.b64"
        integrity_path = f"{self.inbox_dir}/{remote_id}.integrity"
        ack_path = f"{self.ack_dir}/{remote_id}.jsonl"
        for stale in (inbox, integrity_path):
            self.transport.delete(stale)
        for offset in range(0, len(encoded), self.chunk_size):
            self.transport.write(
                inbox,
                encoded[offset:offset + self.chunk_size],
                append=offset > 0,
            )
        integrity = "\n".join([
            "version=1",
            f"size={len(raw)}",
            f"sha256={hashlib.sha256(raw).hexdigest()}",
            f"duck={1 if duck else 0}",
            f"duck_percent={self.duck_percent}",
            f"fade_down_seconds={self.fade_down_seconds}",
            f"fade_up_seconds={self.fade_up_seconds}",
            "ready=1",
            "",
        ])
        self.transport.replace(integrity_path, integrity)
        self.transport.write(inbox, "\nEND.\n", append=True)
        return {
            "remote_id": remote_id,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "ack_path": ack_path,
            "duck_requested": duck,
        }

    def _wait(self, ack_path: str, deadline: float) -> dict[str, dict[str, Any]]:
        last: dict[str, dict[str, Any]] = {}
        while self.monotonic() < deadline:
            try:
                last = _events(self.transport.read(ack_path))
            except PhoneError:
                last = {}
            failed = last.get("voice_failed") or last.get("voice_expired")
            if failed:
                raise PhoneError(
                    f"phone voice failed: {failed.get('detail') or failed.get('event')}"
                )
            yield_events = last
            if "voice_finished" in yield_events:
                return yield_events
            self.sleep(self.poll_interval)
        return last

    @staticmethod
    def _result(staged: dict[str, Any], receipts: dict[str, dict[str, Any]]) -> dict:
        duck_requested = bool(staged.get("duck_requested", True))
        order = (
            ("duck_started", "voice_started", "duck_finished", "voice_finished")
            if duck_requested else ("voice_started", "voice_finished")
        )
        ordered = [receipts.get(name, {}).get("at") for name in order]
        ordered_numbers = all(isinstance(value, (int, float)) for value in ordered)
        ack_order_valid = bool(ordered_numbers and ordered == sorted(ordered))
        duck_failed = receipts.get("duck_failed") or receipts.get("duck_release_failed")
        duck_confirmed = bool(
            "duck_started" in receipts and "duck_finished" in receipts and not duck_failed
        )
        return {
            **staged,
            "played": True,
            "voice_confirmed": True,
            "duck_confirmed": duck_confirmed,
            "ack_order_valid": ack_order_valid,
            "events": receipts,
        }

    def play(
        self, path: Path, duration: float, *, remote_id: str, duck: bool = True
    ) -> dict[str, Any]:
        remote_id = _safe_remote_id(remote_id)
        ack_path = f"{self.ack_dir}/{remote_id}.jsonl"
        try:
            existing = _events(self.transport.read(ack_path))
        except PhoneError:
            existing = {}
        failed = existing.get("voice_failed") or existing.get("voice_expired")
        if failed:
            raise PhoneError(
                f"previous phone voice attempt failed: "
                f"{failed.get('detail') or failed.get('event')}"
            )
        if "voice_finished" in existing:
            return self._result({
                "remote_id": remote_id,
                "ack_path": ack_path,
                "duck_requested": duck,
                "reused_completed_receipt": True,
            }, existing)
        if "voice_started" in existing:
            raise PhoneUncertainError(
                "previous phone voice attempt started without a finish receipt; "
                "refusing automatic replay",
                existing,
            )
        staged = self._stage_and_trigger(path, remote_id, duck=duck)
        started_deadline = self.monotonic() + self.start_timeout
        receipts: dict[str, dict[str, Any]] = {}
        while self.monotonic() < started_deadline:
            try:
                receipts = _events(self.transport.read(staged["ack_path"]))
            except PhoneError:
                receipts = {}
            failed = receipts.get("voice_failed") or receipts.get("voice_expired")
            if failed:
                raise PhoneError(
                    f"phone voice failed: {failed.get('detail') or failed.get('event')}"
                )
            if "voice_started" in receipts:
                break
            self.sleep(self.poll_interval)
        if "voice_started" not in receipts:
            raise PhoneError("timed out waiting for phone voice_started")

        finish_deadline = self.monotonic() + max(1.0, float(duration)) + self.finish_margin
        receipts = self._wait(staged["ack_path"], finish_deadline)
        if "voice_finished" not in receipts:
            raise PhoneUncertainError(
                "timed out waiting for phone voice_finished; refusing automatic replay",
                receipts,
            )

        return self._result(staged, receipts)


class PhoneMusicPlayer:
    """Stage one expiring online stream and require phone-side position evidence."""

    def __init__(
        self,
        transport: Any,
        *,
        start_timeout: float = 30.0,
        poll_interval: float = 0.5,
        chunk_size: int = 100_000,
        inbox_dir: str = "station-music-inbox",
        ack_dir: str = "station-acks",
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.transport = transport
        self.start_timeout = max(1.0, float(start_timeout))
        self.poll_interval = max(0.05, float(poll_interval))
        self.chunk_size = max(1024, int(chunk_size))
        self.inbox_dir = _safe_remote_dir(inbox_dir)
        self.ack_dir = _safe_remote_dir(ack_dir)
        self.monotonic = monotonic
        self.sleep = sleep

    @staticmethod
    def _position(detail: str) -> dict[str, Any]:
        values = dict(re.findall(r"([a-z]+)-([A-Za-z0-9]+)", detail or ""))
        try:
            return {
                "url_sha256": values["url"],
                "progress_ms": int(values["progress"]),
                "duration_ms": int(values["duration"]),
                "volume_percent": int(values.get("volume", 100)),
                "repeat_state": values.get("repeat", "unknown"),
            }
        except (KeyError, TypeError, ValueError) as error:
            raise PhoneError("phone music returned an invalid position receipt") from error

    def _stage(self, stream: dict[str, Any], remote_id: str) -> dict[str, Any]:
        url = stream.get("url")
        uri = stream.get("uri")
        if not isinstance(url, str) or urllib.parse.urlparse(url).scheme not in {"http", "https"}:
            raise PhoneError("phone music stream needs an HTTP URL")
        if not isinstance(uri, str) or not uri.startswith("qqmusic:"):
            raise PhoneError("phone music currently accepts one exact QQ Music MID")
        url_sha256 = hashlib.sha256(url.encode()).hexdigest()
        expiration = int(stream.get("url_expiration_seconds") or 0)
        issued_at = time.time()
        payload = json.dumps({
            "version": 1,
            "uri": uri,
            "url": url,
            "url_sha256": url_sha256,
            "issued_at": issued_at,
            "expires_at": issued_at + expiration if expiration else 0,
            "name": str(stream.get("name") or ""),
            "artists": list(stream.get("artists") or []),
            "album": str(stream.get("album") or ""),
            "duration_ms": int(stream.get("duration_ms") or 0),
        }, ensure_ascii=False, separators=(",", ":")).encode()
        encoded = base64.b64encode(payload).decode("ascii")
        inbox = f"{self.inbox_dir}/{remote_id}.b64"
        integrity_path = f"{self.inbox_dir}/{remote_id}.integrity"
        ack_path = f"{self.ack_dir}/{remote_id}.jsonl"
        for stale in (inbox, integrity_path):
            self.transport.delete(stale)
        for offset in range(0, len(encoded), self.chunk_size):
            self.transport.write(
                inbox, encoded[offset:offset + self.chunk_size], append=offset > 0,
            )
        self.transport.replace(integrity_path, "\n".join([
            "version=1", f"size={len(payload)}",
            f"sha256={hashlib.sha256(payload).hexdigest()}", "ready=1", "",
        ]))
        self.transport.write(inbox, "\nEND.\n", append=True)
        return {
            "remote_id": remote_id,
            "uri": uri,
            "url_sha256": url_sha256,
            "ack_path": ack_path,
            "bytes": len(payload),
        }

    def _result(
        self,
        staged: dict[str, Any],
        receipts: dict[str, dict[str, Any]],
        *,
        reused: bool = False,
    ) -> dict[str, Any]:
        position = self._position(str(receipts["track_position"].get("detail") or ""))
        if position["url_sha256"] != staged["url_sha256"]:
            raise PhoneError("phone music receipt belongs to a different stream")
        if position["progress_ms"] < 400 or position["duration_ms"] <= 0:
            raise PhoneError("phone music position did not prove an audible bed")
        return {
            **staged,
            "accepted": True,
            "confirmed": True,
            "url_stored": False,
            "transport": "qqmusic_api_to_phone_mpv",
            "reused_completed_receipt": reused,
            "playback": {
                "uri": staged["uri"],
                "is_playing": True,
                **{key: value for key, value in position.items() if key != "url_sha256"},
            },
            "music_bed": {
                "confirmed": True,
                "proof": "position_motion",
                "advance_ms": position["progress_ms"],
                "uri": staged["uri"],
            },
            "events": receipts,
        }

    def play(self, stream: dict[str, Any], *, remote_id: str) -> dict[str, Any]:
        remote_id = _safe_remote_id(remote_id)
        ack_path = f"{self.ack_dir}/{remote_id}.jsonl"
        url = stream.get("url")
        if not isinstance(url, str):
            raise PhoneError("phone music stream needs an HTTP URL")
        uri = stream.get("uri")
        if not isinstance(uri, str):
            raise PhoneError("phone music stream needs one exact URI")
        identity = {
            "remote_id": remote_id,
            "uri": uri,
            "url_sha256": hashlib.sha256(url.encode()).hexdigest(),
            "ack_path": ack_path,
        }
        try:
            existing = _events(self.transport.read(ack_path))
        except PhoneError:
            existing = {}
        failed = existing.get("track_failed")
        if failed:
            raise PhoneError(
                f"previous phone music attempt failed: "
                f"{failed.get('detail') or failed.get('event')}"
            )
        if "track_position" in existing:
            return self._result(identity, existing, reused=True)
        if "track_started" in existing:
            raise PhoneError(
                "previous phone music attempt started without position evidence; "
                "refusing automatic replay"
            )
        staged = self._stage(stream, remote_id)
        deadline = self.monotonic() + self.start_timeout
        receipts: dict[str, dict[str, Any]] = {}
        while self.monotonic() < deadline:
            try:
                receipts = _events(self.transport.read(ack_path))
            except PhoneError:
                receipts = {}
            failed = receipts.get("track_failed")
            if failed:
                raise PhoneError(
                    f"phone music failed: {failed.get('detail') or failed.get('event')}"
                )
            if "track_started" in receipts and "track_position" in receipts:
                break
            self.sleep(self.poll_interval)
        if "track_started" not in receipts or "track_position" not in receipts:
            raise PhoneError("timed out waiting for phone music position receipt")
        return self._result(staged, receipts)


def phone_transport(data: dict[str, Any]) -> MCPPhoneTransport:
    android = data.get("android") or {}
    url_env = str(android.get("mcp_url_env") or "STATION_PHONE_MCP_URL")
    token_env = str(android.get("mcp_token_env") or "STATION_PHONE_MCP_TOKEN")
    url = os.environ.get(url_env, "")
    token = os.environ.get(token_env, "")
    if not url:
        raise PhoneError(f"set environment variable {url_env}")
    if not token:
        raise PhoneError(f"set environment variable {token_env}")
    return MCPPhoneTransport(
        url,
        token,
        str(android.get("location_id") or ""),
        path_prefix=str(android.get("path_prefix") or ""),
        timeout=float(android.get("mcp_timeout_seconds", 10.0)),
    )


def phone_voice_player(data: dict[str, Any]) -> PhoneVoicePlayer:
    android = data.get("android") or {}
    transport = phone_transport(data)
    desktop = data.get("desktop") or {}
    return PhoneVoicePlayer(
        transport,
        start_timeout=float(android.get("voice_start_timeout_seconds", 30.0)),
        finish_margin=float(android.get("voice_finish_margin_seconds", 20.0)),
        duck_percent=int(desktop.get("duck_percent", 35)),
        fade_down_seconds=float(desktop.get("fade_down_seconds", 0.8)),
        fade_up_seconds=float(desktop.get("fade_up_seconds", 1.0)),
        inbox_dir=str(android.get("voice_inbox") or "station-inbox"),
        ack_dir=str(android.get("ack_dir") or "station-acks"),
    )


def phone_music_player(data: dict[str, Any]) -> PhoneMusicPlayer:
    android = data.get("android") or {}
    transport = phone_transport(data)
    return PhoneMusicPlayer(
        transport,
        start_timeout=float(android.get("music_start_timeout_seconds", 30.0)),
        inbox_dir=str(android.get("music_inbox") or "station-music-inbox"),
        ack_dir=str(android.get("ack_dir") or "station-acks"),
    )
