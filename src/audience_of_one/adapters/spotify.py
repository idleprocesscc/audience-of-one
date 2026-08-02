"""Spotify Web API client with exact playback receipts."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import spotify_resolver

API_ROOT = "https://api.spotify.com"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class SpotifyError(RuntimeError):
    pass


class SpotifyClient:
    def __init__(self, config: dict[str, Any], state_path: Path):
        self.config = config
        self.state_path = state_path
        self.token_path = state_path / "secrets" / "spotify-tokens.json"

    def _credential(self, field: str) -> str:
        env_name = self.config.get(field)
        if not isinstance(env_name, str) or not env_name:
            raise SpotifyError(f"spotify.{field} must name an environment variable")
        value = os.environ.get(env_name)
        if not value:
            raise SpotifyError(f"environment variable {env_name} is not set")
        return value

    def _load_tokens(self) -> dict:
        try:
            return json.loads(self.token_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise SpotifyError("Spotify is not authorized; run station spotify auth") from error
        except (OSError, json.JSONDecodeError) as error:
            raise SpotifyError("Spotify token file is unreadable") from error

    def _save_tokens(self, data: dict) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.token_path.with_name(f".{self.token_path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            os.replace(temporary, self.token_path)
        finally:
            temporary.unlink(missing_ok=True)

    def access_token(self) -> str:
        tokens = self._load_tokens()
        if time.time() < float(tokens.get("expires_at", 0)) - 60:
            return tokens["access_token"]
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise SpotifyError("Spotify refresh token is missing; authorize again")
        request = urllib.request.Request(
            self.config.get("token_endpoint") or TOKEN_URL,
            data=urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._credential("client_id_env"),
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                refreshed = json.loads(response.read())
        except Exception as error:
            raise SpotifyError(f"Spotify token refresh failed: {error}") from error
        tokens["access_token"] = refreshed["access_token"]
        tokens["expires_at"] = time.time() + int(refreshed["expires_in"])
        if refreshed.get("refresh_token"):
            tokens["refresh_token"] = refreshed["refresh_token"]
        self._save_tokens(tokens)
        return tokens["access_token"]

    def authorized_scopes(self) -> set[str]:
        return set(str(self._load_tokens().get("scope") or "").split())

    def require_scopes(self, required: set[str], *, action: str) -> None:
        missing = sorted(required - self.authorized_scopes())
        if missing:
            raise SpotifyError(
                f"Spotify authorization is missing {', '.join(missing)}; {action}"
            )

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None) -> dict | None:
        url = (self.config.get("api_root") or API_ROOT) + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.access_token()}",
                "Content-Type": "application/json",
            },
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(
                    request, timeout=float(self.config.get("timeout", 10))
                ) as response:
                    if response.status == 204:
                        return None
                    raw = response.read().strip()
                    if not raw:
                        return None
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        if method in {"POST", "PUT", "DELETE"}:
                            return None
                        raise SpotifyError("Spotify returned non-JSON data for a read request")
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt < 2:
                    retry_after = error.headers.get("Retry-After", "1")
                    try:
                        delay = min(30.0, max(0.0, float(retry_after)))
                    except ValueError:
                        delay = 1.0
                    error.close()
                    time.sleep(delay)
                    continue
                try:
                    detail = json.loads(error.read()).get("error") or {}
                except (OSError, json.JSONDecodeError, AttributeError, TypeError):
                    detail = {}
                reason = detail.get("reason")
                message = detail.get("message") or error.reason
                if error.code == 404 and reason == "NO_ACTIVE_DEVICE":
                    raise SpotifyError("no active Spotify device") from error
                raise SpotifyError(f"Spotify API error {error.code}: {message}") from error
            except urllib.error.URLError as error:
                raise SpotifyError(f"Spotify network error: {error.reason}") from error
        raise SpotifyError("Spotify API retry budget exhausted")

    def devices(self) -> list[dict]:
        return (self.request("GET", "/v1/me/player/devices") or {}).get("devices") or []

    @staticmethod
    def find_device(devices: list[dict], spec: str) -> dict | None:
        for device in devices:
            if device.get("id") == spec:
                return device
        folded = spec.casefold()
        exact = [
            device for device in devices
            if str(device.get("name") or "").casefold() == folded
        ]
        if len(exact) > 1:
            raise SpotifyError(f"multiple Spotify devices have the exact name {spec!r}; use an id")
        if exact:
            return exact[0]
        partial = [
            device for device in devices
            if folded in str(device.get("name") or "").casefold()
        ]
        if len(partial) > 1:
            names = ", ".join(str(device.get("name") or "unknown") for device in partial)
            raise SpotifyError(f"Spotify device {spec!r} is ambiguous: {names}")
        return partial[0] if partial else None

    def ready_device(self, spec: str | None = None) -> dict:
        devices = self.devices()
        if spec:
            device = self.find_device(devices, spec)
            if not device:
                raise SpotifyError(
                    f"Spotify device {spec!r} is unavailable; open Spotify there and retry"
                )
            return device
        for device in devices:
            if device.get("is_active"):
                return device
        names = ", ".join(str(device.get("name") or "unknown") for device in devices)
        detail = f"; visible but inactive: {names}" if names else "; no clients visible"
        raise SpotifyError(
            "no active Spotify device; open Spotify or pass an explicit device" + detail
        )

    def resolve(self, value: str) -> tuple[str, dict | None]:
        try:
            return spotify_resolver.resolve_uri(value, self.request)
        except spotify_resolver.SpotifyResolveError as error:
            raise SpotifyError(str(error)) from error

    def snapshot(self) -> dict | None:
        payload = self.request("GET", "/v1/me/player")
        if not payload or not payload.get("item"):
            return None
        track = payload["item"]
        device = payload.get("device") or {}
        return {
            "uri": track.get("uri"),
            "name": track.get("name"),
            "artists": [artist.get("name") for artist in track.get("artists") or []],
            "progress_ms": payload.get("progress_ms"),
            "duration_ms": track.get("duration_ms"),
            "is_playing": bool(payload.get("is_playing")),
            "repeat_state": payload.get("repeat_state"),
            "context_uri": (payload.get("context") or {}).get("uri"),
            "device": {
                "id": device.get("id"),
                "name": device.get("name"),
                "type": device.get("type"),
                "is_restricted": bool(device.get("is_restricted")),
                "supports_volume": device.get("supports_volume"),
                "volume_percent": device.get("volume_percent"),
            },
        }

    def play(self, value: str, device_spec: str | None = None) -> dict:
        uri, resolved = self.resolve(value)
        device = self.ready_device(device_spec)
        receipt = self.play_uri(uri, device)
        return {**receipt, "resolved": resolved}

    def play_uri(self, uri: str, device: dict) -> dict:
        device_id = device.get("id")
        if not device_id:
            raise SpotifyError("target Spotify device has no id")
        params = {"device_id": device["id"]}
        body = {"uris": [uri]} if uri.startswith("spotify:track:") else {"context_uri": uri}
        self.request("PUT", "/v1/me/player/play", params, body)
        return {"accepted": True, "uri": uri, "device": device}

    def set_repeat(self, mode: str, device_spec: str | None = None) -> dict:
        if mode not in {"off", "track", "context"}:
            raise SpotifyError(f"invalid repeat mode: {mode}")
        device = self.ready_device(device_spec)
        return self.set_repeat_on_device(mode, device)

    def set_repeat_on_device(self, mode: str, device: dict) -> dict:
        if mode not in {"off", "track", "context"}:
            raise SpotifyError(f"invalid repeat mode: {mode}")
        device_id = device.get("id")
        if not device_id:
            raise SpotifyError("target Spotify device has no id")
        self.request("PUT", "/v1/me/player/repeat", {
            "state": mode,
            "device_id": device_id,
        })
        return {"accepted": True, "mode": mode, "device": device}

    def pause(self, device_spec: str | None = None) -> dict:
        device = self.ready_device(device_spec)
        return self.pause_on_device(device)

    def pause_on_device(self, device: dict) -> dict:
        device_id = device.get("id")
        if not device_id:
            raise SpotifyError("target Spotify device has no id")
        self.request("PUT", "/v1/me/player/pause", {"device_id": device_id})
        return {"accepted": True, "device": device}

    def resume(self, device_spec: str | None = None) -> dict:
        device = self.ready_device(device_spec)
        device_id = device.get("id")
        if not device_id:
            raise SpotifyError("target Spotify device has no id")
        self.request("PUT", "/v1/me/player/play", {"device_id": device_id})
        return {"accepted": True, "device": device}

    def wait_for_paused(self, device_id: str, timeout: float = 5) -> dict:
        deadline = time.monotonic() + max(0.1, timeout)
        last = None
        while time.monotonic() < deadline:
            last = self.snapshot()
            if last and not last.get("is_playing") \
                    and (last.get("device") or {}).get("id") == device_id:
                return last
            time.sleep(0.25)
        raise SpotifyError(f"pause confirmation timed out on {device_id}")

    def wait_for_resumed(self, device_id: str, timeout: float = 5) -> dict:
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            snapshot = self.snapshot()
            if snapshot and snapshot.get("is_playing") \
                    and (snapshot.get("device") or {}).get("id") == device_id:
                return snapshot
            time.sleep(0.25)
        raise SpotifyError(f"resume confirmation timed out on {device_id}")

    def wait_for_repeat(self, mode: str, device_id: str, timeout: float = 5,
                        expected_uri: str | None = None) -> dict:
        deadline = time.monotonic() + max(0.1, timeout)
        last = None
        while time.monotonic() < deadline:
            last = self.snapshot()
            uri_matches = expected_uri is None or (last or {}).get("uri") == expected_uri
            if last and uri_matches and last.get("repeat_state") == mode \
                    and (last.get("device") or {}).get("id") == device_id:
                return last
            time.sleep(0.25)
        actual = (last or {}).get("repeat_state")
        raise SpotifyError(
            f"repeat confirmation timed out: wanted {mode} on {device_id}, got {actual}"
        )

    def wait_for_playback(self, uri: str, device_id: str, timeout: float = 15) -> dict:
        deadline = time.monotonic() + max(0.1, timeout)
        last = None
        while time.monotonic() < deadline:
            last = self.snapshot()
            if last and last.get("uri") == uri and last.get("is_playing") \
                    and (last.get("device") or {}).get("id") == device_id:
                return last
            time.sleep(0.5)
        actual_uri = (last or {}).get("uri")
        actual_device = ((last or {}).get("device") or {}).get("id")
        raise SpotifyError(
            f"playback confirmation timed out: wanted {uri} on {device_id}, "
            f"got {actual_uri} on {actual_device}"
        )

    def music_bed_probe(self, window: float = 1.2, min_advance_ms: int = 400,
                        expected_uri: str | None = None,
                        expected_device_id: str | None = None) -> dict:
        window = max(0.2, float(window))
        min_advance_ms = max(1, int(min_advance_ms))
        first = self.snapshot()
        if not first:
            return {"confirmed": False, "proof": "position_motion", "reason": "no_player"}
        if expected_uri is not None and first.get("uri") != expected_uri:
            return {
                "confirmed": False, "proof": "position_motion",
                "reason": "unexpected_track", "uri": first.get("uri"),
            }
        if expected_device_id is not None \
                and (first.get("device") or {}).get("id") != expected_device_id:
            return {
                "confirmed": False, "proof": "position_motion",
                "reason": "unexpected_device", "device": first.get("device"),
            }
        time.sleep(window)
        second = self.snapshot()
        common = {
            "proof": "position_motion",
            "window_ms": round(window * 1000),
            "uri": (second or first).get("uri"),
            "device": (second or first).get("device"),
        }
        if not second:
            return {**common, "confirmed": False, "reason": "player_disappeared"}
        if expected_uri is not None and second.get("uri") != expected_uri:
            return {**common, "confirmed": False, "reason": "unexpected_track"}
        if expected_device_id is not None \
                and (second.get("device") or {}).get("id") != expected_device_id:
            return {**common, "confirmed": False, "reason": "unexpected_device"}
        if not first.get("is_playing") or not second.get("is_playing"):
            return {**common, "confirmed": False, "reason": "paused"}
        if (first.get("device") or {}).get("id") != (second.get("device") or {}).get("id"):
            return {**common, "confirmed": False, "reason": "device_changed"}
        if first.get("uri") != second.get("uri"):
            return {**common, "confirmed": False, "reason": "track_changed"}
        try:
            advance = int(second.get("progress_ms")) - int(first.get("progress_ms"))
        except (TypeError, ValueError):
            return {**common, "confirmed": False, "reason": "position_missing"}
        receipt = {**common, "advance_ms": advance}
        if advance < min_advance_ms:
            return {**receipt, "confirmed": False, "reason": "position_stalled"}
        return {**receipt, "confirmed": True, "confirmed_at": time.time()}
