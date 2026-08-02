"""Always-on rundown consumer for receipt-driven station playout."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config as station_config
from . import rundown
from .adapters import music_client
from .adapters.spotify import SpotifyError
from .desktop import DesktopEngine, PlayoutError
from .transactions import Journal


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def status_path(state_path: Path) -> Path:
    return state_path / "scheduler" / "status.json"


def read_status(state_path: Path) -> dict:
    try:
        payload = json.loads(status_path(state_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"running": False, "state": "stopped"}
    pid = payload.get("pid")
    alive = False
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            pass
    payload["running"] = bool(payload.get("running") and alive)
    if not payload["running"]:
        payload["state"] = "stopped"
    return payload


class StationScheduler:
    def __init__(
        self,
        config: dict[str, Any],
        state_path: Path,
        *,
        music: Any | None = None,
        engine: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.state_path = state_path
        self.settings = config.get("scheduler") or {}
        self.poll_seconds = float(self.settings.get("poll_seconds", 1.0))
        self.prepare_window = float(self.settings.get("prepare_window_seconds", 75.0))
        self.boundary_lead = float(self.settings.get("boundary_lead_seconds", 1.0))
        self.music = music or music_client(config, state_path)
        self.engine = engine or DesktopEngine(config, state_path, spotify=self.music)
        self.sleep = sleep
        self.clock = clock
        self.journal = Journal(state_path)
        self.prepared: tuple[dict, dict, dict] | None = None
        self.active_uri: str | None = None
        self.last_remaining: float | None = None
        self.after_mode = "autoplay"
        self.snapshot_failed = False
        self.stopping = False
        self.last_action = "starting"

    def log(self, message: str) -> None:
        path = self.state_path / "scheduler" / "scheduler.log"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{self.clock():.3f} {message}\n")
        path.chmod(0o600)

    def _next_item(self) -> dict | None:
        for item in rundown.items(self.state_path):
            if not isinstance(item.get("data"), dict):
                continue
            transaction = self.journal.ensure(item["filename"])
            state = transaction.get("state")
            if state == "queued":
                return item
            if state == "ready" and self.prepared is not None \
                    and self.prepared[0]["filename"] == item["filename"]:
                return item
            if state in {"preparing", "ready", "firing"}:
                self.journal.set_state(
                    item["filename"], "failed",
                    "scheduler restarted during an uncertain playout; use station retry",
                )
                self.log(f"shelved interrupted item {item['id']}")
        return None

    def _snapshot(self) -> dict | None:
        self.snapshot_failed = False
        try:
            return self.music.snapshot()
        except (SpotifyError, OSError, ValueError) as error:
            self.snapshot_failed = True
            self.log(f"snapshot unavailable: {error}")
            return None

    @staticmethod
    def _remaining(snapshot: dict | None) -> float | None:
        if not snapshot:
            return None
        try:
            duration = float(snapshot.get("duration_ms") or 0) / 1000.0
            progress = float(snapshot.get("progress_ms") or 0) / 1000.0
        except (TypeError, ValueError):
            return None
        if duration <= 0:
            return None
        return max(0.0, duration - progress)

    def _fire_window(self, programme: dict, prepared: dict) -> float:
        if rundown.normalize_transition(programme.get("transition", "overlap")) == "tail":
            return max(self.boundary_lead, float(prepared.get("duration") or 0) + 1.5)
        return self.boundary_lead

    def _prepare(self, item: dict) -> None:
        programme, prepared = self.engine.prepare_boundary(item)
        self.prepared = (item, programme, prepared)
        self.last_action = f"prepared:{item['id']}"
        self.log(self.last_action)

    def _set_after(self, programme: dict, transaction: dict) -> None:
        mode = str(programme.get("after") or "autoplay")
        self.after_mode = mode
        self.journal.receipt(transaction["item"], "track", {"after_mode": mode})
        track = transaction.get("track") or {}
        playback = track.get("playback") or {}
        device = playback.get("device") or track.get("device")
        if mode == "repeat" and isinstance(device, dict):
            try:
                self.music.set_repeat_on_device("track", device)
            except SpotifyError as error:
                self.after_mode = "autoplay"
                self.journal.receipt(transaction["item"], "track", {
                    "after_mode": "autoplay", "after_error": str(error),
                })
                self.log(f"repeat policy failed: {error}")

    def _fire(self) -> None:
        if self.prepared is None:
            raise PlayoutError("scheduler has no prepared item")
        item, programme, prepared = self.prepared
        transaction = self.engine.fire_boundary(item, programme, prepared)
        track = transaction.get("track") or {}
        uri = track.get("uri") or (track.get("playback") or {}).get("uri")
        if isinstance(uri, str) and uri:
            self.active_uri = uri
        else:
            snapshot = self._snapshot()
            if snapshot and snapshot.get("is_playing"):
                self.active_uri = snapshot.get("uri")
        self._set_after(programme, transaction)
        self.last_remaining = None
        self.prepared = None
        self.last_action = f"played:{item['id']}"
        self.log(self.last_action)

    def _clear_repeat_for_next_item(self, snapshot: dict | None) -> None:
        if self.after_mode != "repeat" or not snapshot:
            return
        device = snapshot.get("device")
        if isinstance(device, dict):
            self.music.set_repeat_on_device("off", device)
        self.after_mode = "autoplay"

    def _handle_idle_after(
        self,
        snapshot: dict | None,
        remaining: float | None,
        previous_remaining: float | None,
    ) -> str:
        if self.after_mode == "stop":
            naturally_ended = bool(
                snapshot is None
                and previous_remaining is not None
                and previous_remaining <= max(3.0, self.boundary_lead)
            )
            changed = bool(
                snapshot and self.active_uri and snapshot.get("uri") != self.active_uri
            )
            at_end = bool(
                snapshot and remaining is not None and remaining <= self.boundary_lead
            )
            if changed or at_end:
                device = snapshot.get("device") or {}
                device_spec = device.get("id") or device.get("name") or None
                self.music.pause(device_spec)
                self.after_mode = "autoplay"
                self.last_action = "after:stopped"
                self.log(self.last_action)
                return self.last_action
            if naturally_ended:
                self.after_mode = "autoplay"
                self.last_action = "after:stopped-natural"
                self.log(self.last_action)
                return self.last_action
        if self.last_action in {"after:stopped", "after:stopped-natural"}:
            return self.last_action
        self.last_action = "idle"
        return self.last_action

    def tick(self) -> str:
        item = self._next_item()
        snapshot = self._snapshot()
        remaining = self._remaining(snapshot)
        playing = bool(snapshot and snapshot.get("is_playing"))

        if self.snapshot_failed:
            self.last_action = "waiting:playback-snapshot"
            return self.last_action

        if item is None:
            if playing and self.active_uri is None:
                self.active_uri = snapshot.get("uri")
            previous_remaining = self.last_remaining
            action = self._handle_idle_after(snapshot, remaining, previous_remaining)
            self.last_remaining = remaining
            return action

        self._clear_repeat_for_next_item(snapshot)
        if playing and self.active_uri is None:
            self.active_uri = snapshot.get("uri")

        changed = bool(
            playing and self.active_uri and snapshot.get("uri") != self.active_uri
        )
        ended = bool(
            self.active_uri and not playing
            and self.last_remaining is not None
            and self.last_remaining <= max(3.0, self.boundary_lead)
        )
        cold = not playing and self.active_uri is None

        if self.prepared is None and (
            cold or changed or ended or remaining is None or remaining <= self.prepare_window
        ):
            try:
                self._prepare(item)
            except PlayoutError as error:
                self.log(f"prepare failed {item['id']}: {error}")
                self.last_action = f"failed:{item['id']}"
                return self.last_action

        should_fire = cold or changed or ended
        if self.prepared is not None and remaining is not None:
            _, programme, prepared = self.prepared
            should_fire = should_fire or remaining <= self._fire_window(programme, prepared)

        if should_fire:
            if self.prepared is None:
                try:
                    self._prepare(item)
                except PlayoutError as error:
                    self.log(f"prepare failed {item['id']}: {error}")
                    self.last_action = f"failed:{item['id']}"
                    return self.last_action
            try:
                self._fire()
            except PlayoutError as error:
                self.prepared = None
                self.log(f"playout failed {item['id']}: {error}")
                self.last_action = f"failed:{item['id']}"
            return self.last_action

        self.last_remaining = remaining
        self.last_action = f"waiting:{item['id']}"
        return self.last_action

    def _publish_status(self, *, running: bool, state: str) -> None:
        current = self.prepared[0]["id"] if self.prepared else None
        _atomic_json(status_path(self.state_path), {
            "version": 1,
            "running": running,
            "state": state,
            "pid": os.getpid(),
            "updated_at": self.clock(),
            "current_item": current,
            "active_uri": self.active_uri,
            "last_action": self.last_action,
        })

    def run_forever(self) -> None:
        lock_path = self.state_path / "scheduler" / "scheduler.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock_path.open("a+", encoding="utf-8") as lock:
            lock_path.chmod(0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise PlayoutError("station scheduler is already running") from error

            def stop(_signum: int, _frame: Any) -> None:
                self.stopping = True

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            self._publish_status(running=True, state="running")
            self.log("scheduler started")
            try:
                while not self.stopping:
                    try:
                        self.tick()
                    except (SpotifyError, PlayoutError, OSError, ValueError) as error:
                        self.last_action = "loop-error"
                        self.log(f"loop error: {error}")
                    self._publish_status(running=True, state="running")
                    self.sleep(self.poll_seconds)
            finally:
                self.last_action = "stopped"
                self._publish_status(running=False, state="stopped")
                self.log("scheduler stopped")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="audience-of-one-scheduler")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = station_config.load(args.config)
        station_config.validate(config)
        StationScheduler(config, args.state_dir).run_forever()
    except (station_config.ConfigError, PlayoutError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
