"""macOS Spotify control through the installed app's AppleScript dictionary."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .spotify import SpotifyError

TRACK_URI = re.compile(r"^spotify:track:([A-Za-z0-9]+)$")
CURRENT_SESSION_ID = "spotify-current-session"
LOCAL_MAC_ID = "local_mac"


class MacOSSpotifyClient:
    """Control the current Spotify session without Web API credentials.

    AppleScript can control the session already selected in Spotify, but it
    cannot enumerate or choose a Connect target. ``device = local_mac`` is an
    operator assertion used only when speech and music must share this Mac.
    """

    def __init__(
        self,
        config: dict[str, Any],
        _state_path: Path,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.sleep = sleep
        self.monotonic = monotonic
        self.stability_window_seconds = float(config.get("stability_seconds", 8.0))
        self.ad_wait_seconds = float(config.get("ad_wait_seconds", 120.0))
        self._receipt_device: dict | None = None
        self.executable = shutil.which("osascript")
        if not self.executable:
            raise SpotifyError("macOS Spotify control needs osascript on PATH")

    def _run(self, language: str, source: str, *arguments: str) -> str:
        last = ""
        for attempt in range(2):
            try:
                result = subprocess.run(
                    [self.executable, "-l", language, "-e", source, *arguments],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                last = "timed out after 5 seconds"
                if attempt == 0:
                    self.sleep(0.5)
                    continue
                raise SpotifyError(f"Spotify AppleScript failed: {last}") from None
            if result.returncode == 0:
                return result.stdout.strip()
            last = result.stderr.strip()
            if attempt == 0:
                self.sleep(0.5)
        raise SpotifyError(f"Spotify AppleScript failed: {last or 'unknown error'}")

    def _jxa(self, source: str, *arguments: str) -> str:
        return self._run("JavaScript", source, *arguments)

    @staticmethod
    def _local_computer_name() -> str:
        executable = shutil.which("scutil") or "/usr/sbin/scutil"
        try:
            result = subprocess.run(
                [executable, "--get", "ComputerName"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SpotifyError(f"cannot read the local macOS ComputerName: {error}") from error
        name = result.stdout.strip()
        if result.returncode != 0 or not name:
            raise SpotifyError("cannot read the local macOS ComputerName with scutil")
        return name

    def resolve(self, value: str) -> tuple[str, dict]:
        raw = value.strip()
        match = TRACK_URI.fullmatch(raw)
        source = "spotify_uri"
        if not match:
            parsed = urllib.parse.urlparse(raw)
            path = parsed.path.strip("/").split("/")
            if parsed.scheme == "https" and parsed.hostname == "open.spotify.com" \
                    and len(path) == 2 and path[0] == "track" \
                    and re.fullmatch(r"[A-Za-z0-9]+", path[1]):
                raw = f"spotify:track:{path[1]}"
                match = TRACK_URI.fullmatch(raw)
                source = "open_spotify_url"
        if not match:
            raise SpotifyError(
                "macos_applescript needs an exact spotify:track URI or "
                "https://open.spotify.com/track URL; resolve title and artist first"
            )
        return raw, {"source": source, "track_id": match.group(1)}

    def snapshot(self) -> dict | None:
        raw = self._jxa(
            """
