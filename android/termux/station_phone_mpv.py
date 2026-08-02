#!/data/data/com.termux/files/usr/bin/python3
"""Control one persistent Termux mpv without exposing remote URLs in receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import time
from pathlib import Path
from typing import Any


class MPVError(RuntimeError):
    pass


class MPV:
    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self.request_id = 0

    def command(self, *values: Any) -> Any:
        self.request_id += 1
        request_id = self.request_id
        payload = json.dumps({
            "command": list(values), "request_id": request_id,
        }, separators=(",", ":")).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                connection.connect(str(self.socket_path))
                connection.sendall(payload)
                pending = b""
                while len(pending) < 1_048_576:
                    chunk = connection.recv(65_536)
                    if not chunk:
                        break
                    pending += chunk
                    lines = pending.split(b"\n")
                    pending = lines.pop()
                    for line in lines:
                        try:
                            response = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(response, dict) \
                                and response.get("request_id") == request_id:
                            if response.get("error") != "success":
                                raise MPVError(
                                    f"mpv rejected {values[0]}: {response.get('error')}"
                                )
                            return response.get("data")
        except OSError as error:
            raise MPVError(f"mpv IPC failed: {error}") from error
        raise MPVError("mpv returned no matching response")

    def snapshot(self) -> dict[str, Any]:
        def optional(name: str, fallback: Any) -> Any:
            try:
                return self.command("get_property", name)
            except MPVError:
                return fallback

        path = optional("path", "")
        pause = bool(optional("pause", True))
        progress = float(optional("time-pos", 0) or 0)
        duration = float(optional("duration", 0) or 0)
        volume = float(optional("volume", 0) or 0)
        loop_file = optional("loop-file", "no")
        return {
            "has_path": isinstance(path, str) and bool(path),
            "url_sha256": hashlib.sha256(path.encode()).hexdigest()
            if isinstance(path, str) and path else "",
            "is_playing": not pause,
            "progress_ms": round(progress * 1000),
            "duration_ms": round(duration * 1000),
            "volume_percent": round(volume),
            "repeat_state": "off" if loop_file in {False, None, "", "no"} else "context",
        }


def load(mpv: MPV, manifest_path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MPVError(f"cannot read music task: {error}") from error
    url = manifest.get("url") if isinstance(manifest, dict) else None
    expected = manifest.get("url_sha256") if isinstance(manifest, dict) else None
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise MPVError("music task has no HTTP stream")
    if not isinstance(expected, str) or hashlib.sha256(url.encode()).hexdigest() != expected:
        raise MPVError("music task URL hash does not match")
    expires_at = float(manifest.get("expires_at") or 0)
    if expires_at and expires_at <= time.time() + 5:
        raise MPVError("music task expired before playout")

    mpv.command("loadfile", url, "replace")
    mpv.command("set_property", "pause", False)
    mpv.command("set_property", "loop-file", "no")
    deadline = time.monotonic() + 15
    started = None
    while time.monotonic() < deadline:
        current = mpv.snapshot()
        if current["url_sha256"] == expected and current["is_playing"] \
                and current["duration_ms"] > 0:
            started = current
            break
        time.sleep(0.1)
    if started is None:
        raise MPVError("mpv did not confirm the requested stream")
    time.sleep(1.2)
    after = mpv.snapshot()
    advance = after["progress_ms"] - started["progress_ms"]
    if after["url_sha256"] != expected or not after["is_playing"] or advance < 400:
        raise MPVError("mpv stream did not prove position motion")
    return {
        **after,
        "advance_ms": advance,
        "confirmed": True,
    }


def fade(mpv: MPV, target: int, seconds: float) -> dict[str, Any]:
    if not 0 <= target <= 100 or seconds < 0:
        raise MPVError("fader target or travel is invalid")
    start = int(mpv.snapshot()["volume_percent"])
    steps = max(1, min(40, round(seconds / 0.1)))
    values = []
    for index in range(1, steps + 1):
        value = round(start + (target - start) * index / steps)
        mpv.command("set_property", "volume", value)
        values.append(value)
        if seconds:
            time.sleep(seconds / steps)
    return {"from_percent": start, "to_percent": target, "steps": values}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--socket", type=Path, required=True)
    subs = result.add_subparsers(dest="command", required=True)
    load_parser = subs.add_parser("load")
    load_parser.add_argument("manifest", type=Path)
    fade_parser = subs.add_parser("fade")
    fade_parser.add_argument("target", type=int)
    fade_parser.add_argument("seconds", type=float)
    subs.add_parser("snapshot")
    subs.add_parser("pause")
    subs.add_parser("resume")
    subs.add_parser("quit")
    return result


def main() -> int:
    args = parser().parse_args()
    mpv = MPV(args.socket)
    try:
        if args.command == "load":
            result = load(mpv, args.manifest)
        elif args.command == "fade":
            result = fade(mpv, args.target, args.seconds)
        elif args.command == "snapshot":
            result = mpv.snapshot()
        else:
            mpv.command("set_property", "pause", args.command == "pause") \
                if args.command in {"pause", "resume"} else mpv.command("quit")
            result = {"accepted": True, "action": args.command}
    except (MPVError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, separators=(",", ":")))
        return 2
    print(json.dumps({"ok": True, **result}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
