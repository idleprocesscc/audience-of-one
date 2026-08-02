#!/usr/bin/env python3
"""Receive Android audio-focus receipts over Termux loopback only."""

from __future__ import annotations

import os
import re
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("STATION_FOCUS_RECEIPT_PORT", "18765"))
STATE = os.path.expanduser(os.environ.get(
    "STATION_FOCUS_STATE", "~/.local/state/audience-of-one-phone/focus.state"
))
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
SAFE_STATE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def publish(request_id: str, state: str, result: int, updated_at: int) -> None:
    if not SAFE_ID.fullmatch(request_id) or not SAFE_STATE.fullmatch(state):
        raise ValueError("invalid focus receipt")
    directory = os.path.dirname(STATE)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".focus-", dir=directory, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(
                f"request_id={request_id} state={state} result={int(result)} "
                f"updated_at={int(updated_at)}\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class ReceiptHandler(BaseHTTPRequestHandler):
    server_version = "AudienceOfOneFocus/1"

    def do_POST(self) -> None:
        if self.path != "/focus":
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
                fields.get("request_id", [""])[0],
                fields.get("state", [""])[0],
                int(fields.get("result", ["0"])[0]),
                int(fields.get("updated_at", ["0"])[0]),
            )
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
