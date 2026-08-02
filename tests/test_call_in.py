from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from audience_of_one.adapters.phone import PhoneError
from audience_of_one.call_in import CallInError, CallInWatcher


class FakeTransport:
    """Mimic the phone MCP file tools, including the server's line paging."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})
        self.deletes: list[str] = []
        self.read_calls = 0

    def list_names(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted({
            name[len(prefix):] for name in self.files
            if name.startswith(prefix) and "/" not in name[len(prefix):]
        })

    def read_lines(self, path, *, start_line=1, max_lines=100):
        self.read_calls += 1
        if path not in self.files:
            return []
        lines = self.files[path].splitlines()
        return lines[start_line - 1:start_line - 1 + max_lines]

    def delete(self, path, *, missing_ok=True):
        self.deletes.append(path)
        self.files.pop(path, None)


class FakeSTTResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body


def staged_clip(audio: bytes, columns: int = 8) -> str:
    encoded = base64.b64encode(audio).decode("ascii")
    wrapped = [encoded[i:i + columns] for i in range(0, len(encoded), columns)]
    return "\n".join(wrapped) + "\nEND.\n"


class CallInWatcherTests(unittest.TestCase):
    def watcher(self, transport, state, **kwargs):
        requests = []

        def opener(request, timeout=None):
            requests.append(request)
            return FakeSTTResponse(json.dumps({"text": "hello there"}).encode())

        kwargs.setdefault("stt_url", "http://127.0.0.1:8792")
        kwargs.setdefault("opener", opener)
        kwargs.setdefault("clock", lambda: 1_722_000_000.0)
        watcher = CallInWatcher(transport, Path(state), **kwargs)
        watcher.test_requests = requests
        return watcher

    def test_call_event_pages_decodes_transcribes_and_deletes(self):
        audio = b"eight seconds of aac audio" * 40
        transport = FakeTransport({
            "call-in-outbox/1722000000000.b64": staged_clip(audio),
        })
        with tempfile.TemporaryDirectory() as raw:
            watcher = self.watcher(transport, raw, page_lines=7)
            events = watcher.poll_once()
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["event"], "call")
            self.assertEqual(event["id"], "1722000000000")
            self.assertEqual(event["transcript"], "hello there")
            self.assertEqual(Path(event["audio_path"]).read_bytes(), audio)
            self.assertEqual(event["bytes"], len(audio))
            self.assertGreater(transport.read_calls, 1)
            self.assertIn("call-in-outbox/1722000000000.b64", transport.deletes)
            self.assertEqual(watcher.test_requests[0].data, audio)
            journal = (Path(raw) / "call-in" / "events.jsonl").read_text()
            self.assertIn('"call"', journal)
            # a second pass must not replay the processed clip
            self.assertEqual(watcher.poll_once(), [])

    def test_clip_without_sentinel_waits_for_the_next_pass(self):
        transport = FakeTransport({
            "call-in-outbox/1722000000001.b64": "YWJjZGVm\nYWJjZGVm",
        })
        with tempfile.TemporaryDirectory() as raw:
            watcher = self.watcher(transport, raw)
            self.assertEqual(watcher.poll_once(), [])
            self.assertEqual(transport.deletes, [])
            transport.files["call-in-outbox/1722000000001.b64"] += "\nEND.\n"
            events = watcher.poll_once()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "call")

    def test_ring_file_becomes_a_ring_event_and_is_claimed(self):
        transport = FakeTransport({"call-in/ring": ""})
        with tempfile.TemporaryDirectory() as raw:
            watcher = self.watcher(transport, raw)
            events = watcher.poll_once()
            self.assertEqual([event["event"] for event in events], ["ring"])
            self.assertEqual(transport.deletes, ["call-in/ring"])
            self.assertEqual(watcher.poll_once(), [])

    def test_unsafe_outbox_names_are_ignored(self):
        transport = FakeTransport({
            "call-in-outbox/.hidden.b64": "YQ==\nEND.\n",
            "call-in-outbox/notes.txt": "not a clip",
        })
        with tempfile.TemporaryDirectory() as raw:
            watcher = self.watcher(transport, raw)
            self.assertEqual(watcher.poll_once(), [])
            self.assertEqual(transport.deletes, [])

    def test_stt_failure_keeps_the_audio_and_reports_the_error(self):
        def failing_opener(request, timeout=None):
            raise OSError("connection refused")

        audio = b"still worth keeping"
        transport = FakeTransport({
            "call-in-outbox/1722000000002.b64": staged_clip(audio),
        })
        with tempfile.TemporaryDirectory() as raw:
            watcher = self.watcher(transport, raw, opener=failing_opener)
            events = watcher.poll_once()
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertIsNone(event["transcript"])
            self.assertIn("STT request failed", event["stt_error"])
            self.assertEqual(Path(event["audio_path"]).read_bytes(), audio)
            self.assertIn("call-in-outbox/1722000000002.b64", transport.deletes)

    def test_transport_failure_is_wrapped_as_call_in_error(self):
        class BrokenTransport:
            def list_names(self, path):
                raise PhoneError("phone MCP request failed")

        with tempfile.TemporaryDirectory() as raw:
            watcher = self.watcher(BrokenTransport(), raw)
            with self.assertRaisesRegex(CallInError, "call-in poll failed"):
                watcher.poll_once()

    def test_stt_url_must_be_http(self):
        with tempfile.TemporaryDirectory() as raw, \
                self.assertRaisesRegex(CallInError, "http"):
            CallInWatcher(FakeTransport(), Path(raw), stt_url="ftp://bad")


class CallInPagingBoundaryTests(unittest.TestCase):
    def test_exact_page_multiple_without_sentinel_returns_incomplete(self):
        transport = FakeTransport({
            "call-in-outbox/1722000000003.b64": "YWJj\nZGVm",
        })
        with tempfile.TemporaryDirectory() as raw:
            watcher = CallInWatcher(
                transport, Path(raw), stt_url="", page_lines=2, max_pages=3,
            )
            self.assertEqual(watcher.poll_once(), [])


if __name__ == "__main__":
    unittest.main()