const s = Application('Spotify');
if (!s.running()) JSON.stringify({running: false});
else {
  const state = String(s.playerState());
  if (state === 'stopped') JSON.stringify({running: true, state});
  else {
    let t = null;
    try { t = s.currentTrack(); } catch (error) { t = null; }
    if (!t) JSON.stringify({
      running: true, state, is_playing: state === 'playing',
      content_type: 'interstitial', repeat_state: s.repeating() ? 'context' : 'off',
      volume_percent: Number(s.soundVolume())
    });
    else JSON.stringify({
      running: true, state, is_playing: state === 'playing', content_type: 'track',
      progress_ms: Math.round(Number(s.playerPosition()) * 1000),
      repeat_state: s.repeating() ? 'context' : 'off', uri: String(t.spotifyUrl()),
      name: String(t.name()), artists: [String(t.artist())],
      album: String(t.album()), album_artist: String(t.albumArtist()),
      duration_ms: Number(t.duration()), volume_percent: Number(s.soundVolume())
    });
  }
}
"""
        )
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise SpotifyError("Spotify AppleScript returned invalid playback data") from error
        if not payload.get("running") or payload.get("state") == "stopped":
            return None
        payload["device"] = self._receipt_device or self.ready_device(
            self.config.get("device"), require_snapshot=False
        )
        return payload

    def ready_device(self, spec: str | None = None, *, require_snapshot: bool = True) -> dict:
        value = str(spec or "current").strip()
        if value.casefold() in {"current", "current_session", CURRENT_SESSION_ID.casefold()}:
            device = {
                "id": CURRENT_SESSION_ID,
                "name": "Spotify current session",
                "type": "Unknown",
                "is_active": True,
                "selectable": False,
                "routing_proof": "not_available_in_applescript",
            }
        elif value.casefold() == LOCAL_MAC_ID:
            device = {
                "id": LOCAL_MAC_ID,
                "name": self._local_computer_name(),
                "type": "Computer",
                "is_active": True,
                "selectable": False,
                "routing_proof": "operator_asserted_local_mac",
            }
        else:
            raise SpotifyError(
                "macos_applescript cannot select a named Connect device; use current, "
                "or select the target in Spotify first"
            )
        if require_snapshot:
            self._jxa("const s=Application('Spotify'); JSON.stringify({running:s.running()});")
            self._receipt_device = device
        return device

    def devices(self) -> list[dict]:
        return [self.ready_device(self.config.get("device"))]

    def play_uri(self, uri: str, device: dict) -> dict:
        uri, _ = self.resolve(uri)
        self._jxa(
            "function run(argv) { const s=Application('Spotify'); "
            "s.playTrack(argv[0]); return 'accepted'; }",
            uri,
        )
        return {
            "accepted": True,
            "uri": uri,
            "device": device,
            "transport": "macos_applescript",
        }

    def set_repeat_on_device(self, mode: str, device: dict) -> dict:
        if mode not in {"off", "context"}:
            raise SpotifyError("macos_applescript supports repeat off or context, not track")
        enabled = "true" if mode == "context" else "false"
        self._jxa(f"const s=Application('Spotify'); s.repeating={enabled}; 'accepted';")
        return {"accepted": True, "mode": mode, "device": device}

    def pause_on_device(self, device: dict) -> dict:
        self._jxa("const s=Application('Spotify'); s.pause(); 'accepted';")
        return {"accepted": True, "device": device}

    def pause(self, device_spec: str | None = None) -> dict:
        return self.pause_on_device(self.ready_device(device_spec))

    def resume(self, device_spec: str | None = None) -> dict:
        device = self.ready_device(device_spec)
        self._jxa("const s=Application('Spotify'); s.play(); 'accepted';")
        return {"accepted": True, "device": device}

    def wait_for_playback(self, uri: str, device_id: str, timeout: float = 15) -> dict:
        deadline = self.monotonic() + max(0.1, timeout)
        last = None
        interstitial_observed = False
        while self.monotonic() < deadline:
            last = self.snapshot()
            if last and last.get("content_type") == "interstitial" \
                    and last.get("is_playing"):
                if not interstitial_observed:
                    deadline = max(deadline, self.monotonic() + self.ad_wait_seconds)
                interstitial_observed = True
            if last and last.get("uri") == uri and last.get("is_playing") \
                    and (last.get("device") or {}).get("id") == device_id:
                return {**last, "interstitial_observed": interstitial_observed}
            self.sleep(0.5)
        raise SpotifyError(
            f"local playback confirmation timed out: wanted {uri}, "
            f"got {(last or {}).get('uri')}"
        )

    def wait_for_paused(self, device_id: str, timeout: float = 5) -> dict:
        deadline = self.monotonic() + max(0.1, timeout)
        last = None
        while self.monotonic() < deadline:
            last = self.snapshot()
            if last and not last.get("is_playing") \
                    and (last.get("device") or {}).get("id") == device_id:
                return last
            self.sleep(0.25)
        raise SpotifyError(f"local pause confirmation timed out on {device_id}")

    def wait_for_resumed(self, device_id: str, timeout: float = 5) -> dict:
        deadline = self.monotonic() + max(0.1, timeout)
        while self.monotonic() < deadline:
            last = self.snapshot()
            if last and last.get("is_playing") \
                    and (last.get("device") or {}).get("id") == device_id:
                return last
            self.sleep(0.25)
        raise SpotifyError(f"local resume confirmation timed out on {device_id}")

    def wait_for_repeat(
        self,
        mode: str,
        device_id: str,
        timeout: float = 5,
        expected_uri: str | None = None,
    ) -> dict:
        deadline = self.monotonic() + max(0.1, timeout)
        last = None
        while self.monotonic() < deadline:
            last = self.snapshot()
            if last and last.get("repeat_state") == mode \
                    and (expected_uri is None or last.get("uri") == expected_uri) \
                    and (last.get("device") or {}).get("id") == device_id:
                return last
            self.sleep(0.25)
        raise SpotifyError(
            f"local repeat confirmation timed out: wanted {mode}, "
            f"got {(last or {}).get('repeat_state')}"
        )

    def music_bed_probe(
        self,
        window: float = 1.2,
        min_advance_ms: int = 400,
        expected_uri: str | None = None,
        expected_device_id: str | None = None,
    ) -> dict:
        window = max(float(window), self.stability_window_seconds)
        first = self.snapshot()
        if not first:
            return {"confirmed": False, "proof": "stable_position_motion", "reason": "no_player"}
        self.sleep(window)
        second = self.snapshot()
        common = {
            "proof": "stable_position_motion",
            "window_ms": round(window * 1000),
            "uri": (second or first).get("uri"),
            "device": (second or first).get("device"),
            "actual_track": {
                key: (second or first).get(key)
                for key in (
                    "uri", "name", "artists", "album", "album_artist",
                    "progress_ms", "duration_ms", "content_type",
                )
            },
        }
        if not second:
            return {**common, "confirmed": False, "reason": "player_disappeared"}
        if any(
            snapshot.get("content_type") == "interstitial"
            for snapshot in (first, second)
        ):
            return {**common, "confirmed": False, "reason": "ad_break"}
        if expected_uri is not None and (
            first.get("uri") != expected_uri or second.get("uri") != expected_uri
        ):
            return {**common, "confirmed": False, "reason": "unexpected_track"}
        if expected_device_id is not None and any(
            (snapshot.get("device") or {}).get("id") != expected_device_id
            for snapshot in (first, second)
        ):
            return {**common, "confirmed": False, "reason": "unexpected_session"}
        if not first.get("is_playing") or not second.get("is_playing"):
            return {**common, "confirmed": False, "reason": "paused"}
        if first.get("uri") != second.get("uri"):
            return {**common, "confirmed": False, "reason": "track_changed"}
        try:
            advance = int(second.get("progress_ms")) - int(first.get("progress_ms"))
        except (TypeError, ValueError):
            return {**common, "confirmed": False, "reason": "position_missing"}
        receipt = {**common, "advance_ms": advance, "identity_match": "exact_uri"}
        if advance < max(1, int(min_advance_ms)):
            return {**receipt, "confirmed": False, "reason": "position_stalled"}
        return {**receipt, "confirmed": True, "confirmed_at": time.time()}
