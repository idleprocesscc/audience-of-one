"""Pluggable TTS providers with no secret values in configuration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class TTSError(RuntimeError):
    pass


def is_cjk(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text)
    cjk = sum(
        1 for char in normalized
        if ("\u3400" <= char <= "\u9fff")
        or ("\u3040" <= char <= "\u30ff")
        or ("\uac00" <= char <= "\ud7af")
    )
    return bool(normalized) and cjk / len(normalized) > 0.3


def _secret(provider: dict[str, Any]) -> str:
    env_name = provider.get("api_key_env")
    if not isinstance(env_name, str) or not env_name:
        raise TTSError("provider api_key_env must name an environment variable")
    value = os.environ.get(env_name)
    if not value:
        raise TTSError(f"environment variable {env_name} is not set")
    return value


def _voice_id(provider: dict[str, Any]) -> str:
    value = provider.get("voice_id")
    if not isinstance(value, str) or not value:
        raise TTSError("provider voice_id is not configured")
    return value


def _write_atomic(output: Path, audio: bytes) -> None:
    if not audio:
        raise TTSError("provider returned empty audio")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(audio)
        temporary.chmod(0o600)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _minimax(text: str, output: Path, provider: dict[str, Any]) -> None:
    payload = json.dumps({
        "model": provider.get("model") or "speech-2.8-hd",
        "text": text,
        "stream": False,
        "language_boost": "auto",
        "output_format": "hex",
        "voice_setting": {
            "voice_id": _voice_id(provider),
            "speed": float(provider.get("speed", 1.0)),
            "vol": float(provider.get("volume", 1.0)),
            "pitch": int(provider.get("pitch", 0)),
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
    }).encode()
    request = urllib.request.Request(
        provider.get("endpoint") or "https://api.minimax.io/v1/t2a_v2",
        data=payload,
        headers={
            "Authorization": f"Bearer {_secret(provider)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(provider.get("timeout", 30))) as response:
            body = json.loads(response.read())
        status = (body.get("base_resp") or {}).get("status_code")
        audio_hex = (body.get("data") or {}).get("audio")
        if status not in (None, 0) or not isinstance(audio_hex, str):
            raise TTSError(f"MiniMax rejected synthesis: status={status}")
        _write_atomic(output, bytes.fromhex(audio_hex))
    except TTSError:
        raise
    except Exception as error:
        raise TTSError(f"MiniMax synthesis failed: {error}") from error


def _elevenlabs(text: str, output: Path, provider: dict[str, Any]) -> None:
    voice_id = urllib.parse.quote(_voice_id(provider), safe="")
    endpoint = provider.get("endpoint") or "https://api.elevenlabs.io/v1/text-to-speech"
    output_format = provider.get("output_format") or "mp3_44100_128"
    payload = json.dumps({
        "text": text,
        "model_id": provider.get("model") or "eleven_multilingual_v2",
        "voice_settings": {
            "stability": float(provider.get("stability", 0.5)),
            "similarity_boost": float(provider.get("similarity_boost", 0.75)),
        },
    }).encode()
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/{voice_id}?{urllib.parse.urlencode({'output_format': output_format})}",
        data=payload,
        headers={"xi-api-key": _secret(provider), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(provider.get("timeout", 30))) as response:
            _write_atomic(output, response.read())
    except TTSError:
        raise
    except Exception as error:
        raise TTSError(f"ElevenLabs synthesis failed: {error}") from error


def _macos(text: str, output: Path, provider: dict[str, Any]) -> None:
    say = shutil.which("say")
    ffmpeg = shutil.which("ffmpeg")
    if not say or not ffmpeg:
        raise TTSError("macOS provider needs both say and ffmpeg on PATH")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="station-say-", dir=output.parent) as raw:
        aiff = Path(raw) / "voice.aiff"
        encoded = Path(raw) / "voice.mp3"
        try:
            say_result = subprocess.run(
                [say, "-o", str(aiff), "--", text], capture_output=True, text=True,
                timeout=float(provider.get("timeout", 30)), check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TTSError("macOS say timed out") from error
        if say_result.returncode != 0:
            raise TTSError(f"macOS say failed: {say_result.stderr.strip()}")
        try:
            convert = subprocess.run(
                [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(aiff),
                 "-f", "mp3", str(encoded)],
                capture_output=True, text=True, timeout=float(provider.get("timeout", 30)),
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TTSError("ffmpeg conversion timed out") from error
        if convert.returncode != 0 or not encoded.is_file():
            raise TTSError(f"ffmpeg conversion failed: {convert.stderr.strip()}")
        encoded.chmod(0o600)
        os.replace(encoded, output)
        output.chmod(0o600)


def synthesize(data: dict[str, Any], text: str, output: Path) -> str:
    tts = data["tts"]
    provider_name = tts["chinese" if is_cjk(text) else "english"]
    provider = tts["providers"][provider_name]
    provider_type = provider["type"]
    if provider_type == "minimax":
        _minimax(text, output, provider)
    elif provider_type == "elevenlabs":
        _elevenlabs(text, output, provider)
    elif provider_type == "macos":
        _macos(text, output, provider)
    else:
        raise TTSError(f"unsupported provider type: {provider_type}")
    return provider_name


def verify_audio(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise TTSError("ffprobe is required to verify generated audio")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise TTSError(f"audio verification timed out for {path.name}") from error
    try:
        duration = float(result.stdout.strip())
    except ValueError as error:
        raise TTSError(f"audio verification failed for {path.name}") from error
    if result.returncode != 0 or duration <= 0:
        raise TTSError(f"audio verification failed for {path.name}")
    return duration
