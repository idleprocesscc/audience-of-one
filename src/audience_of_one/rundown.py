"""Mutable rundown stored independently from the broadcast engine."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .provenance import PROVENANCE
from .transactions import Journal, TransactionError

TRANSITIONS = {"overlap", "clean", "tail", "intro", "blackout", "hard"}
LEGACY_TRANSITIONS = {"dark": "blackout"}
AFTER_MODES = {"autoplay", "repeat", "stop"}


class RundownError(RuntimeError):
    pass


def normalize_transition(value: str) -> str:
    return LEGACY_TRANSITIONS.get(value, value)


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _queue_dir(state_path: Path) -> Path:
    return state_path / "queue"


def items(state_path: Path) -> list[dict]:
    result = []
    for path in sorted(_queue_dir(state_path).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            order = float(data.get("order", int(path.stem)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            data = None
            order = float("inf")
        result.append({
            "id": path.stem,
            "filename": path.name,
            "path": path,
            "order": order,
            "data": data,
        })
    return sorted(result, key=lambda item: (item["order"], item["filename"]))


def get(state_path: Path, identifier: str) -> dict:
    identifier = identifier.removesuffix(".json")
    if not identifier.isdigit():
        raise RundownError(f"invalid item id: {identifier}")
    filename = f"{identifier}.json"
    path = _queue_dir(state_path) / filename
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RundownError(f"queued item not found: {identifier}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise RundownError(f"cannot read queued item {identifier}: {error}") from error
    if not isinstance(data, dict):
        raise RundownError(f"queued item {identifier} is not an object")
    return {"id": identifier, "filename": filename, "path": path, "data": data}


def _next_order(state_path: Path) -> int:
    finite = [item["order"] for item in items(state_path) if item["order"] != float("inf")]
    return int(max(finite) if finite else 0) + 1000


def append(state_path: Path, *, track: str | None = None, say: str | None = None,
           lang: str | None = None, transition: str = "overlap",
           after: str = "autoplay", phone: bool = False,
           device: str | None = None, duck: bool = False) -> dict:
    if not track and not say:
        raise RundownError("a programme item needs a track, a voice line, or both")
    transition = normalize_transition(transition)
    if transition not in TRANSITIONS:
        raise RundownError(f"unknown transition: {transition}")
    if after not in AFTER_MODES:
        raise RundownError(f"unknown after mode: {after}")
    if after != "autoplay" and not track:
        raise RundownError("repeat/stop after modes require a track")

    queue_dir = _queue_dir(state_path)
    queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    identifier = int(time.time() * 1000)
    path = queue_dir / f"{identifier:015d}.json"
    # A same-millisecond append after an archive would reuse a filename the
    # played archive already holds, and archive_played would overwrite that
    # earlier evidence; the identifier must be unique across both directories.
    while path.exists() or (state_path / "played" / path.name).exists():
        identifier += 1
        path = queue_dir / f"{identifier:015d}.json"
    data = {
        "version": 1,
        "provenance": PROVENANCE,
        "order": _next_order(state_path),
        "created_at": time.time(),
        "revision": 1,
        "transition": transition,
    }
    if track:
        data["track"] = track
    if say:
        data["say"] = say
    if lang:
        data["lang"] = lang
    if after != "autoplay":
        data["after"] = after
    if phone:
        data["output"] = "phone"
    if device:
        data["device"] = device
    if duck:
        data["duck"] = True
    _atomic_write(path, data)
    Journal(state_path).ensure(path.name)
    return {"id": path.stem, "filename": path.name, "data": data}


def play_history(state_path: Path) -> dict[str, dict]:
    """Aggregate the played archive into per-track evidence.

    Returns a map from the receipted track URI (spotify:track:..., local:...,
    qqmusic:MID) to {"play_count", "last_played_at"} with an epoch timestamp.
    """
    journal = Journal(state_path)
    plays: dict[str, dict] = {}
    for path in sorted((state_path / "played").glob("*.json")):
        try:
            programme = json.loads(path.read_text(encoding="utf-8"))
            transaction = journal.load(path.name)
        except (OSError, json.JSONDecodeError, TransactionError):
            continue
        if not isinstance(programme, dict) or not programme.get("track"):
            continue
        transaction = transaction or {}
        uri = str((transaction.get("track") or {}).get("uri") or programme["track"])
        played_at = max(
            (float(entry.get("at") or 0.0)
             for entry in transaction.get("history") or []
             if entry.get("state") == "played"),
            default=float(
                transaction.get("updated_at") or programme.get("created_at") or 0.0
            ),
        )
        record = plays.setdefault(uri, {"play_count": 0, "last_played_at": 0.0})
        record["play_count"] += 1
        record["last_played_at"] = max(record["last_played_at"], played_at)
    return plays


def archive_played(state_path: Path, filename: str) -> Path:
    source = _queue_dir(state_path) / filename
    if source.name != filename or not filename.endswith(".json"):
        raise RundownError(f"invalid queue item: {filename}")
    destination = state_path / "played" / filename
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.replace(source, destination)
    except FileNotFoundError as error:
        raise RundownError(f"queue item disappeared: {filename}") from error
    return destination
