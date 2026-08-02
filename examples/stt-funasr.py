#!/usr/bin/env python3
"""Optional reference STT endpoint for `station call-in watch`.

Not part of the package and not a dependency: any server that accepts
POSTed audio bytes and answers {"text": "..."} satisfies the call-in
contract. This one uses FunASR's paraformer-zh model, which transcribes
Mandarin far better than small local Whisper builds.

    pip install funasr
    python3 examples/stt-funasr.py          # listens on 127.0.0.1:8792

Then set call_in.stt_url = "http://127.0.0.1:8792" in config.toml.
"""

import json
import os
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

from funasr import AutoModel  # pip install funasr

HOST = os.environ.get("STATION_STT_BIND", "127.0.0.1")
PORT = int(os.environ.get("STATION_STT_PORT", "8792"))

print("Loading FunASR paraformer-zh (first run downloads the model)...")
MODEL = AutoModel(model="paraformer-zh", disable_update=True)
print(f"STT ready on http://{HOST}:{PORT}")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = tempfile.mktemp(prefix="station-stt-", suffix=".bin")
        wav = raw + ".wav"
        text = ""
        try:
            with open(raw, "wb") as handle:
                handle.write(self.rfile.read(length))
            # normalize whatever container arrived to 16 kHz mono
            converted = subprocess.run(
                ["ffmpeg", "-y", "-i", raw, "-ar", "16000", "-ac", "1", wav],
                capture_output=True, timeout=30,
            ).returncode == 0
            result = MODEL.generate(input=wav if converted else raw)
            text = result[0]["text"].replace(" ", "")
        except Exception:  # noqa: BLE001 — report empty text, keep serving
            text = ""
        finally:
            for path in (raw, wav):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        body = json.dumps({"text": text}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    HTTPServer((HOST, PORT), Handler).serve_forever()
