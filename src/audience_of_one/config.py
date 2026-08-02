"""Configuration schema and validation without secret material."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = """# Audience of One
# Secrets are read from environment variables named below. Do not paste values here.

[station]
mode = "desktop"

[scheduler]
poll_seconds = 1.0
prepare_window_seconds = 75.0
boundary_lead_seconds = 1.0

[music]
backend = "spotify"

[desktop]
local_device_name = ""
duck_percent = 35
fade_down_seconds = 0.8
fade_up_seconds = 1.0
voice_start_settle_seconds = 0.12
voice_timeout_margin_seconds = 10.0
intro_delay_seconds = 0.25
blackout_gap_seconds = 0.5

[spotify]
adapter = "web_api"
device = ""
client_id_env = "SPOTIFY_CLIENT_ID"
redirect_uri = "http://127.0.0.1:8899/callback"
primary_playlist = ""
weekly_playlist = ""

[qqmusic]
enabled = false
base_url = "http://127.0.0.1:8080"
timeout_seconds = 8.0
retries = 2

[tts]
chinese = "minimax"
english = "elevenlabs"

[tts.providers.minimax]
type = "minimax"
api_key_env = "MINIMAX_API_KEY"
voice_id = ""
model = "speech-2.8-hd"

[tts.providers.elevenlabs]
type = "elevenlabs"
api_key_env = "ELEVENLABS_API_KEY"
voice_id = ""
model = "eleven_multilingual_v2"

[tts.providers.macos]
type = "macos"

[recovery]
lines = [
  "The signal took the scenic route. The music will be right back.",
  "A brief pause while the station finds its footing.",
]

[improv]
enabled = false
liner_chance = 0.25
lines = [
  "The record took a turn of its own. Let's hear where it goes.",
  "That was not the door I reached for, but something came through it.",
  "It seems the machine wanted a song too.",
]

