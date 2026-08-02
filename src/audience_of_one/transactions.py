"""Durable playout transaction journal."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from .provenance import PROVENANCE

STATES = {"queued", "preparing", "ready", "firing", "played", "failed"}


class TransactionError(RuntimeError):
    pass


def item_id(value: str) -> str:
    name = Path(value).name
    if name != value or not name.endswith(".json") or not name[:-5].isdigit():
        raise TransactionError(f"invalid item name: {value}")
    return name[:-5]


class Journal:
    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.transaction_dir = state_path / "transactions"
        self.events_path = state_path / "events.jsonl"

    def path_for(self, value: str) -> Path:
        return self.transaction_dir / f"{item_id(value)}.json"

    def load(self, value: str) -> dict | None:
        path = self.path_for(value)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise TransactionError(f"cannot read transaction {path.name}: {error}") from error

    @staticmethod
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

    def _append_event(self, data: dict, event: str, detail: str = "") -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        row = {
            "provenance": PROVENANCE,
            "at": time.time(),
            "item": data["item"],
            "attempt_id": data["attempt_id"],
            "event": event,
        }
        if detail:
            row["detail"] = detail
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.events_path.chmod(0o600)

    @staticmethod
    def new(value: str) -> dict:
        base = item_id(value)
        now = time.time()
        attempt = secrets.token_hex(6)
        return {
            "version": 1,
            "provenance": PROVENANCE,
            "item": f"{base}.json",
            "attempt_id": attempt,
            "remote_id": f"{base}--{attempt}",
            "state": "queued",
            "created_at": now,
            "updated_at": now,
            "history": [{"state": "queued", "at": now}],
            "voice": {},
            "track": {},
            "improv": {},
            "recovery": {},
        }

    def save(self, data: dict) -> None:
        data["updated_at"] = time.time()
        self._atomic_write(self.path_for(data["item"]), data)

    def receipt(self, value: str, component: str, payload: dict) -> dict:
        if component not in {"voice", "track", "improv", "recovery"}:
            raise TransactionError(f"invalid receipt component: {component}")
        data = self.ensure(value)
        data[component] = {**data.get(component, {}), **payload}
        self._append_event(data, f"{component}_receipt")
        self.save(data)
        return data

    def ensure(self, value: str) -> dict:
        data = self.load(value)
        if data is None:
            data = self.new(value)
            self.save(data)
            self._append_event(data, "queued")
        return data

    def begin(self, value: str) -> dict:
        data = self.ensure(value)
        if data["state"] == "failed":
            raise TransactionError(
                "transaction is failed; use station retry before another playout attempt"
            )
        if data["state"] != "queued":
            raise TransactionError(
                f"transaction is already {data['state']}; use station retry after interruption"
            )
        now = time.time()
        data["state"] = "preparing"
        data["history"].append({"state": "preparing", "at": now})
        self._append_event(data, "preparing")
        self.save(data)
        return data

    def set_state(self, value: str, state: str, detail: str = "") -> dict:
        if state not in STATES:
            raise TransactionError(f"invalid state: {state}")
        data = self.ensure(value)
        entry = {"state": state, "at": time.time()}
        if detail:
            entry["detail"] = detail
        data["state"] = state
        data.setdefault("history", []).append(entry)
        if state == "failed":
            data["error"] = detail or "unknown failure"
        else:
            data.pop("error", None)
        self._append_event(data, state, detail)
        self.save(data)
        return data

    def reset(self, value: str) -> dict:
        old = self.load(value)
        data = self.new(value)
        if old:
            data["previous_attempt_id"] = old.get("attempt_id")
        self.save(data)
        self._append_event(data, "retry_reset")
        return data
