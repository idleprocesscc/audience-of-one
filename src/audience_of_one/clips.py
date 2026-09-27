"""Recorded station clips: idents and short musical echoes carried on the voice channel.

A clip is a file the host prepared ahead of time — a station ident, a few bars
answering the record that just ended. It never replaces the music backend: at
preparation time it is rendered, alone or under the synthesized line, into the
same single voice file every desktop and phone voice path already plays, ducks,
and receipts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

SUPPORTED_SUFFIXES = {".wav", ".aif", ".aiff", ".caf", ".flac", ".m4a", ".mp3"}
MAX_VOICE_AT_SECONDS = 600.0


class ClipError(RuntimeError):
    pass


def clips_root(config: dict[str, Any]) -> Path:
    raw = str((config.get("clips") or {}).get("root") or "").strip()
    if not raw:
        raise ClipError("clips.root is not configured")
    return Path(raw).expanduser().resolve()


def resolve(config: dict[str, Any], name: str) -> Path:
    """Return one clip file confined below clips.root."""
    root = clips_root(config)
    raw = str(name or "").strip()
    relative = PurePosixPath(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise ClipError("a clip must be a relative path below clips.root")
    path = (root / Path(*relative.parts)).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ClipError("clip escaped clips.root") from error
    if path.suffix.casefold() not in SUPPORTED_SUFFIXES:
        raise ClipError(f"unsupported clip audio type: {path.suffix or 'none'}")
    if not path.is_file():
        raise ClipError(f"clip does not exist: {relative.as_posix()}")
    return path


def render(clip: Path, voice: Path | None, voice_at: float, output: Path) -> None:
    """Render the clip, with an optional voice entering at voice_at seconds, to mp3.

    Levels are the host's own: the clip is mastered to sit under the station voice,
    so nothing is normalized; a limiter only keeps an unlucky sum below full scale.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ClipError("ffmpeg is required to render station clips")
    stereo = "aresample=44100,aformat=channel_layouts=stereo"
    command = [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(clip)]
    if voice is None:
        graph = f"[0:a]{stereo}[out]"
    else:
        if not 0 <= voice_at <= MAX_VOICE_AT_SECONDS:
            raise ClipError("clip voice offset must be from 0 to 600 seconds")
        delay = round(voice_at * 1000)
        command += ["-i", str(voice)]
        graph = (
            f"[0:a]{stereo}[clip];"
            f"[1:a]{stereo},adelay={delay}|{delay}[voice];"
            "[clip][voice]amix=inputs=2:duration=longest:normalize=0,"
            "alimiter=limit=0.97:level=false[out]"
        )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    command += [
        "-filter_complex", graph, "-map", "[out]",
        "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", str(temporary),
    ]
    try:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=60, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ClipError("clip render timed out") from error
        if result.returncode != 0 or not temporary.is_file():
            raise ClipError(f"clip render failed: {result.stderr.strip()}")
        temporary.chmod(0o600)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