[android]
enabled = false
transport = "lan"
device = ""
mcp_url_env = "STATION_PHONE_MCP_URL"
mcp_token_env = "STATION_PHONE_MCP_TOKEN"
location_id = ""
path_prefix = ""
voice_inbox = "station-inbox"
music_inbox = "station-music-inbox"
ack_dir = "station-acks"
health_path = "phone-health.json"
voice_start_timeout_seconds = 30.0
voice_finish_margin_seconds = 20.0
music_start_timeout_seconds = 30.0
"""


class ConfigError(ValueError):
    pass


def load(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as error:
        raise ConfigError(f"configuration not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {path}: {error}") from error


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"missing [{name}] table")
    return value


def validate(data: dict[str, Any]) -> list[str]:
    station = _table(data, "station")
    music = data.get("music") or {"backend": "spotify"}
    if not isinstance(music, dict):
        raise ConfigError("[music] must be a table")
    backend = music.get("backend", "spotify")
    if backend not in {"spotify", "local"}:
        raise ConfigError("music.backend must be spotify or local")
    spotify = _table(data, "spotify") if backend == "spotify" else data.get("spotify", {})
    if not isinstance(spotify, dict):
        raise ConfigError("[spotify] must be a table")
    qqmusic = data.get("qqmusic", {})
    if not isinstance(qqmusic, dict):
        raise ConfigError("[qqmusic] must be a table")
    tts = _table(data, "tts")
    android = _table(data, "android")
    recovery = _table(data, "recovery")
    improv = data.get("improv")
    if improv is not None and not isinstance(improv, dict):
        raise ConfigError("[improv] must be a table")

    desktop = data.get("desktop", {})
    if not isinstance(desktop, dict):
        raise ConfigError("[desktop] must be a table")
    scheduler = data.get("scheduler", {})
    if not isinstance(scheduler, dict):
        raise ConfigError("[scheduler] must be a table")
    for field in ("poll_seconds", "prepare_window_seconds", "boundary_lead_seconds"):
        value = scheduler.get(field)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0
        ):
            raise ConfigError(f"scheduler.{field} must be a positive number")

    mode = station.get("mode")
    if mode != "desktop":
        raise ConfigError(
            "station.mode must be desktop; Android is an output route, not a host mode"
        )

    adapter = spotify.get("adapter") if backend == "spotify" else None
    if backend == "spotify" and adapter not in {"macos_applescript", "web_api"}:
        raise ConfigError("spotify.adapter must be macos_applescript or web_api")
    if backend == "local":
        library_root = music.get("library_root")
        if not isinstance(library_root, str) or not library_root.strip():
            raise ConfigError("music.library_root must be a non-empty path")
        device = music.get("device", "local_mac")
        if not isinstance(device, str):
            raise ConfigError("music.device must be a string")
    stability = (
        spotify.get("stability_seconds", 8.0) if backend == "spotify"
        else music.get("stability_seconds", 1.2)
    )
    if not isinstance(stability, (int, float)) or isinstance(stability, bool) or stability < 0:
        raise ConfigError(f"{backend} stability_seconds must be a non-negative number")
    if backend == "spotify":
        ad_wait = spotify.get("ad_wait_seconds", 120.0)
        if not isinstance(ad_wait, (int, float)) or isinstance(ad_wait, bool) or ad_wait <= 0:
            raise ConfigError("spotify.ad_wait_seconds must be a positive number")
    for field in ("primary_playlist", "weekly_playlist"):
        value = spotify.get(field, "")
        if not isinstance(value, str):
            raise ConfigError(f"spotify.{field} must be a string")
    if qqmusic:
        if not isinstance(qqmusic.get("enabled", False), bool):
            raise ConfigError("qqmusic.enabled must be true or false")
        base_url = qqmusic.get("base_url", "http://127.0.0.1:8080")
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ConfigError("qqmusic.base_url must be an http(s) URL")
        timeout = qqmusic.get("timeout_seconds", 8.0)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ConfigError("qqmusic.timeout_seconds must be positive")
        retries = qqmusic.get("retries", 2)
        if not isinstance(retries, int) or isinstance(retries, bool) or not 0 <= retries <= 5:
            raise ConfigError("qqmusic.retries must be an integer from 0 to 5")

    providers = tts.get("providers")
    if not isinstance(providers, dict):
        raise ConfigError("missing [tts.providers] tables")
    for language in ("chinese", "english"):
        provider = tts.get(language)
        if provider not in providers:
            raise ConfigError(f"tts.{language} names unknown provider: {provider}")
    for provider_name, provider in providers.items():
        if not isinstance(provider, dict):
            raise ConfigError(f"tts.providers.{provider_name} must be a table")
        if provider.get("type") not in {"minimax", "elevenlabs", "macos"}:
            raise ConfigError(
                f"tts.providers.{provider_name}.type must be minimax, elevenlabs, or macos"
            )

    lines = recovery.get("lines")
    if not isinstance(lines, list) or len(lines) != 2 or not all(
        isinstance(line, str) and line.strip() for line in lines
    ):
        raise ConfigError("recovery.lines must contain exactly two non-empty strings")

    if improv is not None:
        enabled = improv.get("enabled")
        if not isinstance(enabled, bool):
            raise ConfigError("improv.enabled must be true or false")
        chance = improv.get("liner_chance")
        if not isinstance(chance, (int, float)) or isinstance(chance, bool) \
                or not 0 <= chance <= 1:
            raise ConfigError("improv.liner_chance must be from 0 to 1")
        liner_lines = improv.get("lines")
        if not isinstance(liner_lines, list) or len(liner_lines) < 2 or not all(
            isinstance(line, str) and line.strip() for line in liner_lines
        ):
            raise ConfigError("improv.lines must contain at least two non-empty strings")

    transport = android.get("transport")
    if transport not in {"", "lan", "tailscale", "cloudflared"}:
        raise ConfigError("android.transport must be lan, tailscale, or cloudflared")
    for field in (
        "device", "mcp_url_env", "mcp_token_env", "location_id", "path_prefix",
        "voice_inbox", "music_inbox", "ack_dir",
        "health_path",
    ):
        value = android.get(field, "")
        if not isinstance(value, str):
            raise ConfigError(f"android.{field} must be a string")
    for field in (
        "mcp_timeout_seconds", "voice_start_timeout_seconds",
        "voice_finish_margin_seconds", "music_start_timeout_seconds",
    ):
        value = android.get(field)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0
        ):
            raise ConfigError(f"android.{field} must be a positive number")

    duck = desktop.get("duck_percent", 35)
    if not isinstance(duck, int) or isinstance(duck, bool) or not 0 <= duck <= 100:
        raise ConfigError("desktop.duck_percent must be an integer from 0 to 100")
    local_device_name = desktop.get("local_device_name", "")
    if not isinstance(local_device_name, str):
        raise ConfigError("desktop.local_device_name must be a string")
    for field in (
        "fade_down_seconds", "fade_up_seconds", "voice_start_settle_seconds",
        "voice_timeout_margin_seconds", "intro_delay_seconds", "blackout_gap_seconds",
        "dark_gap_seconds",
    ):
        value = desktop.get(field)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0
        ):
            raise ConfigError(f"desktop.{field} must be a non-negative number")

    actions: list[str] = []
    if backend == "spotify" and adapter == "web_api":
        for field in ("client_id_env",):
            env_name = spotify.get(field)
            if not isinstance(env_name, str) or not env_name:
                raise ConfigError(f"spotify.{field} must name an environment variable")
            if not os.environ.get(env_name):
                actions.append(f"set environment variable {env_name}")
    for provider_name in {tts["chinese"], tts["english"]}:
        provider = providers[provider_name]
        env_name = provider.get("api_key_env")
        if env_name and not os.environ.get(env_name):
            actions.append(f"set environment variable {env_name}")
        if provider.get("type") in {"minimax", "elevenlabs"} and not provider.get("voice_id"):
            actions.append(f"set tts.providers.{provider_name}.voice_id")
    if mode == "android" and not android.get("enabled"):
        actions.append("set android.enabled = true")
    if android.get("enabled"):
        if not android.get("transport"):
            actions.append(
                "choose android.transport: tailscale when no other VPN must remain, "
                "or cloudflared when one must; lan is fallback"
            )
        if not android.get("location_id"):
            actions.append("set android.location_id")
        for field, fallback in (
            ("mcp_url_env", "STATION_PHONE_MCP_URL"),
            ("mcp_token_env", "STATION_PHONE_MCP_TOKEN"),
        ):
            env_name = str(android.get(field) or fallback)
            if not os.environ.get(env_name):
                actions.append(f"set environment variable {env_name}")
    return actions


def initialize(path: Path, state_path: Path, force: bool = False) -> None:
    if path.exists() and not force:
        raise ConfigError(f"configuration already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    path.chmod(0o600)
