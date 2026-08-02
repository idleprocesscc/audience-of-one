"""Durable manual fader gestures for the local Spotify desktop app."""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .desktop import MacSpotifyFader, PlayoutError


class LiveFader:
    """Move Spotify while preserving the first level as a durable restore point."""

    def __init__(self, state_path: Path, *, backend: Any | None = None):
        self.state_path = state_path
        self.backend = backend or MacSpotifyFader()
        self.control_path = state_path / "control" / "fader.json"

    @contextmanager
    def _claim(self):
        self.state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.state_path / "desktop-playout.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            lock_path.chmod(0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise PlayoutError(
                    "another desktop playout owns the station lock"
                ) from error
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load(self) -> dict:
        try:
            data = json.loads(self.control_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise PlayoutError("no manual fader restore point is active") from error
        except (OSError, json.JSONDecodeError) as error:
            raise PlayoutError(f"cannot read manual fader state: {error}") from error
        restore = data.get("restore_percent")
        if not isinstance(restore, int) or isinstance(restore, bool) or not 0 <= restore <= 100:
            raise PlayoutError("manual fader restore point is invalid")
        return data

    def _write(self, data: dict) -> None:
        self.control_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.control_path.with_name(
            f".{self.control_path.name}.{os.getpid()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, self.control_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _seconds(value: float) -> float:
        seconds = float(value)
        if seconds < 0:
            raise PlayoutError("fader travel time must be non-negative")
        return seconds

    def move(self, target: int, *, seconds: float) -> dict:
        if isinstance(target, bool) or not isinstance(target, int) or not 0 <= target <= 100:
            raise PlayoutError("fader target must be an integer from 0 to 100")
        seconds = self._seconds(seconds)
        with self._claim():
            current = self.backend.volume()
            if self.control_path.exists():
                restore = self._load()["restore_percent"]
            else:
                restore = current
            state = {
                "version": 1,
                "restore_percent": restore,
                "current_percent": current,
                "target_percent": target,
                "updated_at": time.time(),
            }
            # Publish the restore point before moving. A failed gesture can still
            # be repaired with `station fader restore`.
            self._write(state)
            receipt = self.backend.fade(target, seconds)
            state.update({
                "current_percent": receipt["to_percent"],
                "last_receipt": receipt,
                "updated_at": time.time(),
            })
            self._write(state)
            return {
                "moved": True,
                "restore_percent": restore,
                "seconds": seconds,
                **receipt,
            }

    def restore(self, *, seconds: float) -> dict:
        seconds = self._seconds(seconds)
        with self._claim():
            state = self._load()
            receipt = self.backend.fade(state["restore_percent"], seconds)
            self.control_path.unlink(missing_ok=True)
            return {
                "restored": True,
                "restore_percent": state["restore_percent"],
                "seconds": seconds,
                **receipt,
            }
