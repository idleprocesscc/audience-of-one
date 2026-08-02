"""Pre-rendered wildcard liners for an honestly superseded programme request."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from . import tts


class WildcardError(RuntimeError):
    pass


def build(data: dict, state_path: Path) -> list[dict]:
    """Render and atomically publish one complete liner generation."""
    try:
        lines = data["improv"]["lines"]
    except (KeyError, TypeError) as error:
        raise WildcardError("configuration has no [improv] liner bank") from error
    root = state_path / "wildcards"
    generations = root / "generations"
    generations.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    pending = root / f".generation-{name}.pending"
    pending.mkdir(mode=0o700)
    published = generations / name
    link_pending = root / f".current-{name}.pending"
    receipts = []
    paths = [pending / f"liner-{index}.mp3" for index in range(1, len(lines) + 1)]
    manifest = pending / "manifest.json"
    try:
        entries = []
        for index, (line, path) in enumerate(zip(lines, paths), 1):
            provider = tts.synthesize(data, line, path)
            duration = tts.verify_audio(path)
            path.chmod(0o600)
            entry = {
                "id": f"liner-{index}",
                "audio": path.name,
                "provider": provider,
                "duration_seconds": round(duration, 3),
                "bytes": path.stat().st_size,
            }
            entries.append(entry)
            receipts.append(dict(entry))
        manifest.write_text(
            json.dumps({"version": 1, "liners": entries}, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest.chmod(0o600)
        os.replace(pending, published)
        link_pending.symlink_to(Path("generations") / name, target_is_directory=True)
        os.replace(link_pending, root / "current")
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        link_pending.unlink(missing_ok=True)
        try:
            pending.rmdir()
        except FileNotFoundError:
            pass
    return receipts


def _fraction(seed: str) -> float:
    value = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big")
    return value / 2**64


def choose(
    data: dict,
    state_path: Path,
    attempt_id: str,
    *,
    previous_id: str | None = None,
) -> dict:
    """Make one retry-stable chance decision without claiming playback."""
    try:
        config = data["improv"]
    except (KeyError, TypeError) as error:
        raise WildcardError("configuration has no [improv] policy") from error
    chance = float(config["liner_chance"])
    roll = _fraction(f"{attempt_id}:chance")
    base = {
        "enabled": bool(config["enabled"]),
        "chance": chance,
        "roll": round(roll, 8),
        "attempt_id": attempt_id,
        "played": False,
    }
    if not config["enabled"]:
        return {**base, "selected": False, "reason": "disabled"}
    if roll >= chance:
        return {**base, "selected": False, "reason": "probability"}

    current = state_path / "wildcards" / "current"
    try:
        manifest = json.loads((current / "manifest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
        raise WildcardError("wildcard liners are not built") from error
    entries = manifest.get("liners")
    if not isinstance(entries, list) or not entries:
        raise WildcardError("wildcard liner manifest is empty")
    usable = [
        entry for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and isinstance(entry.get("audio"), str)
        and (current / Path(entry["audio"]).name).is_file()
    ]
    if not usable:
        raise WildcardError("wildcard liner generation has no playable audio")
    alternatives = [entry for entry in usable if entry["id"] != previous_id]
    if alternatives:
        usable = alternatives
    index_roll = _fraction(f"{attempt_id}:liner")
    selected = usable[min(len(usable) - 1, int(index_roll * len(usable)))]
    audio = current / Path(selected["audio"]).name
    return {
        **base,
        "selected": True,
        "reason": "selected",
        "liner_id": selected["id"],
        "audio": str(audio),
        "duration_seconds": selected.get("duration_seconds"),
    }


def last_played_id(state_path: Path) -> str | None:
    try:
        data = json.loads(
            (state_path / "wildcards" / "last-played.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    value = data.get("liner_id") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def mark_played(state_path: Path, liner_id: str, remote_id: str) -> None:
    """Remember only a phone-confirmed liner, never a mere selection."""
    root = state_path / "wildcards"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = root / "last-played.json"
    temporary = root / f".last-played-{os.getpid()}.tmp"
    try:
        temporary.write_text(json.dumps({
            "liner_id": liner_id,
            "remote_id": remote_id,
            "played_at": time.time(),
        }, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
