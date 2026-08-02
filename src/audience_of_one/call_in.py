"""Voice call-in: pull phone-staged clips and rings, emit one JSON event each."""

from __future__ import annotations

import base64
import binascii
import json
import posixpath
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .adapters.phone import PhoneError

CLIP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.b64$")
SENTINEL = "END."


class CallInError(RuntimeError):
    pass


class CallInWatcher:
    """Poll the phone receiver for ring touches and base64-staged voice clips.

    The phone stages each clip as ``<outbox>/<epoch>.b64`` — base64 wrapped to
    fixed columns with a final ``END.`` line, the same sentinel convention the
    voice inbox uses in the other direction. A clip without the sentinel is
    still being written and is left for the next pass.
    """

    def __init__(
        self,
        transport: Any,
        state_path: Path,
        *,
        stt_url: str = "",
        outbox_dir: str = "call-in-outbox",
        ring_path: str = "call-in/ring",
        page_lines: int = 200,
        max_pages: int = 500,
        stt_timeout: float = 120.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
    ):
        if stt_url and not stt_url.startswith(("http://", "https://")):
            raise CallInError("call-in STT URL must use http or https")
        self.transport = transport
        self.state_path = Path(state_path) / "call-in"
        self.stt_url = stt_url
        self.outbox_dir = outbox_dir.strip("/")
        self.ring_path = ring_path.strip("/")
        self.page_lines = max(1, int(page_lines))
        self.max_pages = max(1, int(max_pages))
        self.stt_timeout = max(1.0, float(stt_timeout))
        self.opener = opener
        self.clock = clock

    def _record(self, event: dict[str, Any]) -> dict[str, Any]:
        self.state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state_path / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def _ring_event(self) -> dict[str, Any] | None:
        directory, name = posixpath.split(self.ring_path)
        if not directory or not name:
            raise CallInError("call-in ring path needs a directory and a file name")
        if name not in self.transport.list_names(directory):
            return None
        self.transport.delete(self.ring_path)
        return self._record({
            "version": 1,
            "event": "ring",
            "at": int(self.clock()),
        })

    def _read_clip(self, name: str) -> str | None:
        """Page through one staged clip; None while the sentinel is missing."""
        lines: list[str] = []
        start = 1
        for _ in range(self.max_pages):
            page = self.transport.read_lines(
                f"{self.outbox_dir}/{name}",
                start_line=start,
                max_lines=self.page_lines,
            )
            lines.extend(page)
            while lines and not lines[-1].strip():
                lines.pop()
            if lines and lines[-1].strip().endswith(SENTINEL):
                body = "".join(line.strip() for line in lines)
                return body[:-len(SENTINEL)]
            if len(page) < self.page_lines:
                return None
            start += len(page)
        return None

    def _transcribe(self, audio: bytes) -> tuple[str | None, str | None]:
        if not self.stt_url:
            return None, "no STT endpoint configured"
        request = urllib.request.Request(
            self.stt_url,
            data=audio,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        try:
            response = self.opener(request, timeout=self.stt_timeout)
            payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            return None, f"STT request failed: {error}"
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            return None, "STT response carried no text field"
        return text, None

    def _call_event(self, name: str) -> dict[str, Any] | None:
        marker = self.state_path / f"{name}.json"
        if marker.exists():
            return None
        encoded = self._read_clip(name)
        if encoded is None:
            return None
        event: dict[str, Any] = {
            "version": 1,
            "event": "call",
            "id": name.removesuffix(".b64"),
            "at": int(self.clock()),
        }
        try:
            audio = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            audio = b""
            event["decode_error"] = "clip was not valid base64"
        if audio:
            clips = self.state_path / "clips"
            clips.mkdir(parents=True, exist_ok=True, mode=0o700)
            audio_path = clips / f"{event['id']}.m4a"
            audio_path.write_bytes(audio)
            audio_path.chmod(0o600)
            event["audio_path"] = str(audio_path)
            event["bytes"] = len(audio)
            transcript, stt_error = self._transcribe(audio)
            event["transcript"] = transcript
            if stt_error:
                event["stt_error"] = stt_error
        self.transport.delete(f"{self.outbox_dir}/{name}")
        self.state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker.write_text(
            json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return self._record(event)

    def poll_once(self) -> list[dict[str, Any]]:
        events = []
        try:
            ring = self._ring_event()
            if ring:
                events.append(ring)
            for name in sorted(self.transport.list_names(self.outbox_dir)):
                if not CLIP_NAME.fullmatch(name):
                    continue
                call = self._call_event(name)
                if call:
                    events.append(call)
        except PhoneError as error:
            raise CallInError(f"call-in poll failed: {error}") from error
        return events
