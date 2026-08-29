#!/usr/bin/env python3
"""Receive Android audio-focus and recorder receipts over Termux loopback only."""

from __future__ import annotations

import os
import base64
import re
import socket
import tempfile
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("STATION_FOCUS_RECEIPT_PORT", "18765"))
STATES = {
    "/focus": os.path.expanduser(os.environ.get(
        "STATION_FOCUS_STATE", "~/.local/state/audience-of-one-phone/focus.state"
    )),
    "/record": os.path.expanduser(os.environ.get(
        "STATION_RECORD_STATE", "~/.local/state/audience-of-one-phone/record.state"
    )),
}
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
SAFE_STATE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
PHONE_ROOT = os.path.expanduser(os.environ.get(
    "STATION_PHONE_ROOT", "~/.local/share/audience-of-one-phone/transport"
))
MEDIA_ROOT = os.path.expanduser(os.environ.get(
    "STATION_CALLIN_MEDIA_ROOT", "~/storage/shared/Download/AudienceOfOne"
))
EVENT_SOCKET = os.path.expanduser(os.environ.get(
    "STATION_PHONE_EVENT_SOCKET", "~/.local/state/audience-of-one-phone/events.sock"
))


def notify(event: str) -> None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.sendto(event.encode("utf-8"), EVENT_SOCKET)
    except OSError:
        pass
    finally:
        client.close()


def stage_call_in(request_id: str) -> None:
    match = re.fullmatch(r"callin-([0-9]{10,18})", request_id)
    if not match:
        return
    clip_id = match.group(1)
    source = os.path.join(MEDIA_ROOT, "call-in-work", f"{clip_id}.m4a")
    for _attempt in range(10):
        if os.path.getsize(source) if os.path.exists(source) else 0:
            break
        time.sleep(0.1)
    else:
        return
    outbox = os.path.join(PHONE_ROOT, "call-in-outbox")
    os.makedirs(outbox, exist_ok=True)
    with open(source, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    wrapped = "\n".join(
        encoded[offset:offset + 1024] for offset in range(0, len(encoded), 1024)
    ) + "\nEND.\n"
    target = os.path.join(outbox, f"{clip_id}.b64")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{clip_id}.", dir=outbox, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(wrapped)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.unlink(source)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    notify(f"call-in:{clip_id}")


def publish(target: str, request_id: str, state: str, result: int, updated_at: int) -> None:
    if not SAFE_ID.fullmatch(request_id) or not SAFE_STATE.fullmatch(state):
        raise ValueError("invalid receipt")
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=directory, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(
                f"request_id={request_id} state={state} result={int(result)} "
                f"updated_at={int(updated_at)}\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class ReceiptHandler(BaseHTTPRequestHandler):
    server_version = "AudienceOfOneFocus/1"

    def do_POST(self) -> None:
        target = STATES.get(self.path)
        if target is None:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 1 <= length <= 4096:
            self.send_error(400)
            return
        try:
            fields = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8"), keep_blank_values=True
            )
            publish(
                target,
                fields.get("request_id", [""])[0],
                fields.get("state", [""])[0],
                int(fields.get("result", ["0"])[0]),
                int(fields.get("updated_at", ["0"])[0]),
            )
            if self.path == "/record" and fields.get("state", [""])[0] == "saved":
                stage_call_in(fields.get("request_id", [""])[0])
        except (UnicodeDecodeError, ValueError):
            self.send_error(400)
            return
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), ReceiptHandler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=0.25)
