"""Transactional recovery-line generation."""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

from . import tts


def build(data: dict, state_path: Path) -> list[dict]:
    lines = data["recovery"]["lines"]
    cover_dir = state_path / "covers"
    cover_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    generations = cover_dir / "generations"
    generations.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    pending = cover_dir / f".generation-{name}.pending"
    pending.mkdir(mode=0o700)
    temporary = [pending / f"cover-{index}.mp3" for index in (1, 2)]
    published = generations / name
    link_pending = cover_dir / f".current-{name}.pending"
    receipts = []
    try:
        for line, path in zip(lines, temporary):
            provider = tts.synthesize(data, line, path)
            duration = tts.verify_audio(path)
            receipts.append({
                "provider": provider,
                "duration_seconds": round(duration, 3),
                "bytes": path.stat().st_size,
            })
            path.chmod(0o600)
        os.replace(pending, published)
        link_pending.symlink_to(Path("generations") / name, target_is_directory=True)
        os.replace(link_pending, cover_dir / "current")
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
        link_pending.unlink(missing_ok=True)
        try:
            pending.rmdir()
        except FileNotFoundError:
            pass
    return receipts
