"""External service adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .local_mpv import LocalMPVClient
from .qqmusic import QQMusicClient
from .spotify import SpotifyClient, SpotifyError
from .spotify_local import MacOSSpotifyClient


def spotify_client(config: dict[str, Any], state_path: Path):
    adapter = config.get("adapter")
    if adapter == "web_api":
        return SpotifyClient(config, state_path)
    if adapter == "macos_applescript":
        return MacOSSpotifyClient(config, state_path)
    raise SpotifyError(f"unsupported Spotify adapter: {adapter}")


def music_client(config: dict[str, Any], state_path: Path):
    music = config.get("music") or {"backend": "spotify"}
    backend = music.get("backend", "spotify")
    if backend == "spotify":
        return spotify_client(config["spotify"], state_path)
    if backend == "local":
        qqmusic = config.get("qqmusic") or {}
        resolver = QQMusicClient(
            qqmusic,
            credential_path=state_path / "secrets" / "qqmusic-credential.json",
        ) if qqmusic.get("enabled") else None
        return LocalMPVClient(music, state_path, stream_resolver=resolver)
    raise SpotifyError(f"unsupported music backend: {backend}")
