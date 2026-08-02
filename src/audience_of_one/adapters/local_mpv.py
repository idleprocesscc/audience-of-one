"""Exact local-file playout through one persistent mpv IPC process."""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from .spotify import SpotifyError

LOCAL_DEVICE_ID = "local_mac"
SUPPORTED_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}


class LocalMPVClient:
    """Play owned files below one configured library root and prove progress."""

    def __init__(
        self, config: dict[str, Any], state_path: Path, *,
        stream_resolver: Any | None = None,
        executable: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        root = str(config.get("library_root") or "").strip()
        if not root:
            raise SpotifyError("music.library_root is required for the local backend")
        self.root = Path(root).expanduser().resolve()
        self.state_path = state_path
        # A process-specific temporary directory splits the controller when station is
        # invoked from macOS Shortcuts. Keep transport identity with station state so
        # terminals, agents, and keyboard shortcuts all reach the same player.
        self.socket_path = state_path / "local-mpv.sock"
        self.pid_path = state_path / "local-mpv.pid"
        self.remote_state_path = state_path / "local-mpv-remote.json"
        self.stream_resolver = stream_resolver
        self._resolved_streams: dict[str, dict[str, Any]] = {}
        self.executable = executable or shutil.which("mpv")
        self.sleep = sleep
        self.monotonic = monotonic
        self.stability_window_seconds = float(config.get("stability_seconds", 1.2))
        self._request_id = 0
        if not self.executable:
            raise SpotifyError("local music backend needs mpv on PATH")
        if not self.root.is_dir():
            raise SpotifyError(f"local music library does not exist: {self.root}")

    @staticmethod
    def _computer_name() -> str:
        result = subprocess.run(
            [shutil.which("scutil") or "/usr/sbin/scutil", "--get", "ComputerName"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return result.stdout.strip() or "Local Mac"

    def ready_device(self, spec: str | None = None, *, require_snapshot: bool = True) -> dict:
        value = str(spec or LOCAL_DEVICE_ID).strip()
        name = self._computer_name()
        if value.casefold() not in {
            LOCAL_DEVICE_ID, "current", name.casefold(),
        }:
            raise SpotifyError("local music backend can target only this Mac")
        if require_snapshot:
            self._ensure_process()
        return {
            "id": LOCAL_DEVICE_ID, "name": name, "type": "Computer",
            "is_active": True, "selectable": False,
            "routing_proof": "local_mpv_process",
        }

    def devices(self) -> list[dict]:
        return [self.ready_device(require_snapshot=False)]

    def _path(self, uri: str) -> Path:
        if not uri.startswith("local:"):
            raise SpotifyError("local tracks use local:<relative-path>")
        raw = uri.removeprefix("local:")
        relative = PurePosixPath(raw)
        if not raw or relative.is_absolute() or ".." in relative.parts:
            raise SpotifyError("local track must stay below music.library_root")
        path = (self.root / Path(*relative.parts)).resolve(strict=False)
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise SpotifyError("local track escaped music.library_root") from error
        if path.suffix.casefold() not in SUPPORTED_SUFFIXES:
            raise SpotifyError(f"unsupported local audio type: {path.suffix or 'none'}")
        if not path.is_file():
            raise SpotifyError(f"local track does not exist: {relative.as_posix()}")
        return path

    def resolve(self, value: str) -> tuple[str, dict]:
        raw = value.strip()
        if raw.startswith(("qqmusic:", "qqmusic-search:")):
            if self.stream_resolver is None:
                raise SpotifyError("QQ Music input needs [qqmusic] enabled = true")
            try:
                uri, evidence, url = self.stream_resolver.resolve(raw)
            except Exception as error:
                raise SpotifyError(str(error)) from error
            self._resolved_streams[uri] = {"url": url, "evidence": evidence}
            return uri, evidence
        uri = raw if raw.startswith("local:") else f"local:{raw}"
        path = self._path(uri)
        relative = path.relative_to(self.root).as_posix()
        return f"local:{relative}", {
            "source": "local_library", "relative_path": relative,
            "bytes": path.stat().st_size,
        }

    @staticmethod
    def supports_track_uri(uri: str) -> bool:
        return uri.startswith(("local:", "qqmusic:"))

    def phone_stream(self, uri: str) -> dict[str, Any]:
        """Return one just-resolved QQ stream for transient phone staging."""
        if not uri.startswith("qqmusic:"):
            raise SpotifyError(
                "Android local-file playout is not shipped; phone music needs qqmusic:MID"
            )
        stream = self._resolved_streams.get(uri)
        if not stream:
            raise SpotifyError("QQ Music URL expired from this command; resolve the track again")
        evidence = stream["evidence"]
        return {
            "uri": uri,
            "url": stream["url"],
            "url_expiration_seconds": int(evidence.get("url_expiration_seconds") or 0),
            "name": evidence.get("name") or "",
            "artists": evidence.get("artists") or [],
            "album": evidence.get("album") or "",
            "duration_ms": int(evidence.get("duration_ms") or 0),
        }

    def _command(self, *command: Any) -> Any:
        self._ensure_process()
        self._request_id += 1
        request_id = self._request_id
        payload = json.dumps({
            "command": list(command), "request_id": request_id,
        }, separators=(",", ":")).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                connection.connect(str(self.socket_path))
                connection.sendall(payload)
                raw = b""
                response = None
                while len(raw) < 1_048_576:
                    chunk = connection.recv(65_536)
                    if not chunk:
                        break
                    raw += chunk
                    lines = raw.split(b"\n")
                    raw = lines.pop()
                    for line in lines:
                        try:
                            candidate = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(candidate, dict) \
                                and candidate.get("request_id") == request_id:
                            response = candidate
                            break
                    if response is not None:
                        break
        except OSError as error:
            raise SpotifyError(f"mpv IPC failed: {error}") from error
        if not isinstance(response, dict):
            raise SpotifyError("mpv returned no matching IPC response")
        if response.get("error") != "success":
            raise SpotifyError(f"mpv rejected {command[0]}: {response.get('error')}")
        return response.get("data")

    def _ping(self) -> bool:
        if not self.socket_path.exists():
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.3)
                connection.connect(str(self.socket_path))
                connection.sendall(b'{"command":["get_property","idle-active"]}\n')
                return bool(connection.recv(4096))
        except OSError:
            return False

    def _ensure_process(self) -> None:
        if self._ping():
            return
        self.socket_path.unlink(missing_ok=True)
        self.state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        process = subprocess.Popen(
            [
                self.executable, "--idle=yes", "--no-terminal", "--no-video",
                "--force-window=no", f"--input-ipc-server={self.socket_path}",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        self.pid_path.write_text(f"{process.pid}\n", encoding="ascii")
        self.pid_path.chmod(0o600)
        for _ in range(30):
            if self._ping():
                return
            if process.poll() is not None:
                break
            self.sleep(0.1)
        raise SpotifyError("mpv did not create its IPC socket")

    def _property(self, name: str, fallback: Any = None) -> Any:
        try:
            return self._command("get_property", name)
        except SpotifyError:
            return fallback

    @staticmethod
    def _metadata_value(metadata: Any, key: str) -> str:
        if not isinstance(metadata, dict):
            return ""
        wanted = key.casefold()
        for raw_key, raw_value in metadata.items():
            if str(raw_key).casefold() == wanted and raw_value is not None:
                return str(raw_value).strip()
        return ""

    def snapshot(self) -> dict | None:
        self._ensure_process()
        path_value = self._property("path")
        if not isinstance(path_value, str) or not path_value:
            return None
        remote = self._remote_state(path_value)
        if remote:
            paused = bool(self._property("pause", True))
            loop_file = self._property("loop-file", False)
            repeat_off = loop_file in {False, None, "", "no"}
            return {
                "uri": remote["uri"],
                "name": remote.get("name") or str(self._property("media-title", "QQ Music")),
                "artists": remote.get("artists") or [],
                "album": remote.get("album") or "",
                "content_type": "track",
                "is_playing": not paused,
                "progress_ms": round(float(self._property("time-pos", 0) or 0) * 1000),
                "duration_ms": round(float(self._property("duration", 0) or 0) * 1000),
                "repeat_state": "off" if repeat_off else "context",
                "volume_percent": round(float(self._property("volume", 0) or 0)),
                "device": self.ready_device(require_snapshot=False),
                "source": "qqmusic_api",
            }
        path = Path(path_value).resolve(strict=False)
        try:
            relative = path.relative_to(self.root).as_posix()
        except ValueError:
            return None
        paused = bool(self._property("pause", True))
        metadata = self._property("metadata", {})
        artist = self._metadata_value(metadata, "artist")
        album = self._metadata_value(metadata, "album")
        loop_file = self._property("loop-file", False)
        repeat_off = loop_file in {False, None, "", "no"}
        return {
            "uri": f"local:{relative}",
            "name": str(self._property("media-title", path.stem)),
            "artists": [artist] if artist else [],
            "album": album,
            "content_type": "track",
            "is_playing": not paused,
            "progress_ms": round(float(self._property("time-pos", 0) or 0) * 1000),
            "duration_ms": round(float(self._property("duration", 0) or 0) * 1000),
            "repeat_state": "off" if repeat_off else "context",
            "volume_percent": round(float(self._property("volume", 0) or 0)),
            "device": self.ready_device(require_snapshot=False),
        }

    def play_uri(self, uri: str, device: dict) -> dict:
        if uri.startswith("qqmusic:"):
            stream = self._resolved_streams.get(uri)
            if not stream:
                raise SpotifyError("QQ Music URL expired from this command; resolve the track again")
            url = stream["url"]
            evidence = stream["evidence"]
            for attempt in range(2):
                self._command("loadfile", url, "replace")
                self._command("set_property", "pause", False)
                self._save_remote_state(uri, url, evidence)
                if self._wait_for_remote_path(uri):
                    return {
                        "accepted": True, "uri": uri, "device": device,
                        "transport": "qqmusic_api_to_mpv", "url_stored": False,
                        "mpv_restarted": attempt == 1,
                    }
                if attempt == 0:
                    self.quit()
            raise SpotifyError("mpv accepted the QQ Music URL but did not load it")
        path = self._path(uri)
        self._command("loadfile", str(path), "replace")
        self._command("set_property", "pause", False)
        return {"accepted": True, "uri": uri, "device": device, "transport": "mpv_ipc"}

    def _wait_for_remote_path(self, uri: str) -> bool:
        for _ in range(30):
            snapshot = self.snapshot()
            if snapshot and snapshot.get("uri") == uri:
                return True
            self.sleep(0.1)
        return False

    def _save_remote_state(self, uri: str, url: str, evidence: dict[str, Any]) -> None:
        self.state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {
            "uri": uri,
            "url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
            "name": evidence.get("name") or "",
            "artists": evidence.get("artists") or [],
            "album": evidence.get("album") or "",
        }
        self.remote_state_path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        self.remote_state_path.chmod(0o600)

    def _remote_state(self, path_value: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self.remote_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not str(data.get("uri") or "").startswith("qqmusic:"):
            return None
        actual = hashlib.sha256(path_value.encode("utf-8")).hexdigest()
        return data if actual == data.get("url_sha256") else None

    def wait_for_playback(self, uri: str, device_id: str, timeout: float = 15) -> dict:
        deadline = self.monotonic() + timeout
        while self.monotonic() < deadline:
            snapshot = self.snapshot()
            if snapshot and snapshot["uri"] == uri and snapshot["is_playing"] \
                    and snapshot["device"]["id"] == device_id \
                    and snapshot["duration_ms"] > 0 \
                    and snapshot["progress_ms"] > 0:
                return snapshot
            self.sleep(0.1)
        raise SpotifyError(f"local playback confirmation timed out for {uri}")

    def set_repeat_on_device(self, mode: str, device: dict) -> dict:
        if mode not in {"off", "context"}:
            raise SpotifyError("local backend supports repeat off or context")
        self._command("set_property", "loop-file", "inf" if mode == "context" else "no")
        return {"accepted": True, "mode": mode, "device": device}

    def wait_for_repeat(
        self, mode: str, device_id: str, timeout: float = 5, expected_uri: str | None = None,
    ) -> dict:
        deadline = self.monotonic() + timeout
        while self.monotonic() < deadline:
            snapshot = self.snapshot()
            if snapshot and snapshot["device"]["id"] == device_id \
                    and snapshot["repeat_state"] == mode \
                    and (not expected_uri or snapshot["uri"] == expected_uri):
                return snapshot
            self.sleep(0.1)
        raise SpotifyError("local repeat confirmation timed out")

    def music_bed_probe(
        self, *, window: float = 1.2, min_advance_ms: int = 400,
        expected_uri: str | None = None, expected_device_id: str | None = None,
    ) -> dict:
        before = self.snapshot()
        self.sleep(max(0.05, window))
        after = self.snapshot()
        if not before or not after:
            return {"confirmed": False, "reason": "no_player"}
        if before["uri"] != after["uri"]:
            return {"confirmed": False, "reason": "track_changed", "actual_track": after}
        if expected_uri and after["uri"] != expected_uri:
            return {"confirmed": False, "reason": "unexpected_track", "actual_track": after}
        if expected_device_id and after["device"]["id"] != expected_device_id:
            return {"confirmed": False, "reason": "unexpected_device"}
        advance = after["progress_ms"] - before["progress_ms"]
        return {
            "confirmed": bool(after["is_playing"] and advance >= min_advance_ms),
            "proof": "position_motion", "advance_ms": advance,
            "uri": after["uri"], "device": after["device"],
            "reason": None if advance >= min_advance_ms else "insufficient_motion",
        }

    def pause(self, device_spec: str | None = None) -> dict:
        device = self.ready_device(device_spec)
        self._command("set_property", "pause", True)
        return {"accepted": True, "device": device}

    def resume(self, device_spec: str | None = None) -> dict:
        device = self.ready_device(device_spec)
        current = self.snapshot()
        if not current:
            raise SpotifyError("local player has no record to resume")
        self._command("set_property", "pause", False)
        return {"accepted": True, "device": device, "uri": current["uri"]}

    def wait_for_paused(self, device_id: str, timeout: float = 5) -> dict:
        deadline = self.monotonic() + timeout
        while self.monotonic() < deadline:
            snapshot = self.snapshot()
            if snapshot and not snapshot["is_playing"] \
                    and snapshot["device"]["id"] == device_id:
                return snapshot
            self.sleep(0.1)
        raise SpotifyError("local pause confirmation timed out")

    def wait_for_resumed(self, device_id: str, timeout: float = 5) -> dict:
        deadline = self.monotonic() + timeout
        while self.monotonic() < deadline:
            snapshot = self.snapshot()
            if snapshot and snapshot["is_playing"] \
                    and snapshot["device"]["id"] == device_id:
                return snapshot
            self.sleep(0.1)
        raise SpotifyError("local resume confirmation timed out")

    def quit(self, timeout: float = 3) -> dict:
        if not self._ping():
            self.socket_path.unlink(missing_ok=True)
            self.pid_path.unlink(missing_ok=True)
            self.remote_state_path.unlink(missing_ok=True)
            return {"accepted": True, "stopped": True, "already_stopped": True}
        self._command("quit")
        deadline = self.monotonic() + timeout
        while self.monotonic() < deadline:
            if not self._ping():
                self.socket_path.unlink(missing_ok=True)
                self.pid_path.unlink(missing_ok=True)
                self.remote_state_path.unlink(missing_ok=True)
                return {"accepted": True, "stopped": True, "already_stopped": False}
            self.sleep(0.1)
        raise SpotifyError("mpv quit confirmation timed out")

    def fader(self) -> MPVFader:
        return MPVFader(self, sleep=self.sleep)


class MPVFader:
    def __init__(self, client: LocalMPVClient, *, sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self.sleep = sleep

    def local_computer_name(self) -> str:
        return self.client._computer_name()

    def volume(self) -> int:
        return round(float(self.client._command("get_property", "volume")))

    def fade(self, target: int, seconds: float) -> dict:
        if not 0 <= target <= 100 or seconds < 0:
            raise SpotifyError("invalid local fader request")
        start = self.volume()
        steps = max(1, min(30, round(seconds * 10)))
        values = []
        for step in range(1, steps + 1):
            value = round(start + (target - start) * step / steps)
            self.client._command("set_property", "volume", value)
            values.append(value)
            if step < steps and seconds:
                self.sleep(seconds / steps)
        actual = self.volume()
        if abs(actual - target) > 1:
            raise SpotifyError(f"mpv fader target did not stick: wanted {target}, got {actual}")
        return {
            "from_percent": start, "to_percent": actual,
            "seconds": seconds, "steps": values,
        }
